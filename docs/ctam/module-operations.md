# Operating CTAM modules

Validate an installation without running module code:

```bash
python src/run_edgewarn.py --check-ctam-modules
python src/run_edgewarn.py --list-ctam-modules
```

Use `--ctam-module-dir` or `EDGEWARN_CTAM_MODULE_DIR` for a packaged module
root; otherwise `run.ctam_module_dir` in `config/runtime.yaml` is used. A
missing root is valid and leaves StormProb enabled unless CTAM itself is
disabled. `--list-ctam-modules` reports diagnostics and exits successfully;
`--check-ctam-modules` returns nonzero for invalid modules. Install, upgrade, or
remove a module between cycles. The catalog is frozen per cycle, but module
discovery is currently scanned in more than one CTAM phase, so do not mutate
the root while a cycle is active.

Inspect `<base-dir>/data/ctam/cycles/<cycle-id>/status.json` to determine why a
module did not run. The status record reports discovery state, a
requirements-satisfied boolean, unmet requirement selectors, and an outcome
such as `completed`, `skipped_missing_requirements`, `failed`, or `timed_out`.
Discovery states such as `invalid`, `skipped_disabled`, `ready`, and `running`
may also appear. Required failures set the status record state to failed, but
the integration pipeline currently isolates the exception and continues
publication. A required module skipped for unmet requirements does not count as
a required failure.

Unsealed module changes are in memory and are lost if the service stops. The
host recovers prepared publication journals from
`<base-dir>/data/ctam/transactions/` during cycle recovery and before a later
publication. Irrecoverable journals move to
`<base-dir>/data/ctam/transactions/quarantine/`; do not delete journals
manually before collecting them for diagnosis.

`--disable-ctam` disables both StormProb and external modules.
`--disable-ctam-modules` disables only external modules for troubleshooting.
