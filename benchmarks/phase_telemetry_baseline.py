"""Independent-process performance baseline harness (decomposition Phase 0).

Parses ``[PhaseTelemetry]`` producer-time phase lines emitted by
``util/runtime/cycle.py`` and aggregates them into a per-phase latency
baseline, so the all-services launcher experiment
(plans/realtime-runner-decomposition-plan.md, "All-services launcher
performance gate") can be compared against the monolithic runner on identical
instruments.

Measurement procedure
---------------------

1. Start the runner exactly as production does, from its own shell or service
   unit -- never through a parent that pipes stdout, so timestamps stay
   producer-time.
2. Capture at least ten warm MRMS cycles of stdout to a file::

       cd src && python run.py --lat_limits 20 55 --lon_limits 230 300 \
           > /tmp/opencode/run_baseline.log 2>&1

3. Build the baseline::

       python benchmarks/phase_telemetry_baseline.py \
           /tmp/opencode/run_baseline.log \
           --label independent-monolith \
           --out /tmp/opencode/baseline.json

4. After the decomposition, repeat the capture against each service log and
   pass multiple logs to this script; phases keep their names, so aggregates
   remain comparable.

The numbers recorded here are host- and load-dependent: baselines are only
meaningful when both modes are measured on the same host, configuration, input
window, warm caches, and base-directory storage class, with mode order
alternated to reduce bias.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

TELEMETRY_RE = re.compile(
    r"\[PhaseTelemetry\]\s+"
    r"utc=(?P<utc>\S+)\s+"
    r"monotonic=(?P<monotonic>-?\d+(?:\.\d+)?)\s+"
    r"cycle=(?P<cycle>\S+)\s+"
    r"phase=(?P<phase>\S+)\s+"
    r"status=(?P<status>\S+)"
)


def parse_events(lines):
    """Yield one event dict per well-formed telemetry line."""
    for line in lines:
        match = TELEMETRY_RE.search(line)
        if match:
            yield {
                **match.groupdict(),
                "monotonic": float(match.group("monotonic")),
            }


def collect_cycles(events):
    """Group events by cycle id and normalize each to cycle-relative offsets."""
    cycles: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        cycles[event["cycle"]].append(event)

    normalized = []
    for cycle_id in sorted(cycles):
        entries = sorted(cycles[cycle_id], key=lambda e: e["monotonic"])
        origin = entries[0]["monotonic"]
        for entry in entries:
            normalized.append({
                "cycle": cycle_id,
                "phase": entry["phase"],
                "status": entry["status"],
                "utc": entry["utc"],
                "offset_seconds": entry["monotonic"] - origin,
            })
    return normalized


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[index]


def aggregate(cycle_events):
    """Reduce normalized events to per-phase offset statistics."""
    by_phase: dict[str, list[float]] = defaultdict(list)
    statuses: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in cycle_events:
        by_phase[event["phase"]].append(event["offset_seconds"])
        statuses[event["phase"]][event["status"]] += 1

    summary = {}
    for phase, offsets in sorted(by_phase.items()):
        ordered = sorted(offsets)
        summary[phase] = {
            "samples": len(ordered),
            "p50_offset_seconds": round(statistics.median(ordered), 6),
            "p95_offset_seconds": round(_percentile(ordered, 0.95), 6),
            "max_offset_seconds": round(ordered[-1], 6),
            "statuses": dict(statuses[phase]),
        }
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", type=Path, help="Captured service log file(s)")
    parser.add_argument("--label", default="unlabeled", help="Baseline label stored in the output")
    parser.add_argument("--out", type=Path, default=None, help="Write the JSON baseline here")
    args = parser.parse_args(argv)

    events = []
    total_lines = 0
    for log_path in args.logs:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        total_lines += len(lines)
        events.extend(parse_events(lines))

    if not events:
        print("No [PhaseTelemetry] lines found; nothing to record.", file=sys.stderr)
        return 1

    cycle_events = collect_cycles(events)
    cycles_seen = len({event["cycle"] for event in cycle_events})
    report = {
        "label": args.label,
        "logs": [str(path) for path in args.logs],
        "lines_scanned": total_lines,
        "cycles_observed": cycles_seen,
        "phases": aggregate(cycle_events),
    }

    rendered = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
        print(f"Wrote baseline for {cycles_seen} cycle(s) to {args.out}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
