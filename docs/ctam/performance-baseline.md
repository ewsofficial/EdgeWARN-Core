# CTAM Phase 7 performance gate

The runner is measured with a one-cell synthetic cycle on the project CI host.
The gate is intentionally broad enough to tolerate normal host variation while
still catching accidental server/process startup on the StormProb-only path.

| Scenario | Latency budget | Peak extra RSS | File I/O budget |
| --- | ---: | ---: | ---: |
| CTAM disabled | no CTAM API server or child process | 0 MB attributable CTAM | 0 CTAM writes |
| StormProb-only / absent external root | 1 s | 64 MB | history and alert publication only |
| One SDK external module | 5 s | 128 MB | one staged transaction plus host publication |

The Phase 7 benchmark test records elapsed time for the first two paths and
asserts the budgets. The external-runner contract suite measures the third path
with the tracked synthetic module fixture. Re-baseline deliberately after a
measured architecture change; do not relax these limits for an isolated noisy
machine without recording the evidence.
