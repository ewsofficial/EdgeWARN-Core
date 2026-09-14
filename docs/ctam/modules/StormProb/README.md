# StormProb built-in

StormProb is the reserved, host-owned CTAM forecast engine. It reads committed
feature windows from `<BASE_DIR>/data/stormprob/stormprob.sqlite3`, runs the
packaged radial and motion ONNX graphs, and writes four versioned operational
forecast envelopes for 15, 30, 45, and 60 minutes.

Each cell result is published under `modules.StormProb` with only the operational
status, analysis time, and four lead records. Each lead contains its lead and
valid times, status, and—when successful—centroid displacement, predicted
centroid, and a compact polygon spanning the original and predicted areas. The
polygon adaptively has 4–12 points, adding points when needed to fit the shape,
and has a 1 km metric buffer. Detailed probability heatmaps
use the full calibrated raster path asynchronously and do not block operational
publication; the operational polygon itself uses the same calibrator's 0.25
threshold to select its ensemble radial boundary.
Non-successful leads contain a machine-readable reason and geometry is never
fabricated.
Model version, checkpoint IDs, threshold, postprocessing version, and timing are
retained in the SQLite forecast record and audit logs.

The existing alert product is separate from those operational envelopes: its
0–30-minute swept envelope is built from the current detection and the 15/30
minute forecasts and is labeled `swept-envelope-0-30min`.

StormProb cannot be installed, shadowed, disabled independently, or replaced by
an external CTAM module. External modules may declare `after = ["stormprob"]`
when they consume its committed/public output.
