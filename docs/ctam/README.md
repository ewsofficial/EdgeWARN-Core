# CTAM overview

CTAM runs a reserved built-in StormProb adapter followed by independently
installed external modules. External modules are discovered only from
`<configured-root>/<module-id>/module.toml`; the default root is
`ctam_modules/` and it can be overridden with `--ctam-module-dir`,
`EDGEWARN_CTAM_MODULE_DIR`, or `run.ctam_module_dir`. Importing
`EdgeWARN.ctam` does not register or execute any module.

StormProb is bundled because its published motion is consumed by later tracking
cycles. It shares the cycle and catalog with external modules, but uses a
distinct in-process host boundary rather than the external loopback transaction
boundary. The `stormprob` module ID and the `StormProb` and `_grid_outputs`
output keys are reserved, case-insensitively, and cannot be installed
externally.

The former in-process `AnalysisModule`, registry, and grid-module conventions
are retired. A grid analysis must use the cycle-scoped external API rather than
returning an in-memory `_grid_outputs` object.

See [module-manifest.md](module-manifest.md) for discovery and declaration,
[internal-api.md](internal-api.md) for the private loopback contract, and the
checked-in OpenAPI/schema documents for request and response validation.

Module authors should follow [module-development.md](module-development.md);
operators should use [module-operations.md](module-operations.md). The protocol
support policy is in [compatibility.md](compatibility.md), and the measured
regression budget is in [performance-baseline.md](performance-baseline.md).
