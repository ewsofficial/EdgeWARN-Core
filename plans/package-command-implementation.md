# EdgeWARN Package Command Implementation Plan

**Planning baseline:** commit `5cfbb048` on branch
`yuchen-wei3667/package-commands`
**Package version:** `2.7.0` from `package.json`
**Status:** Phases 1-5 implemented. Focused package-command verification passes;
the complete repository Python suite still has unrelated pre-existing CTAM
failures (see the verification report in the handoff).

## Implementation progress

- [x] Phase 1: Python packaging, console registration, installed-version
      lookup, dependency declarations, editable configuration deployment notes,
      and package/import tests.
- [x] Phase 2: topology-aware `edgewarn run` dispatch.
- [x] Phase 3: safe noninteractive configuration mutation.
- [x] Phase 4: interactive configuration TUI.
- [x] Phase 5: Docker and final documentation integration.

## Objective

Register a Python console command through `pyproject.toml` so Docker and local
operators can launch EdgeWARN services as `edgewarn run ...` and safely inspect
or modify the deployed YAML configuration as `edgewarn configure ...`.

The command must be a thin package-facing control layer. Scientific work,
service supervision, process locking, signal handling, and durable readiness
remain owned by the existing launchers and runtime modules.

## Public command contract

### Service launch commands

| Command | Services started | Existing implementation used |
| --- | --- | --- |
| `edgewarn run` | primary EdgeWARN, EWMRS/accessories, and NEXRAD | `run_all.py` with all services |
| `edgewarn run core` | primary EdgeWARN only | `run_edgewarn.py` |
| `edgewarn run ewmrs` | primary EdgeWARN plus EWMRS/accessories | `run_all.py --services edgewarn,ewmrs` |
| `edgewarn run nexrad` | NEXRAD ingest and NEXRAD render only | `run_nexrad.py` |

`core`, `ewmrs`, and `nexrad` are mutually exclusive positional modes. Omitting
the mode selects `all`.

The requested EWMRS dependency is the primary service currently implemented by
`src/run_edgewarn.py`. There is no `src/run_core.py` in the planning baseline,
so this work must not add a duplicate launcher merely to satisfy that filename.
The package CLI maps the public name `core` to the existing internal service
name `edgewarn`.

The NEXRAD note is already satisfied in the current runtime architecture:
`run_nexrad.py` calls `register_nexrad_supervision()`, which registers both
`NEXRAD Ingest` and `NEXRAD Render`. Implementation work must preserve and test
that behavior rather than create another ingest process in the package layer.

### Launch options

```text
edgewarn run [all|core|ewmrs|nexrad]
             [--config-path PATH]
             [--args WORKER JSON_ARGV]
```

- `--config-path` selects the complete configuration directory. Its default is
  `<registered-project-root>/config` for an editable/source installation and
  the installed configuration resource copied by the image for a wheel/container
  installation. The resolved directory is translated to the existing
  `--config-dir`/`EDGEWARN_CONFIG_DIR` contract before a launcher starts.
- The directory must contain every name in `common.config.loader.CONFIG_NAMES`
  and the matching `schema/*.schema.json` files. All documents are validated
  before filesystem initialization or child-process creation. Failure prints
  the filename and dotted path from `ConfigError` and exits with status `2`.
- `--args` is repeatable and worker-scoped. `WORKER` is one of `core`,
  `ewmrs`, or `nexrad`; `JSON_ARGV` is a JSON array of strings. JSON avoids shell
  re-tokenization and works consistently with Docker's exec-form `CMD`.
- A worker may be named only if it is part of the selected topology. Repeating
  the same worker is an error. Wrapper-owned options such as `--config-dir`,
  `--config-path`, and service-selection flags are rejected inside `JSON_ARGV`.
  The wrapper injects the single resolved configuration path itself.
- The child launcher remains authoritative for script-specific parsing. An
  invalid forwarded option produces that launcher's normal nonzero usage error.

Examples:

```bash
edgewarn run
edgewarn run core --config-path /etc/edgewarn/config
edgewarn run core \
  --args core '["--lat_limits", "20", "55", "--disable-ctam"]'
edgewarn run ewmrs \
  --args core '["--lat_limits", "20", "55"]' \
  --args ewmrs '["--disable-wpc"]'
edgewarn run nexrad \
  --args nexrad '["--profile"]'
```

The JSON-argv contract resolves the current ambiguity around passing arguments
to multi-worker modes. Do not accept a single unscoped remainder and broadcast
it to children: several existing flags have intentionally different owners.

### Configuration commands

```text
edgewarn configure [--config-path PATH]
edgewarn configure [--config-path PATH] FILE.KEY[.KEY|.INDEX...] VALUE
```

Examples:

