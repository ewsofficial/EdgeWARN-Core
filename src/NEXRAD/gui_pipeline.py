"""NEXRAD-owned GUI rendering, discovery, and retention."""

from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
from pathlib import Path

import util.file as fs
from common.ingest.nexrad.s3_chunks import format_nexrad_timestamp, parse_nexrad_timestamp
from util.io import IOManager

from NEXRAD.config import (
    gui_cleanup_max_age_minutes,
    nexrad_poll_interval_min_seconds,
    nexrad_poll_interval_seconds,
    nexrad_render_max_workers,
    nexrad_source_max_age_minutes,
)

io_manager = IOManager("[NEXRAD-GUI]")

_NEXRAD_SITE_DIR_PATTERN = re.compile(r"^[A-Z0-9]{4}$")
_NEXRAD_ELEVATION_DIR_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{1,2})?$")
_NEXRAD_TIMESTAMP_PATTERN = re.compile(r"^\d{8}-\d{6}$")
_inflight_renders: set[str] = set()
_inflight_lock = threading.Lock()


def cleanup_old_nexrad_gui_files(max_age_minutes: int | None = None) -> int:
    """Remove stale NEXRAD GUI files and empty site/elevation directories."""

    if max_age_minutes is None:
        max_age_minutes = gui_cleanup_max_age_minutes()

    nexrad_root = Path(fs.GUI_NEXRAD_DIR)
    if not nexrad_root.exists():
        return 0

    now = time.time()
    max_age_seconds = max_age_minutes * 60
    total_removed = 0
    affected_sites: set[str] = set()
    files_removed = 0
    dirs_removed = 0

    for site_dir in nexrad_root.iterdir():
        if not site_dir.is_dir() or site_dir.name.startswith(".") or not _NEXRAD_SITE_DIR_PATTERN.fullmatch(site_dir.name):
            continue

        site_had_removals = False

        for child_dir in site_dir.iterdir():
            if not child_dir.is_dir() or child_dir.name.startswith("."):
                continue
            if child_dir.name != "render" and not _NEXRAD_ELEVATION_DIR_PATTERN.fullmatch(child_dir.name):
                continue

            try:
                for child_file in child_dir.iterdir():
                    if not child_file.is_file() or child_file.name.startswith("."):
                        continue
                    file_age = now - child_file.stat().st_mtime
                    if file_age <= max_age_seconds:
                        continue
                    child_file.unlink(missing_ok=True)
                    files_removed += 1
                    total_removed += 1
                    site_had_removals = True

                if not any(child_dir.iterdir()):
                    child_dir.rmdir()
                    dirs_removed += 1
                    total_removed += 1
                    site_had_removals = True
            except Exception as exc:
                io_manager.write_warning(f"Failed to process NEXRAD directory {child_dir}: {exc}")

        try:
            if any(site_dir.iterdir()):
                if site_had_removals:
                    affected_sites.add(site_dir.name)
                continue

            site_dir.rmdir()
            dirs_removed += 1
            total_removed += 1
            affected_sites.add(site_dir.name)
        except Exception as exc:
            io_manager.write_warning(f"Failed to process NEXRAD site folder {site_dir}: {exc}")

    if files_removed or dirs_removed:
        sites_str = ", ".join(sorted(affected_sites))
        io_manager.write_debug(
            f"Cleaned up NEXRAD GUI: {files_removed} file(s), {dirs_removed} dir(s) "
            f"across {len(affected_sites)} site(s): [{sites_str}]"
        )

    return total_removed


