# Configurable MRMS phase 3

Phase 3 prepares validation and offline conversion. The shipped catalog remains
v1 until phase 8 coordinates the runtime, consumer, and migration release.
`ingest.v2.schema.json` validates development v2 catalogs; both Python and Node
select it when `ingest.schema_version` is 2. The existing v1 schema remains the
startup default. Promoting the v2 schema to `ingest.schema.json` rejects v1 with
an actionable migration diagnostic. Do not run workers against converted trees
before the later runtime phases are complete.

Both disk loading and `edgewarn configure` use the same semantic validation.
Protected product collisions are checked against the packaged Core contract.
V2 rejects legacy source, directory, membership, and discovery overrides.
Download concurrency is an integer from 1 through 64; optional timeout is positive
and at most 3600 seconds. Retention, buffer sizes, and HTTP settings retain their
positive v1 bounds. Active-consumer history requirements are deferred to preflight
in phase 6. The initial defaults are 8 downloads and a 30-second optional timeout;
phase 8 must qualify them with latency measurements.

Integration statistics and MRMS render layers accept exactly one of legacy
`filepath` or the prepared `product` field. `product` uses the upstream identity,
including elevation and without the `MRMS_` prefix, for example
`MergedAzShear_3-6kmAGL_00.50`; ProbSevere is `ProbSevere`. Conversion replaces
raw aliases only. GUI names, output paths, colormaps, GOES, and document versions
other than ingest remain unchanged. Consumer activation belongs to phase 7.

`runtime.run.disable_stormprob` is prepared as a boolean schema field and is added
as `false` in converted documents. It does **not yet disable StormProb**. The shared
fixture records the intended `--disable-stormprob` / `--no-disable-stormprob` mapping;
CLI parsing, overlays, dependency checks, and inference bypass belong to phase 6.

## Offline migration report

```bash
edgewarn migrate-mrms --config-path /etc/edgewarn/config --base-dir /runtime
```

The command reads v1 YAML directly without loading workers or binding runtime
paths. It prints JSON containing candidate converted documents, restored protected
products, all 21 path mappings (ten renames and eleven unchanged), custom-setting
conflicts, and existing runtime records requiring backlog/pin review. It validates
the candidate using the installed release schemas, independently of an operator's
old schema directory. Exit status is 0 for a conflict-free report and 2 for an
invalid input or reported conflict. `--dry-run` is optional and has the same effect.

The report never writes config, creates runtime directories, renames data, or
contacts upstream services. Custom source/directory mappings require review and
are reported as conflicts; candidate documents with conflicts must not be applied.
Existing cycle, checkpoint, lease, and service records are listed conservatively;
this is not proof that services are stopped or a backlog is drained. Apply, backups,
journaling, resume, liveness/pin interlocks, and rollback belong to phase 8.
`--apply` is currently rejected.

Regression coverage uses `tests/fixtures/config/mrms_v2_validation.json` in both
Python and Node. Tests compare the actual converter output to that fixture,
exercise editor validation, and compare config/runtime bytes before and after a
migration dry run. The packaged `common/config/mrms-v1.json` is the frozen migration
baseline for detecting old customizations, not an active product catalog.
