# PR #97 fix verification

Reviewed September 5, 2026 against local commit `d24a1c2ec030cc5c8ec34e2114030bb5e9d01c9a`.

Source: [Pre-release packaging and deployment audit](https://github.com/ewsofficial/EdgeWARN-Core/pull/97#pullrequestreview-5119184493). Finding numbers below refer to that report, not separate GitHub issues. The two automated inline findings duplicate findings 1 and 4.

The local checkout contains substantial fixes, but the work is not ready to close out. Findings **2, 4, 6 (package command), 8, 9, 10 (original failure), 11, and 12 (direct-child exception cleanup)** have local implementations addressing the reported failures. Findings **1 and 5 remain open**. Finding **3 is partial**, and **7 cannot be fully confirmed within the requested file scope**. Finding 10 also introduces a shutdown-status concern described below.

The remote PR branch was checked with `git ls-remote` and still points to `6a0e2d2893c75853e86d94d127a97d1f14d0307c`, the original audit commit. These local fixes are therefore **not yet present on the published PR**. No GitHub comments, reviews, or branch updates were made.

## Locally fixed findings

| Finding | Evidence in inspected files | Verification limits |
| --- | --- | --- |
| 2. Runtime roots missing from accessory service state | `src/run_ewmrs.py:72` and `src/run_nexrad.py:57` resolve CLI/environment/YAML into an absolute `args.base_dir`. Their main functions use it for filesystem initialization, service locks, worker registration, and heartbeat paths. `src/edgewarn_cli/run.py:204` also resolves and forwards the default runtime root. | Static confirmation of the reported `None` propagation fix; actual installed service startup was not exercised. |
| 4. Persisted topology settings ignored | `src/run_all.py:67` provides shared `resolve_services`, which applies environment/YAML omissions and MRMS-only filtering and rejects an empty selection. `src/edgewarn_cli/run.py:230` calls it before validating forwarded arguments or constructing commands. `src/run_all.py:186` forwards resolved MRMS-only mode. | Persisted omissions take precedence over requested mode membership. The package-command topology table in `INSTALLATION.md:135` does not yet explain this filtering explicitly. |
| 6. Relative runtime paths fail after package dispatch | `src/edgewarn_cli/run.py:132` canonicalizes both base-directory spellings, including `=value`, relative to the invocation directory. Lines 204–210 resolve the environment/YAML default before the supervisor changes CWD. Commands receive the absolute path. | Confirmed for the package-command path. The direct `run_all.py` entry point still forwards raw base paths, and `overlay.resolve_base_dir` itself still returns relative paths unchanged. This is not a universal direct-launcher fix. |
| 8. Dotted mapping keys cannot be edited | `src/edgewarn_cli/configure.py:61` implements escaped dots/backslashes and reversible path parsing. `src/edgewarn_cli/tui.py:173` retains original segments; its save action at line 334 passes a `DottedTarget` directly. `INSTALLATION.md:241` documents escaped CLI syntax. | Static confirmation of the reported dotted-key addressing fix. |
| 9. Quoted strings change type during YAML serialization | `src/edgewarn_cli/configure.py:327` wraps string assignments in `DoubleQuotedScalarString`. Lines 334–338 parse serialized bytes with PyYAML and validate the resulting document before atomic replacement at line 340. `src/common/config/loader.py:483` uses the same runtime parser. | Addresses the reported `"on"` failure and pre-replacement schema validation. No runtime round-trip suite was executed. |
| 10. Nonzero child shutdown exits reported as success | `src/run_all.py:309` collects final return codes and sets wrapper status 1 for nonzero exits at lines 317–318. A cleanup exit of 7 is no longer classified as success. | The implementation also treats expected signal termination, such as POSIX `-SIGTERM`, as failure. See the remaining concern below. |
| 11. Stop requests do not halt startup admission | `src/run_all.py:249` checks the stop event before each `Popen` and breaks before admitting another service. Already-started processes remain in the cleanup path. | Static confirmation; no signal timing test was run. |
| 12. Supervisor exceptions bypass child cleanup | `src/run_all.py:319` catches exceptions across spawning/supervision/shutdown, invokes `_cleanup_after_error`, and re-raises the original exception. The helper at line 230 sends TERM, waits, escalates on timeout, and waits again without depending on logging. | Addresses the reported supervision/logging exception path for direct children. Cleanup is best effort and sequential, and it does not resolve descendant containment in finding 5. |

## Open, partial, or unverified findings

| Finding | Status | Remaining work or evidence gap |
| --- | --- | --- |
| 1. Conda environment mismatch | **Open — partial fix** | `environment.yml:1`, Docker packaging commands at `Dockerfile:12`/`:14`, and installation activation at `INSTALLATION.md:22` now agree on `EdgeWARN`. However, `Dockerfile:21` still adds `/opt/conda/envs/EdgeWARN-dev/bin` to PATH. The exec-form `edgewarn` entry point is installed in the other environment, so the supplied image still has an executable-discovery defect. `INSTALLATION.md:422` also still names `EdgeWARN-dev` for tests. Align the remaining paths and demonstrate a clean image build/start. |
| 3. NWS assets unavailable in container deployment | **Partial** | `compose.yaml:14` adds a read-only asset mount, and lines 26–32 add an administrative synchronization service sharing that directory. `INSTALLATION.md:364` documents an installed initialization command and Compose initialization. However, the plain Docker default and EWMRS examples at lines 285–288 and 305–308 still omit the asset mount; `geomapper.py:54` still directs missing-asset users to the repository-only script. The new CLI synchronization implementation is outside the report's named files, so its registration and operation were not inspected. The Compose workflow is also blocked by finding 1 until PATH is corrected. |
| 5. Forced shutdown leaves descendants alive | **Open** | `src/run_all.py:222` sends signals only to direct `Popen` processes and skips already-exited leaders. Services still start new sessions at line 257, but there is no process-group cleanup. `src/EdgeWARN/ctam/runner.py:58` still spawns external modules without an outer process-tree containment mechanism. Neither the Dockerfile nor Compose supplies an init/reaping strategy. The original orphaned-descendant failure remains unaddressed in the inspected files. |
| 7. Worker arguments lack full preflight validation | **Implementation present; full confirmation pending** | `src/edgewarn_cli/run.py:159` now rejects explicitly listed one-shot arguments and calls `build_service_parser(...).parse_args()` before dispatch. Parser failures become wrapper usage errors. Both accessory launchers call the same parser builder. However, that builder lives in `src/util/cli.py`, which the audit did not name, so it was not opened. The accepted grammar, abbreviation policy, primary-worker parity, and one-shot bypass handling cannot be fully confirmed from the allowed files. |

Finding **10 needs a follow-up adjustment**: `src/run_all.py:317` rejects every nonzero return code, including expected POSIX signal exits. A worker terminated normally by TERM before installing its handler can therefore make a user-requested stop return 1. The audit requested classification of intended normal/signal outcomes, so the original false-success defect is fixed but the complete shutdown-status contract is not yet satisfied.

## Other audit concerns still pending

- **Windows lifecycle and busy-worker shutdown:** the supervisor still references POSIX `SIGKILL`, lacks a Windows process-tree mechanism, and retains a fixed 10-second grace. Compose retains 25 seconds. Platform behavior and nested shutdown budgets remain unverified.
- **Profiling:** accessory parsers accept profile settings, but the inspected EWMRS/NEXRAD launchers do not wire them into rendering. The NEXRAD profiling example remains in `INSTALLATION.md:157` without explaining the limitation.
- **Configuration discovery:** Docker still exports no `EDGEWARN_CONFIG_DIR`; replacing CMD requires retaining an explicit configuration argument. External configuration deployment is documented, but wheel-only fallback population was not verified.
- **Ownership and recovery:** atomic replacement preserves mode bits but does not preserve UID/GID (`configure.py:274`). The image has no explicit non-root user. Full-tree validation still occurs before editing, and the installation guide does not explain recovery from an already-invalid tree.
- **Build tools, scientific dependencies, and deployment:** clean build-tool availability, healthy scientific imports, installed worker startup, and the separate API deployment sharing runtime output remain release checks. No prior environment failure is claimed to have been reproduced or resolved in this verification.

## Scope and method

This was source inspection, not an execution-based certification. No tests, container builds, live ingest, asset synchronization, or service launches were run. This avoided indirectly loading additional project files outside the requested scope. File references above refer to the local commit, not the older published PR head.

The following 15 files named by the audit were read in full:

- `environment.yml`
- `Dockerfile`
- `INSTALLATION.md`
- `compose.yaml`
- `src/edgewarn_cli/run.py`
- `src/run_ewmrs.py`
- `src/run_nexrad.py`
- `src/common/ingest/nws/geomapper.py`
- `src/run_all.py`
- `src/EdgeWARN/ctam/runner.py`
- `src/common/config/overlay.py`
- `src/util/file.py`
- `src/edgewarn_cli/configure.py`
- `src/edgewarn_cli/tui.py`
- `src/common/config/loader.py`

Scope exception: the initial GitHub PR metadata fetch unexpectedly bundled the PR-wide diff in its response. That response exposed unrelated diff content; it was not used to assess fixes. Subsequent source inspection was restricted to the files listed above, with no follow-up reads of unlisted implementation files.