```bash
edgewarn configure
edgewarn configure ewmrs_pipeline.workers.budget_mb.goes 2048
edgewarn configure --config-path /etc/edgewarn/config \
  runtime.run.disable_nexrad true
```

The first dotted segment is a filename stem from
`common.config.loader.CONFIG_NAMES`; `.yaml` in the command is not accepted.
Subsequent mapping keys traverse YAML objects. A decimal segment traverses a
sequence index when the current node is a sequence. Empty segments, negative
indices, missing keys, out-of-range indices, and attempts to replace the YAML
document root are usage errors.

`VALUE` is parsed as exactly one YAML scalar, so `2048`, `true`, `null`, and
quoted strings retain their intended types. Collections and multiple YAML
documents are rejected in the first implementation; complex structural edits
remain a manual configuration-management operation.

Before changing a file, the command must:

1. Resolve and security-check the configuration root and target path.
2. Validate the complete existing configuration tree.
3. Load the selected document with round-trip YAML support so comments, key
   order, quoting where practical, and final newline are retained.
4. Resolve the dotted path and parse the replacement value.
5. Validate the modified in-memory document against that file's schema.
6. Write a temporary file in the target directory, flush and `fsync` it,
   preserve the original permission bits, and atomically replace the target.
7. Clear the loader cache and validate the complete tree from disk again.

If any step before replacement fails, the original file remains untouched. If
the post-write verification unexpectedly fails, restore the same-process backup
with another atomic replace, report both failures, and exit nonzero. The command
must reject a target YAML file that resolves outside the selected root through a
symlink.

On success, print the file, dotted path, old value, new value, and validation
result. Do not print unrelated configuration values because future catalogs may
contain secrets.

### Interactive TUI

Running `edgewarn configure` without a dotted path starts a Textual TUI only
when stdin and stdout are TTYs. In a noninteractive container it exits with
status `2` and explains how to use the dotted assignment form.

The TUI has two layers:

1. A `Select` control lists the validated configuration filename stems in
   `CONFIG_NAMES` order.
2. Selecting a file opens a tree/table of its leaf variables. Each row shows
   dotted path, current value, inferred type, and a concise schema constraint
   summary when present. Selecting a row opens an input/editor action using the
   same parsing, validation, and atomic-write service as the noninteractive
   command.

The TUI must never implement a second mutation path. Save, error rendering,
reload, and rollback call the same configuration service exercised by CLI unit
tests. `Esc` returns to file selection, `Ctrl+S` saves a valid edit, and `q`
quits when no editor is open. Validation errors remain on screen and do not
modify the file.

## Baseline findings before Phase 1

- No `pyproject.toml`, `setup.py`, or `setup.cfg` exists, so there is currently
  no Python distribution metadata or console-script registration.
- Python imports assume `src` is on `PYTHONPATH`; pytest supplies that through
  `pytest.ini`.
- `run_all.py` already provides subprocess supervision, signal forwarding,
  bounded termination, service selection, and ownership-aware routing for its
  fixed option set.
- `run_edgewarn.py`, `run_ewmrs.py`, and `run_nexrad.py` validate configuration
  through the existing loader, but validation is not centralized at a package
  command boundary.
- `common.config.loader` already owns config-root discovery, the canonical
  `CONFIG_NAMES`, schema walking, frozen loading, caching, and `ConfigError`.
  Its schema-document validator is private and needs a small public API for
  validating an edited in-memory document without writing invalid YAML first.
- The declared Python environment includes PyYAML but has no round-trip YAML or
  TUI dependency.
- No Dockerfile or Compose file exists at the baseline. This change defines the
  command contract Docker should invoke; image creation can then use it without
  embedding source-script paths.

## Proposed code organization

```text
pyproject.toml
src/
├── edgewarn_cli/
│   ├── __init__.py
│   ├── main.py             # top-level argparse parser and exit-code boundary
│   ├── run.py              # topology, JSON argv validation, launcher dispatch
│   ├── configure.py        # dotted-path mutation and atomic transaction
│   └── tui.py              # Textual widgets; lazily imported
└── common/config/loader.py # public in-memory document validation helper
tests/
├── unit/test_package_cli.py
├── unit/test_package_run.py
├── core/config/test_configure_command.py
├── core/config/test_configure_tui.py
└── integration/test_installed_command.py
```

Keep `edgewarn_cli.__init__` lightweight. It must not import Textual or any
scientific pipeline. `main.py` imports the TUI lazily only for an interactive
`configure` invocation. `edgewarn --help`, configuration errors, and command
parsing should therefore remain fast and safe in minimal diagnostic contexts.

## Phase 1: Add Python packaging and the console entry point

1. Add `pyproject.toml` using `setuptools.build_meta`, `src` package discovery,
   Python `>=3.13`, the MIT license, project metadata matching `package.json`,
   and this entry point:

   ```toml
   [project.scripts]
   edgewarn = "edgewarn_cli.main:main"
   ```

