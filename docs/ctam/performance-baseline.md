# CTAM Phase 7 performance gate

The benchmark is run locally with synthetic cells; it is not included in the
default CI pytest paths. The gate is intentionally broad enough to tolerate
normal host variation, but the current tests do not independently detect
server/process startup.

| Scenario | Latency budget | Peak traced Python memory | Exercised side effects |
| --- | ---: | ---: | ---: |
| CTAM disabled | no CTAM call | not measured | no CTAM writes |
| StormProb-only | 1 s | not measured | alert cleanup/publication side effects only |
| One inline external module | 5 s | less than 128 MB `tracemalloc` peak | one in-memory transaction; host publication is not exercised |

`benchmarks/test_ctam_phase7.py` records elapsed time for the StormProb-only and
external-module paths. The disabled path checks the disabled return contract.
The external test creates an inline synthetic module under `tmp_path`; it does
not use the tracked fixture or the SDK. The memory assertion uses
`tracemalloc`, not process RSS. Re-baseline deliberately after a measured
architecture change and record the evidence.
