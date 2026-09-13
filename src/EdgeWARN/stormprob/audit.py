"""Read-only per-cycle feature coverage and source-freshness audit.

Run against a runtime database before promotion. A nonzero exit means that the
sample failed a gate; the JSON report remains suitable for retaining with the
deployment decision.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import struct

from .database import StormProbRepository
from .features import CURRENT_FEATURE_ORDER, MISSING_THRESHOLD, feature_order_checksum


def audit(repository: StormProbRepository, *, cycle_limit: int = 20,
          maximum_missing_fraction: float = 0.5,
          maximum_source_age_hours: float = 3.0) -> dict:
    if cycle_limit < 1 or not 0 <= maximum_missing_fraction <= 1:
        raise ValueError("invalid audit sample or missingness threshold")
    if not repository.path.is_file():
        return {"cycles": 0, "observations": 0, "passed": False,
                "failures": ["database-not-found"], "database": str(repository.path)}
    with repository.reader() as db:
        cycles = db.execute("SELECT cycle_id FROM cycles WHERE state='inputs-committed' "
                            "ORDER BY analysis_time DESC LIMIT ?", (cycle_limit,)).fetchall()
        if not cycles:
            return {"cycles": 0, "observations": 0, "passed": False,
                    "failures": ["no-committed-cycles"]}
        placeholders = ",".join("?" for _ in cycles)
        rows = db.execute(f"""SELECT o.analysis_time,o.inference_ready,
            f.values_f32,f.order_checksum,f.source_times_json
            FROM cell_observations o JOIN feature_values f
            USING(cell_id,analysis_time,feature_schema_version)
            WHERE o.cycle_id IN ({placeholders})""",
            [row[0] for row in cycles]).fetchall()
        forecasts = db.execute(f"""SELECT lead_minutes,status,metadata_json FROM forecasts
            WHERE cycle_id IN ({placeholders}) AND model_version='stormprob/v1'""",
            [row[0] for row in cycles]).fetchall()
    counts = Counter()
    values = {name: [] for name in CURRENT_FEATURE_ORDER}
    freshness = Counter()
    bad_order = 0
    for row in rows:
        if row["order_checksum"] != feature_order_checksum():
            bad_order += 1
            continue
        vector = struct.unpack("<135f", row["values_f32"])
        for name, value in zip(CURRENT_FEATURE_ORDER, vector):
            if value <= MISSING_THRESHOLD:
                counts[name] += 1
            else:
                values[name].append(float(value))
        analysis = datetime.fromisoformat(row["analysis_time"])
        for family, source in json.loads(row["source_times_json"]).items():
            if source is None:
                freshness[f"absent:{family}"] += 1
                continue
            try:
                source_time = datetime.fromisoformat(str(source).replace("Z", "+00:00"))
            except ValueError:
                freshness[f"corrupt:{family}"] += 1
                continue
            if source_time.tzinfo is None:
                source_time = source_time.replace(tzinfo=timezone.utc)
            age_hours = (analysis - source_time).total_seconds() / 3600
            if age_hours < -120 / 3600:
                freshness[f"future:{family}"] += 1
            elif age_hours > maximum_source_age_hours:
                freshness[f"stale:{family}"] += 1
    total = len(rows)
    forecast_status = Counter(f"{row['lead_minutes']}:{row['status']}" for row in forecasts)
    durations = [float(json.loads(row["metadata_json"])["inference_duration_ms"])
                 for row in forecasts if row["lead_minutes"] == 15
                 and "inference_duration_ms" in json.loads(row["metadata_json"])]
    missing = {name: round(counts[name] / total, 4) for name in CURRENT_FEATURE_ORDER} if total else {}
    failures = (["no-observations"] if not total else [])
    if bad_order:
        failures.append(f"feature-order-mismatch:{bad_order}")
    if not any(row["status"] == "ok" for row in forecasts):
        failures.append("no-successful-forecasts")
    failures.extend(f"systematic-missing:{name}" for name, rate in missing.items()
                    if rate > maximum_missing_fraction)
    failures.extend(f"source-{issue}" for issue in sorted(freshness)
                    if issue.startswith(("future:", "stale:", "corrupt:")))
    return {"cycles": len(cycles), "observations": total,
            "inference_ready": sum(bool(row["inference_ready"]) for row in rows),
            "feature_order_checksum": feature_order_checksum(),
            "missing_fraction": missing,
            "distribution": {name: {"count": len(sample),
                                     "median": statistics.median(sample) if sample else None,
                                     "minimum": min(sample) if sample else None,
                                     "maximum": max(sample) if sample else None}
                             for name, sample in values.items()},
            "source_issues": dict(freshness), "passed": not failures,
            "forecast_status": dict(forecast_status),
            "inference_duration_ms": {"count": len(durations),
                                      "maximum": max(durations) if durations else None,
                                      "median": statistics.median(durations) if durations else None},
            "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--maximum-missing-fraction", type=float, default=0.5)
    args = parser.parse_args()
    report = audit(StormProbRepository(args.base_dir), cycle_limit=args.cycles,
                   maximum_missing_fraction=args.maximum_missing_fraction)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
