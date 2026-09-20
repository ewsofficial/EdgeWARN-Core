# Fix plan: eliminate the post-CTAM API-index rebuild stall

## Problem

The primary EdgeWARN worker stalls after CTAM while publishing StormProb data and
rebuilding API indexes. The dominant cost is an unindexed SQLite query executed
twice per cycle:

```sql
SELECT o.cell_id, MAX(c.committed_at)
FROM cell_observations o
JOIN cycles c ON c.cycle_id = o.cycle_id
WHERE c.state = 'inputs-committed'
  AND c.projection_state = 'published'
GROUP BY o.cell_id;
```

For the current database (`12,379` `cell_observations` rows and `55` published
cycles), one scan takes approximately `14.1 s`. The duplicate scans add about
`28 s` to every cycle. Together with the approximately `18 s` publication
transaction, this produces the observed `44–51 s` post-CTAM stall and can make
the 30-second API heartbeat appear stale.

The duplicate calls are created by `_update_api_indexes()` constructing a new
`APIIndexManager` and then invoking both paths below:

1. `update_stormcell_index()` → `_initialize_stormcell_index()` → `index_projection()`
2. `update_cell_index()` → `_initial_scan_cell_index()` → `index_projection()`

Relevant locations:

- `src/EdgeWARN/api_integration/index_manager.py:80`
- `src/EdgeWARN/stormprob/database.py:525`
- `src/EdgeWARN/process/integrate/pipeline.py:562`

## Plan

### 1. Add supporting indexes through the StormProb schema migration

Add idempotent migration statements for:

```sql
CREATE INDEX IF NOT EXISTS cycles_published
ON cycles(state, projection_state, cycle_id, committed_at);

CREATE INDEX IF NOT EXISTS observations_cycle_cell
ON cell_observations(cycle_id, cell_id);
```

Ensure the migration runs for existing databases as well as newly created
databases, and add a schema-version/migration test.

### 2. Rewrite the projection query for the published-cycle access path

Update `index_projection()` so SQLite starts with the subset of published,
inputs-committed cycles and then joins observations through
`observations_cycle_cell`. Preserve the existing result semantics, including
the latest `committed_at` value per `cell_id`.

Use `EXPLAIN QUERY PLAN` in a regression test to verify that the new indexes are
used and that SQLite no longer scans the full observation history for each
projection rebuild.

### 3. Reuse one projection result per cycle

Refactor `_update_api_indexes()` and the `APIIndexManager` initialization flow
so both `update_stormcell_index()` and `update_cell_index()` consume the same
`index_projection()` result. Do not create a second manager or repeat the
initial scan during the same cycle.

Keep the shared result scoped to one update operation and invalidate it when a
new publication changes the underlying cycle/observation data.

### 4. Preserve publication and readiness ordering

Keep the publication transaction and API-index update atomic from the
application’s perspective:

- Do not mark a cycle published before database and filesystem updates commit.
- Do not expose partially rebuilt indexes.
- Ensure a failed index update is retried or reported without losing the
  committed forecast.
- Keep only one publication/index update active at a time.

Do not move work to a background worker until the synchronous query and duplicate
scan costs are fixed; the primary fix should remove the avoidable stall first.

### 5. Add phase-level telemetry

Log separate durations for:

- ingest
- detection
- rasterization
- CTAM
- SQLite publication transaction
- API index query/rebuild
- filesystem publication
- backup/cleanup

Include the cycle ID, row counts, whether the projection result was reused, and
the selected SQLite indexes in diagnostic mode.

## Validation

1. Run the migration against a copy of the current database and verify it is
   idempotent.
2. Compare the projection query before and after the migration using the same
   database snapshot.
3. Confirm the query drops from approximately `14.1 s` to the observed
   `~0.012 s` range, subject to hardware and cache state.
4. Verify one `index_projection()` call per `_update_api_indexes()` invocation.
5. Exercise empty, partial, failed, and restarted publication cases.
6. Run multiple cycles and confirm post-CTAM time stays below the 30-second
   heartbeat freshness threshold.
7. Confirm no forecast leads are skipped and no published cycle exposes stale or
   partially rebuilt API indexes.

## Acceptance criteria

- The two supporting indexes are installed by the StormProb migration.
- The projection query uses the published-cycle and observation indexes.
- `index_projection()` executes once per API-index update.
- A representative projection scan completes in milliseconds rather than ~14 s.
- Post-CTAM API-index rebuilding no longer contributes approximately 28 s per
  cycle.
- The primary worker remains heartbeat-fresh during publication.
- SQLite contention does not cause forecast failures or skipped leads.
- Interrupted jobs recover safely after restart.
- Cycle logs separately report all phase durations listed above.
