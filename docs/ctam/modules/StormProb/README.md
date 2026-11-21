# StormProb built-in

StormProb is the reserved, host-owned CTAM forecast engine. It reads committed
feature windows from `<BASE_DIR>/data/stormprob/stormprob.sqlite3`, runs the
packaged radial and motion ONNX graphs, and writes four versioned operational
forecast envelopes for 15, 30, 45, and 60 minutes.

The radial history uses each detection's original ProbSevere polygon and a
reflectivity-weighted centroid calculated only from gates in that polygon when
full-precision geometry is available. Legacy or fallback records can use
rounded centroid/bbox geometry and remain inference-ready.
The operational envelope starts from the same committed polygon and centroid.
Expanded detection footprints remain available to other EdgeWARN processing.
A predicted-only track without a current ProbSevere observation is not
inference-ready.
The radial graph predicts Fourier shape coefficients and a Gaussian centroid
correction per lead; the motion graph predicts a residual to the initial wind.
Inference draws 20 joint members (fixed seed 42). Each member pairs an
independent shape draw with an independent centroid draw:
`centre = (initial_wind + residual) * lead + offset + L @ z`, where `L` is the
predicted Cholesky factor. Sampled radii use the joint model's 0.1 km floor.
Each member is rasterized about its own centre on a 201×201, 1 km grid, and
the probability is the raw member fraction `k/20`. No calibration is applied.

Each cell result is published under `modules.StormProb` with only the operational
status, analysis time, and four lead records. Each lead contains its lead and
valid times, status, and—when successful—centroid displacement, predicted
centroid (the centroid mean), and a compact polygon spanning the original and
predicted areas. Lead metadata carries `centroid_std_km`, the east/north
marginal standard deviations of the centroid distribution. The predicted area
is the largest component of the ≥0.25 occupancy contour (5 of 20 members). The
polygon adaptively has 4–12 points, adding points when needed to fit the shape,
and has a 1 km metric buffer. The current adapter does not persist detailed
probability heatmaps or run a separate asynchronous heatmap workflow.
A lead whose 0.25 contour is empty is recorded as `no-polygon` with reason
`empty-0.25-contour`; it keeps the centroid but has no polygon.
Handled non-successful leads contain a machine-readable reason and geometry is
not fabricated. A batch-level adapter exception can instead leave the `leads`
array empty, so the four-lead guarantee applies only to handled per-cell paths.
Forecast status, metadata, and timing are retained in SQLite and the audit
report. Model version, checkpoint IDs, threshold, and postprocessing version are
not currently all exposed by the audit log.

The existing alert product is separate from those operational envelopes: its
0–30-minute swept envelope is built from the current detection and the 15/30
minute forecasts and is labeled `swept-envelope-0-30min`.

StormProb cannot be installed, shadowed, disabled independently, or replaced by
an external CTAM module. External modules may declare `after = ["stormprob"]`
when they consume its committed/public output.
