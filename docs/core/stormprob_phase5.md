# StormProb Phase 5 rollout and validation

StormProb is the sole production forecast engine. Each inference-ready cell
publishes four lead records (15, 30, 45, and 60 minutes) to the committed
SQLite forecast store and the public `modules.StormProb` projection. Tracking
uses the 15-minute east/north displacement as its initial control velocity;
alerts use the separately labeled 0–30-minute swept envelope.

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

Local verification on 2026-09-13: the focused StormProb, CTAM, integration, and
tracking suite passed in the installed `EdgeWARN` Python 3.13 environment.
The packaged ONNX graphs and calibrator are deployment assets under
`models/stormprob/`; no source checkout is required at runtime.
