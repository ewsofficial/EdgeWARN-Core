"""StormProb track history builder (Phase 1).

Builds exactly 30 chronological rows per tracked cell, left-padded with zeros
and a false mask, mirroring StormProb's ``pack_storm_track`` /
``build_training_sample_from_packed_track`` / ``build_trajectory_sequence``
semantics with explicitly documented Phase 1 policies:

- duplicate timestamps: last-wins replacement (matches the EdgeWARN history
  manager's refresh semantics). Training dedups first-wins on raw strings;
  training tracks carry unique timestamps, so no observed track changes
  meaning under either rule. Replacements are counted in ``meta``.
- input order vs time order: rows are committed in chronological order; when
  arrival order was nonmonotone a ``reordered`` flag is recorded.
- scan gaps: no interpolation is performed (training has none either). Per-row
  ``dt_minutes`` and ``gap_minutes`` metadata expose gaps explicitly, and rows
  following a gap larger than ``gap_flag_minutes`` (default 15) are flagged.
- split/merge lineage: ``LINEAGE_POLICY = "per-cell-id-independent"``. Each
  stable track/cell id commits its own rows; ``parent_ids`` / ``split_from``
  / ``event_type`` are carried as metadata. A split child therefore starts a
  new track (optionally fork-inheriting via :func:`fork_track`), and a merge
  target starts a new track with ``merged_from`` parents. No split/merge
  lineage was observed in 4500+ surveyed training files, so this matches the
  training track definition (one cell file = one independent track).
- new cells with a single valid row are eligible: the parity fixtures confirm
  the one-valid-row path.

From the same committed rows the builder derives the motion-model tensors
(``current`` [135], ``history_sequence`` [30, 135], ``history_mask`` [30],
``trajectory_sequence`` [30, 16], ``trajectory_mask`` [30]) and the radial
tensors (``radial_history`` [30, 64], ``radial_statistics_history`` [30, 1],
``radial_history_mask`` [30]).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from . import features as _features
from . import geometry as _geometry

HISTORY_STEPS = _features.HISTORY_STEPS
N_RAYS = _geometry.N_RAYS
LINEAGE_POLICY = "per-cell-id-independent"
DEFAULT_GAP_FLAG_MINUTES = 15.0


# ---------------------------------------------------------------------------
# Trajectory math (exact port of features.trajectory_feature_vector_from_history)
# ---------------------------------------------------------------------------

def centroid_displacement_m(start, end) -> tuple[float, float]:
    """East/north displacement in meters between [lat, lon] centroids."""
    slat, slon = float(start[0]), float(start[1])
    elat, elon = float(end[0]), float(end[1])
    mean_lat = math.radians((slat + elat) / 2.0)
    dy = (elat - slat) * 111000.0
    raw_dlon = elon - slon
    dlon = (raw_dlon + 180.0) % 360.0 - 180.0
    if raw_dlon == 180.0:  # keep training-exact +180
        dlon = 180.0
    dx = dlon * 111000.0 * math.cos(mean_lat)
    return dx, dy


def _window_velocity(centroids, times_s, index, window_seconds) -> tuple[float, float]:
    target = times_s[index] - window_seconds
    start_index = min(range(index), key=lambda c: abs(times_s[c] - target))
    elapsed = times_s[index] - times_s[start_index]
    if elapsed <= 0.0:
        return 0.0, 0.0
    dx, dy = centroid_displacement_m(centroids[start_index], centroids[index])
    return dx / elapsed, dy / elapsed


def _turn_rate_deg_per_min(prev, cur, dt_seconds) -> float:
    prev_speed = math.hypot(*prev)
    cur_speed = math.hypot(*cur)
    if prev_speed <= 1e-6 or cur_speed <= 1e-6:
        return 0.0
    prev_heading = math.atan2(prev[1], prev[0])
    cur_heading = math.atan2(cur[1], cur[0])
    change = math.atan2(math.sin(cur_heading - prev_heading),
                        math.cos(cur_heading - prev_heading))
    return math.degrees(change) * 60.0 / dt_seconds


def _recent_path_statistics(centroids, times_s, index, window_seconds=600.0):
    first = index
    while (first > 0 and times_s[index] - times_s[first - 1] <= window_seconds):
        first -= 1
    path_length = 0.0
    max_speed = 0.0
    for step in range(first + 1, index + 1):
        dx, dy = centroid_displacement_m(centroids[step - 1], centroids[step])
        distance = math.hypot(dx, dy)
        elapsed = times_s[step] - times_s[step - 1]
        path_length += distance
        if elapsed > 0.0:
            max_speed = max(max_speed, distance / elapsed)
    net_dx, net_dy = centroid_displacement_m(centroids[first], centroids[index])
    efficiency = math.hypot(net_dx, net_dy) / path_length if path_length > 0.0 else 0.0
    return efficiency, max_speed


def trajectory_vector_for_index(
    centroids: list[tuple[float, float]],
    times_s: list[float],
    history_steps: int = HISTORY_STEPS,
) -> list[float]:
    """16 trajectory features ending at the last centroid (training port)."""
    index = len(centroids) - 1
    if index <= 0:
        return [0.0] * 14 + [0.0, min(index + 1, history_steps) / float(history_steps)]
    dx, dy = centroid_displacement_m(centroids[index - 1], centroids[index])
    dt = times_s[index] - times_s[index - 1]
    if dt <= 0.0:
        raise ValueError("Track timestamps must increase monotonically.")
    velocity = (dx / dt, dy / dt)
    vel_5min = _window_velocity(centroids, times_s, index, 300.0)
    vel_10min = _window_velocity(centroids, times_s, index, 600.0)
    previous = (0.0, 0.0)
    if index >= 2:
        pdx, pdy = centroid_displacement_m(centroids[index - 2], centroids[index - 1])
        pdt = times_s[index - 1] - times_s[index - 2]
        if pdt > 0.0:
            previous = (pdx / pdt, pdy / pdt)
    accel_scale = 60.0 / dt
    accel = ((velocity[0] - previous[0]) * accel_scale,
             (velocity[1] - previous[1]) * accel_scale)
    turn_rate = _turn_rate_deg_per_min(previous, velocity, dt)
    efficiency, max_speed = _recent_path_statistics(centroids, times_s, index, 600.0)
    track_age_hours = (times_s[index] - times_s[0]) / 3600.0
    return [
        dx / 1000.0, dy / 1000.0, dt / 60.0,
        velocity[0], velocity[1],
        vel_5min[0], vel_5min[1], vel_10min[0], vel_10min[1],
        accel[0], accel[1], turn_rate, efficiency, max_speed,
        track_age_hours, min(index + 1, history_steps) / float(history_steps),
    ]


def build_trajectory_sequence(
    centroids: list[tuple[float, float]],
    times_s: list[float],
    history_steps: int = HISTORY_STEPS,
) -> tuple[list[list[float]], list[bool]]:
    """Cumulative per-row trajectory vectors for one committed track."""
    rows = [trajectory_vector_for_index(centroids[: i + 1], times_s[: i + 1],
                                        history_steps)
            for i in range(len(centroids))]
    return rows, [True] * len(rows)


# ---------------------------------------------------------------------------
# Track commit + 30-row tensor windows
# ---------------------------------------------------------------------------

def _row_key(epoch: float) -> int:
    # Millisecond quantization keeps distinct scan times distinct while
    # treating exact re-publications as duplicates.
    return int(round(epoch * 1000.0))


def commit_track_rows(
    observations: list[dict],
    *,
    history_steps: int = HISTORY_STEPS,
    gap_flag_minutes: float = DEFAULT_GAP_FLAG_MINUTES,
) -> dict:
    """Commit chronological track rows from arrival-ordered observations.

    Each observation needs ``timestamp`` (naive ISO), ``centroid`` ([lat, lon])
    and ``properties`` (mapping). Returns ``{"rows": [...], "meta": {...}}``;
    corrupt observations are skipped with counted reasons, never raised.
    """
    meta: dict[str, Any] = {
        "lineage_policy": LINEAGE_POLICY,
        "n_arrived": len(observations),
        "n_skipped": 0,
        "skip_reasons": {},
        "duplicates_replaced": 0,
        "reordered": False,
        "gap_flag_minutes": gap_flag_minutes,
        "gap_rows": [],
    }

    def skip(reason: str) -> None:
        meta["n_skipped"] += 1
        meta["skip_reasons"][reason] = meta["skip_reasons"].get(reason, 0) + 1

    parsed: list[dict] = []
    for position, obs in enumerate(observations):
        if not isinstance(obs, dict):
            skip("not-a-mapping")
            continue
        try:
            moment = _features.parse_analysis_timestamp(obs.get("timestamp"))
        except ValueError:
            skip("bad-timestamp")
            continue
        centroid = obs.get("centroid")
        try:
            clat, clon = float(centroid[0]), float(centroid[1])
        except (TypeError, ValueError, IndexError):
            skip("bad-centroid")
            continue
        if not (math.isfinite(clat) and math.isfinite(clon)):
            skip("non-finite-centroid")
            continue
        properties = obs.get("properties")
        if not isinstance(properties, dict):
            skip("bad-properties")
            continue
        epoch = _features._to_epoch_seconds(moment)
        parsed.append({
            "arrival": position,
            "timestamp": obs.get("timestamp"),
            "epoch": epoch,
            "centroid": [clat, clon],
            "properties": properties,
            "parent_ids": list(obs.get("parent_ids") or []),
            "split_from": obs.get("split_from"),
            "event_type": obs.get("event_type"),
            "tracking_mode": obs.get("tracking_mode"),
        })

    # Chronological commit; arrival order was nonmonotone when this permutes.
    chronological = sorted(parsed, key=lambda row: (row["epoch"], row["arrival"]))
    if [row["arrival"] for row in chronological] != sorted(row["arrival"] for row in parsed):
        meta["reordered"] = True

    # Duplicate timestamps: last arrival wins (history-manager refresh rule).
    by_time: dict[int, dict] = {}
    for row in chronological:
        key = _row_key(row["epoch"])
        if key in by_time:
            meta["duplicates_replaced"] += 1
        by_time[key] = row
    rows = [by_time[key] for key in sorted(by_time)]

    for position, row in enumerate(rows):
        if position == 0:
            row["dt_minutes"] = 0.0
            row["gap_minutes"] = 0.0
            row["post_gap"] = False
        else:
            dt_minutes = (row["epoch"] - rows[position - 1]["epoch"]) / 60.0
            row["dt_minutes"] = dt_minutes
            row["gap_minutes"] = dt_minutes
            row["post_gap"] = dt_minutes > gap_flag_minutes
            if row["post_gap"]:
                meta["gap_rows"].append(position)

    meta["n_committed"] = len(rows)
    return {"rows": rows, "meta": meta}


def fork_track(rows: list[dict], at_index: int = -1) -> list[dict]:
    """Start a child track inheriting committed rows up to ``at_index``.

    Split children normally start new tracks (per-cell-id independence); call
    this explicitly when an operator wants the parent stem as history prefix.
    The forked rows keep their timestamps/centroids/properties verbatim.
    """
    if not rows:
        raise ValueError("rows must not be empty.")
    return [dict(row) for row in rows[: at_index if at_index >= 0 else len(rows)]]


def build_model_inputs(
    rows: list[dict],
    *,
    history_steps: int = HISTORY_STEPS,
    radial_by_row: list[dict] | None = None,
) -> dict:
    """Derive motion + radial model tensors from committed track rows.

    ``radial_by_row`` optionally carries ``{"radii_km": [...], "log_area": x}``
    per row (from the geometry step); missing rows default to zeros with a
    true mask only where geometry was valid.
    """
    if not rows:
        raise ValueError("rows must not be empty.")
    if history_steps <= 0:
        raise ValueError("history_steps must be positive.")

    entries = [{"timestamp": row["timestamp"], "centroid": row["centroid"],
                "properties": row["properties"]} for row in rows]
    centroids = [(row["centroid"][0], row["centroid"][1]) for row in rows]
    times_s = [row["epoch"] for row in rows]

    current_rows: list[list[float]] = []
    initial_winds: list[dict] = []
    for index in range(len(rows)):
        vector, info = _features.build_current_feature_vector(entries, index, history_steps)
        # Track-relative age uses the committed (deduped) rows, matching the
        # training definition of storm age from the track start.
        current_rows.append(vector)
        initial_winds.append({"u": info["initial_u"], "v": info["initial_v"],
                              "status": info["initial_status"]})

    full_traj, _ = build_trajectory_sequence(centroids, times_s, history_steps)

    radial_rows: list[list[float]] = []
    stats_rows: list[list[float]] = []
    radial_valid: list[bool] = []
    for position in range(len(rows)):
        payload = (radial_by_row or [None] * len(rows))[position]
        try:
            radii = [float(v) for v in payload["radii_km"]]
            log_area = float(payload["log_area"])
            ok = (len(radii) == N_RAYS and math.isfinite(log_area)
                  and all(math.isfinite(v) for v in radii))
        except (TypeError, ValueError, KeyError, IndexError):
            radii, log_area, ok = [0.0] * N_RAYS, 0.0, False
        radial_rows.append(radii if ok else [0.0] * N_RAYS)
        stats_rows.append([log_area] if ok else [0.0])
        radial_valid.append(bool(ok))

    first = max(0, len(rows) - history_steps)
    valid_current = current_rows[first:]
    valid_traj = full_traj[first:]
    valid_radial = radial_rows[first:]
    valid_stats = stats_rows[first:]
    valid_radial_mask = radial_valid[first:]
    pad = history_steps - len(valid_current)
    zero_current = [0.0] * _features.N_CURRENT
    zero_traj = [0.0] * _features.N_TRAJECTORY

    return {
        "current": [float(v) for v in current_rows[-1]],
        "history_sequence": [[0.0] * _features.N_CURRENT for _ in range(pad)]
                            + [list(map(float, row)) for row in valid_current],
        "history_mask": [False] * pad + [True] * len(valid_current),
        "trajectory_sequence": [[0.0] * _features.N_TRAJECTORY for _ in range(pad)]
                               + [list(map(float, row)) for row in valid_traj],
        "trajectory_mask": [False] * pad + [True] * len(valid_traj),
        "radial_history": [[0.0] * N_RAYS for _ in range(pad)]
                          + [list(map(float, row)) for row in valid_radial],
        "radial_statistics_history": [[0.0] for _ in range(pad)]
                                     + [list(map(float, row)) for row in valid_stats],
        "radial_history_mask": [False] * pad + list(valid_radial_mask),
        "initial_winds": initial_winds,
        "storm_age_seconds": float(current_rows[-1][-2]),
        "valid_history_length": int(current_rows[-1][-1]),
        "n_valid_rows": len(valid_current),
    }


def track_tensors_for_cell(
    observations: list[dict],
    *,
    history_steps: int = HISTORY_STEPS,
    radial_by_row: list[dict] | None = None,
    gap_flag_minutes: float = DEFAULT_GAP_FLAG_MINUTES,
) -> dict:
    """One-call helper: commit rows for a cell id and derive model tensors."""
    committed = commit_track_rows(observations, history_steps=history_steps,
                                  gap_flag_minutes=gap_flag_minutes)
    rows = committed["rows"]
    if not rows:
        return {"rows": [], "meta": committed["meta"], "tensors": None,
                "status": "skipped:no-committed-rows"}
    tensors = build_model_inputs(rows, history_steps=history_steps,
                                 radial_by_row=radial_by_row)
    return {"rows": rows, "meta": committed["meta"], "tensors": tensors,
            "status": "ok"}


__all__ = [
    "HISTORY_STEPS",
    "LINEAGE_POLICY",
    "DEFAULT_GAP_FLAG_MINUTES",
    "centroid_displacement_m",
    "trajectory_vector_for_index",
    "build_trajectory_sequence",
    "commit_track_rows",
    "fork_track",
    "build_model_inputs",
    "track_tensors_for_cell",
]