2. Include the existing top-level launcher modules needed by the installed
   command, or refactor their reusable supervision functions into packages so
   wheel installs do not depend on repository-relative imports. Verify both an
   editable install and a built wheel; success from the repository checkout
   alone is insufficient.
3. Keep version `2.7.0` synchronized between Python metadata and `package.json`.
   Update `util.release.get_release_version()` to use installed distribution
   metadata first and retain `package.json` as a source-checkout fallback.
4. Keep `environment.yml` as the sole runtime dependency authority. Add
   `textual` and `ruamel.yaml` there alongside PyYAML; leave
   `project.dependencies` empty so a package install cannot silently create a
   pip-managed runtime that diverges from Conda.
5. Ensure the deployed `config/` and `config/schema/` trees are copied as
   operator-editable files rather than treated as immutable files inside a
   zipped wheel. Document how the container image places them and selects them
   with `--config-path`.

### Phase 1 acceptance

- `pip install --no-deps -e .` creates an `edgewarn` executable in the Conda
  environment.
- A wheel built from the repository installs into a clean Python 3.13
  environment and `edgewarn --help` succeeds outside the repository.
- `edgewarn --version` reports `2.7.0` without requiring `package.json` beside
  the installed package.
- Importing `edgewarn_cli` starts no worker, creates no runtime directory, and
  imports no EdgeWARN/EWMRS/NEXRAD scientific module.

## Phase 2: Implement topology-aware `edgewarn run`

1. Build a small immutable topology table mapping the public modes to internal
   services. Use that table for parser choices, help text, validation, and
   dispatch.
2. Refactor `run_all.build_service_commands()` to accept an optional per-service
   argv mapping. Append validated worker argv and inject one canonical
   `--config-dir` value into every child command. Reject attempts to override
   the wrapper-owned configuration path.
3. Use the existing `run_all.supervise()` path for `all` and `ewmrs` modes.
   For `core` and `nexrad`, use the same command builder and supervisor with a
   one-service set rather than maintaining separate signal/exit-code logic.
   This gives Docker one PID-1 supervisor contract for every mode.
4. Resolve `--config-path`, export `EDGEWARN_CONFIG_DIR`, clear stale config
   caches, and call `validate_all_configs()` before constructing child
   processes. No base-directory initialization belongs in this wrapper.
5. Return child startup/exit failures and forced-shutdown failures as nonzero
   wrapper exit codes. SIGINT/SIGTERM-driven clean shutdown retains the existing
   zero-exit behavior.

### Phase 2 acceptance

- Each public mode constructs exactly the service set in the contract table.
- `ewmrs` starts primary before EWMRS and stops both if either exits.
- `nexrad` registers and runs both ingest and render supervision children.
- Malformed JSON argv, non-string argv elements, args for an absent worker, and
  wrapper-owned forwarded flags fail before any subprocess starts.
- Each worker receives its own argv only, exactly once, without shell parsing.
- An invalid/missing config tree exits `2` before filesystem or process side
  effects.
- SIGTERM sent to an `edgewarn run ...` PID is forwarded and all children are
  reaped within the existing bounded grace period.

## Phase 3: Add safe configuration mutation

1. Expose a supported `validate_document(name, document, *, config_dir=None)`
   function from `common.config.loader`. It must use the same schema lookup,
   supported-keyword checks, path formatting, and `ConfigError` semantics as
   `load_config()`.
2. Implement configuration-root resolution shared by `run` and `configure`.
   The resolver records the registered default at install time without relying
   on the caller's current working directory; an explicit `--config-path`
   always wins.
3. Implement and unit-test dotted-path parsing independently from YAML I/O.
   Mapping keys take precedence when a mapping contains a numeric-looking key;
   integer conversion occurs only while traversing a sequence.
4. Parse replacement values with a safe scalar-only YAML parser. Reject tags,
   aliases, multiple documents, and collections.
5. Implement the round-trip load, in-memory validation, atomic write,
   permissions preservation, cache reset, post-write validation, and rollback
   transaction described above.
6. Serialize concurrent writers with a lock file inside the configuration root.
   Re-read and revalidate after acquiring the lock so two `configure` commands
   cannot silently overwrite each other.

### Phase 3 acceptance

- The provided `ewmrs_pipeline.workers.budget_mb.goes 2048` example updates only
  that leaf and reloads as an integer.
- Boolean, null, float, and explicitly quoted string assignments retain type.
- Missing paths, invalid types/ranges/enums, read-only targets, symlink escapes,
  and malformed YAML leave the original bytes unchanged.
- Comments and mapping order survive a successful leaf edit.
- A simulated write, `fsync`, replace, and post-validation failure has a tested
  error/rollback outcome.
