# Changelog

## [3.0.1] 2026-09-25

### Added
- Published storm-cell snapshot files now include a top-level `modified`
  timestamp in UTC ISO 8601 format (with a `Z` suffix). It records when the
  snapshot was generated and is refreshed when a cycle publishes the snapshot,
  alongside the existing `latest_timestamp` field for the latest storm-cell
  data time.

### Changed
- Reduced API record publication work by reusing indexed StormProb database
  projections and avoiding redundant CTAM journal writes.
- Excluded RALA from MRMS scan-selection readiness to reduce cycle-selection
  latency.

### Removed

### Fixed
- Corrected MRMS cycle bucketing so timestamps with seconds over 30 round to
  the next minute rather than skipping two minutes.

### Testing
