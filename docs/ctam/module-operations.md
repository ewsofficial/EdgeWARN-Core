# Operating CTAM modules

Validate an installation without running module code:

```bash
python src/run_edgewarn.py --check-ctam-modules
python src/run_edgewarn.py --list-ctam-modules
```

Use `--ctam-module-dir` or `EDGEWARN_CTAM_MODULE_DIR` for a packaged module
root. A missing root is valid and leaves StormProb enabled. Install, upgrade, or
remove a module between cycles; discovery is frozen for each active cycle.

Inspect `<base-dir>/data/ctam/cycles/<cycle-id>/status.json` to determine why a
module did not run. The status record reports discovery state, declared
requirement evaluation, and an outcome such as `completed`,
`skipped_missing_requirements`, `failed`, or `timed_out`. Required failures mark
the CTAM stage failed; optional failures remain isolated but visible.

If a module has staged changes but the service stops, the host recovers prepared
publications from `<base-dir>/data/ctam/transactions/` before a later cycle
touches the same files. Irrecoverable journals move to `quarantine/`; do not
delete journals manually before collecting them for diagnosis.

`--disable-ctam` disables both StormProb and external modules.
`--disable-ctam-modules` disables only external modules for troubleshooting.
