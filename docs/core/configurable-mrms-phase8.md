# MRMS coordinated release: migration and qualification

The shipped `ingest.yaml` now uses schema version 2. Its 18 additions plus the
three immutable protected products preserve all 21 upstream identities. Only
protected products participate in discovery and mandatory MRMS readiness.
Integration and EWMRS references use full upstream identities; GUI paths and
binary payloads and API v3 product identities do not change.

## Upgrade and rollback

Use the complete release checkout with `npm install` and the EdgeWARN Conda
environment. For a wheel deployment, Node validator assets and package manifests
are installed beneath `<python-prefix>/share/edgewarn`; install their dependencies
with `npm ci --omit=dev --prefix <python-prefix>/share/edgewarn`. Copy/deploy the entire configuration tree. Stop Core, EWMRS,
NEXRAD, and the API; drain the EWMRS consumer backlog. Migration refuses live
service locks, pending cycles, active pins, source/target conflicts, unsafe
symlinks, cross-device moves, and custom mappings it cannot prove safe. A phase
that was never published does not create a consumer backlog.

```bash
edgewarn migrate-mrms --config-path /etc/edgewarn/config --base-dir /runtime
edgewarn migrate-mrms --config-path /etc/edgewarn/config --base-dir /runtime --apply
# After interruption, with services still stopped:
edgewarn migrate-mrms --config-path /etc/edgewarn/config --base-dir /runtime --resume
# Restore original configuration and directory names:
edgewarn migrate-mrms --config-path /etc/edgewarn/config --base-dir /runtime --rollback
```

The dry run performs no writes. Apply stores the original configuration bytes
and per-step progress in `<config-root>/.mrms-migration/journal.json`, writes config
atomically, and records each raw directory rename for resume and inverse rollback.
Unchanged catalog documents retain their original bytes. Resume rechecks completed
steps for conflicts, and an interrupted rollback must continue with `--rollback`.
Preserve this journal until the upgrade is accepted. Committed cycle records and
unrelated data directories remain intact. Final completion requires both Python
and Node validation. Missing Node or its dependencies leaves the journal applying;
restore the packaged validator dependencies or complete release checkout and run
`--resume`. The packaged validator does not start or install the Node API service.

Python workers and the Node API refuse startup when this journal is incomplete
or corrupt, including after an interrupted rollback. Resume or rollback with
services stopped before restarting.

Restart only after complete validation. `MRMS_*_DIR` Python aliases remain
supported through the 3.x series and resolve to canonical generated paths only
while their products are enabled. For names containing hyphens, use
`fs.MRMS_PATHS['MRMS_MergedAzShear_3-6kmAGL']` or `getattr`; ordinary dotted Python
syntax cannot express that name. Full identities retain elevations; paths strip
only the final elevation. Collisions, including Windows case collisions, are
configuration errors. Adding a raw product does not add an API or render product.

## Reproduce offline measurements

```bash
conda activate EdgeWARN
PYTHONPATH=src python scripts/qualify_mrms_release.py
```

The script uses the actual coordinator, acquisition budget, payload validation,
staging, and atomic publication. Listing and transfer use tiny fixed GRIB2/JSON
transport fixtures. It measures mandatory callbacks independently of optional
drain and total cycle duration; tests the shipped concurrency 8/deadline 30s,
then a 120ms deadline with optional transfer stalled. Every replay starts a new
Python process against the same temporary runtime and checks the publication
count. Each process exits successfully; active/queued work, pending asyncio tasks,
MRMS worker threads, and staging directories are empty at completion. The parent
joins each subprocess with a 60s bound. No operational runtime is used.

Representative run on 2026-09-29, seconds (wall time varies):

| Fixture | Mandatory callbacks | Optional drain | Total cycle | Peak active / queued | Published files |
| --- | --- | --- | --- | --- | --- |
| Defaults, first cycle | 0.032 | 0.141 | 0.173 | 8 / 3 | 21 |
| Defaults, fresh process replay | 0.011 | 0.001 | 0.012 | 1 / 1 | 21 |
| 120ms deadline, first cycle | 0.036 | 0.093 | 0.129 | 8 / 3 | 3 |
| 120ms deadline, fresh process replay | 0.007 | 0.122 | 0.129 | 7 / 2 | 3 |

Both catalog validators passed all 18 documents. The focused architecture
catalog/invariant/contract and packaged Core contract asset checks passed 88 tests.
The shipped defaults satisfy the fixture resource and callback separation checks.
These measurements do not establish production throughput, network latency, RSS,
scientific validity of fixture GRIB sections, or the optimal 30s NOAA deadline.
RAP/GLM, actual service supervision, tracking, and public binary contracts are
covered by their existing regression suites rather than this transport workload.

## Coordinated validation results

Final offline checks on 2026-09-29:

- Full Python suite in the `EdgeWARN` environment: 2,418 passed, 7 skipped.
- `npm test -- --runInBand`: 134 passed across 12 suites. Jest confines discovery
  to the repository's `tests/api` directory, excluding nested worktrees.
- Python and Node configuration validators: all 18 documents passed in each.
- Migration fixtures cover all ten renames, unchanged documents and unrelated
  data, preserved drained records, installed-wheel apply/rollback, interruptions
  during configuration commits and both rename directions, and completed-step
  conflict detection on resume.

Deployment acceptance still requires the operator to verify stopped services,
drained deployment backlog, reviewed custom mappings, filesystem permissions,
and representative NOAA throughput and recovery before restarting production.
