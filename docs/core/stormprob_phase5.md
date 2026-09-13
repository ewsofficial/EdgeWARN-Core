# StormProb Phase 5 rollout and validation

StormProb runs in `shadow` mode by default. It records four model statuses or
forecasts per cell in the SQLite database, but does not publish StormProb alerts,
put StormProb fields in public storm-cell JSON, or use forecast velocity for
tracking. Set `STORMPROB_MODE=promoted` only after the gates below pass. The
setting is read by CTAM, publication, and both tracker control paths. To roll
back public behavior, set `STORMPROB_MODE=rollback`: the preserved StormCast
adapter again produces its original public projection, alerts, and tracking
velocity. This mode does not run StormProb inference or alter its model assets;
the input database continues collecting cycle observations. Return to `shadow`
for further validation. Preserve old JSON during the migration window.

Run the read-only audit against the actual configured runtime base directory:

```bash
conda run -n EdgeWARN python -m EdgeWARN.stormprob.audit --base-dir /path/to/runtime --cycles 20
```

Run the command from `src`, or set `PYTHONPATH=src` from the repository root.
Save its JSON output for review. It reports the missing fraction and observed
min/median/max of all 135 ordered channels, source timestamps, forecast status
by lead, and measured per-cell inference duration. It fails when there are no
committed cycles, no successful forecasts, a feature-order mismatch, more than
50% missing observations for a channel, or future/stale source timestamps.
The 50% and three-hour audit thresholds are screening defaults, not a claim of
training parity: compare each channel with StormProb's dataset/cache builder
before promotion, especially RAP winds, Ref10/Ref20, and SRH02km. Review both
current and historical samples; a historical source timestamp must never be
after its analysis time.

The scorecard gate requires held-out paired fixtures from the StormProb Python
evaluation path. Compare all four leads' Fourier parameters and motion
residuals, then the calibrated 0.25 occupancy masks and centroid displacement.
Measure CSI, POD, FAR, 5/10/20 km FSS, and reliability against the checked-in
v7 scorecard's 20-member, 1 km, ±100 km grid. Document any float32 or raster
resolution differences. Do not infer those metrics from a model smoke test.
Measure the full per-cell postprocess time and total realtime cycle latency on
the service host, including SQLite and CTAM, before promotion.

Local verification on 2026-09-13: 532 targeted Python tests passed (four
skipped), as did all 71 Node tests including the new StormProb Supertest
contract. The full Python suite was started but
did not finish during this pass; its buffered runner was interrupted after
several minutes without output. The packaged ONNX graphs loaded and produced finite
four-lead outputs in `EdgeWARN` after installing `onnxruntime==1.30.0` (one
synthetic single-history-row pair took 2.22 ms for the graphs alone). The
documented `EdgeWARN-dev` environment does not exist; the installed `EdgeWARN`
environment has Python 3.13, the scientific libraries, and the pinned ONNX
Runtime dependency.
The parity fixtures and StormProb dataset cache named in the plan are absent
from this checkout, and no representative StormProb SQLite cycle database is
present under the local runtime base directory. These are deployment blockers,
so the output must remain in shadow mode until the gates are measured.