- Concurrent editors cannot produce a lost update or a partially written file.

## Phase 4: Build the two-layer TUI

1. Add a Textual application with file-selection and variable-browser screens.
2. Flatten mapping leaves and sequence elements into the same canonical dotted
   paths accepted by the noninteractive command. Do not display aliases as
   separate editable objects.
3. Read applicable `type`, `enum`, numeric bounds, and array bounds from the
   selected JSON schema for contextual hints. The loader remains authoritative;
   hints are not a replacement validator.
4. Route edits through the Phase 3 transaction service and refresh the document
   only after a successful save.
5. Add Textual pilot tests for file selection, variable rendering, successful
   edit, validation failure, navigation, and quit behavior.

### Phase 4 acceptance

- The first screen is a dropdown of all registered config filenames.
- Selecting a filename displays every editable leaf with its full path/value.
- A valid save updates disk and refreshes the displayed value.
- An invalid save displays the schema error and leaves disk unchanged.
- No-argument `configure` refuses to launch the TUI without a TTY.

## Phase 5: Docker and documentation integration

1. Update `README.md`, `INSTALLATION.md`, and `docs/core/configuration.md` with
   installation, all command modes, JSON argv examples, configuration mutation,
   TUI behavior, and exit codes.
2. When container definitions are added, install the built project and use
   exec-form commands. The default all-service image contract is:

   ```dockerfile
   ENTRYPOINT ["edgewarn"]
   CMD ["run", "--config-path", "/etc/edgewarn/config"]
   ```

   Specialized containers append `core`, `ewmrs`, or `nexrad`. Mount the
   runtime base directory separately from the configuration directory, and
   mount configuration read-write only in an administrative container intended
   to run `edgewarn configure`.
3. Add CI that builds a wheel, installs it into an isolated environment, runs
   CLI help/version/config validation smoke tests, and invokes supervisor tests
   with throwaway child scripts rather than live weather ingestion.

### Phase 5 acceptance

- Container commands contain no `python src/run_*.py` paths.
- Container stop reaches every selected service and leaves no child process.
- A read-only production config mount supports `edgewarn run`; a configuration
  edit on it fails clearly without partial output.
- Documentation states that `ewmrs` includes its primary producer dependency
  and that `nexrad` includes ingest plus rendering.

## Test matrix

Run focused tests during implementation:

```bash
conda activate EdgeWARN-dev
python -m pytest \
  tests/unit/test_package_cli.py \
  tests/unit/test_package_run.py \
  tests/unit/test_run_all_launcher.py \
  tests/core/config/test_configure_command.py \
  tests/core/config/test_configure_tui.py \
  tests/integration/test_installed_command.py
PYTHONPATH=src python -m common.config.validate
npm run validate-config
```

Then run the complete suites before merge:

```bash
python -m pytest tests/
npm test
```

The installed-command integration tests must use temporary config copies and
stub service scripts. They must not contact NOAA/AWS, initialize the normal
runtime base directory, or leave background processes behind.

## Exit-code contract

| Code | Meaning |
| --- | --- |
| `0` | successful configuration edit, help/version, or clean signal shutdown |
| `1` | child startup/runtime failure, forced shutdown, write/rollback I/O failure |
| `2` | command usage, forwarded-argv, config-root, YAML, or schema validation error |

Child-specific nonzero codes are logged with the worker name; the supervisor
returns `1` rather than exposing an unstable union of child exit codes.

## Completion checklist

- [x] `pyproject.toml` installs `edgewarn = edgewarn_cli.main:main`.
- [x] Source/editable and clean wheel installs both work outside the repo root.
- [x] Default, core, EWMRS-with-core, and complete NEXRAD modes match the public
      topology contract.
- [x] Worker-scoped JSON argv reaches only its named worker.
- [x] `--config-path` is resolved once, propagated to every child, and fully
      validated before process startup.
- [x] Noninteractive configuration edits are typed, locked, schema-validated,
      comment-preserving, atomic, and rollback-tested.
- [x] No-argument configuration opens the two-layer TUI only on a real terminal.
- [x] Docker uses the registered command and exec-form process arguments.
- [x] Packaging, operator, configuration, and container documentation is synced.
- [ ] Focused and full Python/Node validation matrices pass.

### Verification note

Phase 5 focused verification is green (package delivery contracts, installed
wheel smoke tests, topology/supervisor tests, configuration mutation/TUI tests,
both config validators, and the complete Node suite). The full Python suite was
run against the repository and reached 2,019 collected tests, but it reports
failures in pre-existing CTAM read-only API, documentation-citation, and
runner tests outside this package-command plan. The full-matrix checklist stays
unchecked until those unrelated baseline failures are resolved.