def _load_nexrad_artifact_metadata(artifact_path: Path) -> dict | None:
    sidecar_payload = {}
    sidecar_path = artifact_path.with_suffix(".json")
    if sidecar_path.exists():
        try:
            loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                sidecar_payload = loaded
        except Exception:
            sidecar_payload = {}

    stem_prefix = f"{artifact_path.parent.parent.name}_{artifact_path.parent.name}_"
    filename_timestamp = artifact_path.stem[len(stem_prefix):] if artifact_path.stem.startswith(stem_prefix) else None
    normalized_filename_timestamp = _normalize_nexrad_timestamp(filename_timestamp)
    elevation_timestamp = _normalize_nexrad_timestamp(sidecar_payload.get("elevation_timestamp")) or normalized_filename_timestamp
    scan_timestamp = _normalize_nexrad_timestamp(sidecar_payload.get("scan_timestamp")) or elevation_timestamp
    if elevation_timestamp is None:
        return None

    return {
        "site": artifact_path.parent.parent.name,
        "elevation": artifact_path.parent.name,
        "scan_timestamp": scan_timestamp,
        "elevation_timestamp": elevation_timestamp,
        "volume_id": str(sidecar_payload.get("volume_id") or artifact_path.stem),
        "member_group_names": list(sidecar_payload.get("member_group_names") or []),
        "member_sweeps": list(sidecar_payload.get("member_sweeps") or []),
        "artifact_path": artifact_path,
    }


def _normalize_nexrad_timestamp(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _NEXRAD_TIMESTAMP_PATTERN.fullmatch(text):
        return text
    return format_nexrad_timestamp(parse_nexrad_timestamp(text))


def _nexrad_source_artifact_is_fresh(artifact_path: Path, *, now: float, max_age_minutes: int) -> bool:
    try:
        return (now - artifact_path.stat().st_mtime) <= max_age_minutes * 60
    except OSError:
        return False


def _iter_latest_nexrad_artifacts():
    nexrad_root = Path(fs.NEXRAD_LEVEL2_DIR)
    if not nexrad_root.exists():
        return

    for site_dir in sorted(nexrad_root.iterdir(), key=lambda path: path.name):
        if (
            not site_dir.is_dir()
            or site_dir.name.startswith(".")
            or not _NEXRAD_SITE_DIR_PATTERN.fullmatch(site_dir.name)
        ):
            continue

        for elevation_dir in sorted(site_dir.iterdir(), key=lambda path: path.name):
            if (
                not elevation_dir.is_dir()
                or elevation_dir.name.startswith(".")
                or not _NEXRAD_ELEVATION_DIR_PATTERN.fullmatch(elevation_dir.name)
            ):
                continue

            preferred_by_stem: dict[str, Path] = {}
            for artifact_path in sorted(elevation_dir.iterdir(), key=lambda path: path.name):
                if not artifact_path.is_file() or artifact_path.suffix not in {".nc", ".ar2v"}:
                    continue
                if ".part" in artifact_path.name.lower():
                    continue
                existing = preferred_by_stem.get(artifact_path.stem)
                if existing is None or (existing.suffix != ".nc" and artifact_path.suffix == ".nc"):
                    preferred_by_stem[artifact_path.stem] = artifact_path

            for artifact_path in sorted(preferred_by_stem.values(), key=lambda path: path.name):
                metadata = _load_nexrad_artifact_metadata(artifact_path)
                if metadata is not None:
                    yield metadata


def _nexrad_gui_timestamp_exists(site: str, elevation: str, timestamp: str) -> bool:
    from NEXRAD.render import nexrad_render_elevation_dir

    elevation_dir = nexrad_render_elevation_dir(site, elevation)
    if not elevation_dir.exists():
        return False
    pattern = f"{str(site).upper()}_*_{elevation}_{timestamp}.bin.gz"
    return any(elevation_dir.glob(pattern))


def _render_pending_nexrad_gui_artifact(metadata: dict) -> bool:
    from NEXRAD.render import serialize_nexrad_elevation_artifacts
    from common.ingest.nexrad.models import ElevationArtifact

    site = str(metadata["site"]).upper()
    elevation = str(metadata["elevation"])
    timestamp = str(metadata["elevation_timestamp"])
    artifact_path = Path(metadata["artifact_path"])

    # The sidecar is atomically written only after the source artifact is
    # complete. Do not begin decompression until that commit marker is valid.
    sidecar_path = artifact_path.with_suffix(".json")
    try:
        sidecar_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(sidecar_payload, dict):
        return False

    artifact = ElevationArtifact(
        site=site,
        volume_id=str(metadata["volume_id"]),
        volume_timestamp=str(metadata["scan_timestamp"]),
        scan_timestamp=str(metadata["scan_timestamp"]),
        elevation=elevation,
        elevation_timestamp=timestamp,
        first_sweep_index=0,
        last_sweep_index=0,
        first_sweep_timestamp=None,
        last_sweep_timestamp=None,
        member_group_names=list(metadata.get("member_group_names") or []),
        member_sweeps=list(metadata.get("member_sweeps") or []),
        waveforms_present=set(),
        supplemental=False,
        netcdf_path=str(artifact_path) if artifact_path.suffix == ".nc" else None,
        ar2v_path=str(artifact_path) if artifact_path.suffix == ".ar2v" else None,
    )
    manifest_path = serialize_nexrad_elevation_artifacts(
        site, str(metadata["volume_id"]), str(metadata["scan_timestamp"]), [artifact]
    )
    if manifest_path is not None and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            layers = manifest.get("layers") or []
            if layers:
                for layer in layers:
                    bin_path = layer.get("bin_path")
                    if bin_path is None or not Path(bin_path).exists():
                        return False
                return True
        except Exception:
            pass
    return _nexrad_gui_timestamp_exists(site, elevation, timestamp)


def render_pending_nexrad_gui_files(*, base_dir=None, max_source_age_minutes: int | None = None) -> int:
    if max_source_age_minutes is None:
        max_source_age_minutes = nexrad_source_max_age_minutes()

    if base_dir:
        fs.initialize_filesystem(base_dir)

    now = time.time()
    pending_metadata = []
    for metadata in _iter_latest_nexrad_artifacts() or ():
        site = str(metadata["site"]).upper()
        elevation = str(metadata["elevation"])
        timestamp = str(metadata["elevation_timestamp"])
        artifact_path = Path(metadata["artifact_path"])
        if not _nexrad_source_artifact_is_fresh(artifact_path, now=now, max_age_minutes=max_source_age_minutes):
            continue
        if _nexrad_gui_timestamp_exists(site, elevation, timestamp):
            continue
        render_key = f"{site}/{elevation}/{timestamp}"
        with _inflight_lock:
            if render_key in _inflight_renders:
                continue
            _inflight_renders.add(render_key)
        pending_metadata.append(metadata)

    if not pending_metadata:
        return 0

    max_workers = min(nexrad_render_max_workers(), len(pending_metadata))
    rendered_count = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_render_pending_nexrad_gui_artifact, metadata) for metadata in pending_metadata]
            for future in concurrent.futures.as_completed(futures):
                if future.result():
                    rendered_count += 1
    finally:
        with _inflight_lock:
            for metadata in pending_metadata:
                render_key = (
                    f"{str(metadata['site']).upper()}"
                    f"/{metadata['elevation']}"
                    f"/{metadata['elevation_timestamp']}"
                )
                _inflight_renders.discard(render_key)

    return rendered_count


