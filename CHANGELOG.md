# Changelog

## [3.0.2] 2026-09-28

### Added
- Added `api_index.stormprob_inactive_cell_max_age_minutes` (default 20) to the
  configuration catalog and schema. It controls how long StormProb history is
  retained for inactive cells, independently of the 120-minute compatibility
  cell-file retention.
- Added `StormProbRepository.prune_inactive_cells()` and `vacuum()` to remove
  inactive cells and reclaim disk space.

### Changed
- Updated EdgeWARN package, API, deployment, and documentation version metadata
  to 3.0.2.
- Realtime publication now prunes a cell's StormProb forecasts and observations
  once its newest observation is older than the StormProb retention window.
  Feature values and radial profiles cascade with their observation, and
  historical runs do not prune.
- After pruning, the StormProb database is vacuumed so the file shrinks on disk
  instead of only freeing pages for reuse.
- API indexes are refreshed after StormProb data is pruned.

### Removed
- Removed StormProb SQLite backup code: the daily online backup after
  publication, the pre-import backup in the legacy migration tool, and backup
  retention helpers. The `backup_seconds` field is no longer logged in
  publication phase timings.

### Fixed
- Pruned cell IDs are scrubbed from stored StormProb cycle projections, and cycle
  rows no longer referenced by any retained observation or forecast are deleted.
  Cycles shared with retained cells are kept.

### Testing
- Updated StormProb database tests to cover pruning and the removal of backups.
