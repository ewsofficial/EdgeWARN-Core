# Production failure recovery

The fixes for audit #115 preserve source timestamps, binary payload formats,
readiness ordering, and atomic publication while bounding failure recovery.

## Service and worker recovery

The launcher restarts individual failed services with `runtime.supervisor`
backoff and restart limits, keeping siblings running. Exhausting the limit
enters a cooldown while siblings remain alive; if every service stops, the
launcher exits nonzero.
Accessory children enter a cooldown of `restart_window_seconds` after a crash
burst and are then eligible to restart. Shutdown stops all owned process groups.
Compose uses `restart: unless-stopped` and a read-only heartbeat health check.

NEXRAD serializes processing per site. Runtime cleanup yields to the site lock,
and parsers consume separate snapshots. A timed-out pool generation is retired
and terminated. Retried downloads truncate to their last committed byte count
before appending, and volume boundaries are accepted only at S-chunk headers.
Elevation AR2V outputs are atomic. Pool recycling drains outside the event loop
and outside the lifecycle lock.

## Feed and scientific quality

Cell matching of externally published official alerts rejects
snapshots older than `detection.alert_matching.snapshot_max_age_seconds` and
filters expired warnings, including cached features. Station-catalog downloads
reuse last-known-good radar stations after transient failures.

Kalman prediction applies eastward velocity to longitude and northward velocity
to latitude; acceleration and process-noise axes follow the same ordering.
Infeasible gated Hungarian matrices retain finite partial matches. Raster
processing exceptions produce `PROCESSING_ERROR`, not a numeric measurement.
Detection skips cells without finite radar centroids, and atomic JSON writers
reject nonfinite numbers before replacing an existing artifact. Tracking starts
fresh when its previous cycle exceeds inactive-cell retention.

## Storage and publication

Orphaned cell-history files participate in inactive-cell cleanup even after
SQLite has pruned their projections. Committed CTAM journals age out using the
inactive-cell retention interval. A corrupt journal is isolated; a transient
SQLite lock leaves prepared work available for later recovery. Per-cycle pruning
reuses freed SQLite pages; operators can call `StormProbRepository.vacuum()` for
explicit maintenance. Indexes are published once per cycle.

Container logs use a circular rotation budget from `runtime.logging`:
`retention_files` files of `max_file_mb` MiB each. The current-log hard link
identifies the active file. Configure the entire catalog and restart services
when changing these settings.

Ingest timer errors retry on the next scheduled poll. Deferred cleanup is
flushed by realtime maintenance. Core checks local readiness at the consumer
cadence, uses bounded cycle waits, and records transient cycle failures under
its retry policy. Pagination rejects a cursor whose item has been pruned with
400, allowing the client to restart the collection explicitly.

## Verification

Offline regressions inject worker death, hung renders, lock/read failures,
partial NEXRAD download failure, stale official-alert snapshots, station-feed failure,
and corrupt publication state. Scientific fixtures check direction, finite
coordinates, and per-lead forecast publication. Configuration is validated by
both runtimes; API contract and process/container tests accompany the changes.