def run_nexrad_render_loop(
    *,
    base_dir=None,
    poll_interval_seconds: float | None = None,
    wait_for_quiescence=None,
) -> None:
    if poll_interval_seconds is None:
        poll_interval_seconds = nexrad_poll_interval_seconds()
    poll_interval_seconds = max(nexrad_poll_interval_min_seconds(), float(poll_interval_seconds))
    while True:
        try:
            # Optional cross-service throttle (decomposition Phase 3): checked
            # before admitting a new render batch; a batch in progress is
            # never interrupted.
            if wait_for_quiescence is not None:
                wait_for_quiescence()
            rendered = render_pending_nexrad_gui_files(base_dir=base_dir)
            if rendered > 0:
                io_manager.write_info(f"NEXRAD render poll cycle wrote {rendered} artifact(s)")
        except KeyboardInterrupt:
            return
        except Exception as exc:
            io_manager.write_warning(f"NEXRAD render poll cycle failed: {exc}")
        try:
            # NEXRAD owns retention beneath gui/NEXRAD; sweep each poll cycle.
            removed = cleanup_old_nexrad_gui_files()
            if removed > 0:
                io_manager.write_debug(f"NEXRAD retention removed {removed} stale file(s)/dir(s)")
        except Exception as exc:
            io_manager.write_warning(f"NEXRAD retention sweep failed: {exc}")
        time.sleep(poll_interval_seconds)
