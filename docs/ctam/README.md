# CTAM internal API

CTAM runs a reserved built-in StormProb adapter followed by independently
installed external modules. External modules are discovered only from
`ctam_modules/<module-id>/module.toml`; importing `EdgeWARN.ctam` does not
register or execute any module.

StormProb is bundled because its published motion is consumed by later tracking
cycles. It runs through the same host-owned cycle boundary as external modules.
The `stormprob` module ID and `StormProb` output key are reserved and cannot be
installed externally.

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
