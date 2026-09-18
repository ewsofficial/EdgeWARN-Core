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
Sampled radial boundaries use the v7 training and evaluation floor of 0.1 km
before calibration, occupancy rasterization, or operational geometry.

Each cell result is published under `modules.StormProb` with only the operational
status, analysis time, and four lead records. Each lead contains its lead and
valid times, status, and—when successful—centroid displacement, predicted
centroid, and a compact polygon spanning the original and predicted areas. The
polygon adaptively has 4–12 points, adding points when needed to fit the shape,
and has a 1 km metric buffer. The operational polygon uses the calibrator's
0.25 threshold to select its ensemble radial boundary. The current adapter does
not persist detailed probability heatmaps or run a separate asynchronous
heatmap workflow.
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
