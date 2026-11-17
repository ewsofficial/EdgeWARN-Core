# Core runtime overview

Independent services separate acquisition from latency-sensitive analysis:

```text
MRMS / RAP / GLM -> ingest -> validated inventory and pinned readiness -> Core
NOAA Level-II -> NEXRAD acquisition -> staged elevations -> NEXRAD polar products
Core snapshots / cells / alerts / modules + NEXRAD products -> separate API v3
```

Core performs detection, optional tracking/lineage, enrichment, CTAM, StormProb,
alerts, and filesystem publication. Historical processing reuses the Core
pipeline through the staged coordinator rather than the realtime consumer.

The supervisor performs no scientific work. `edgewarn run` starts ingest, Core,
and NEXRAD; `core` starts ingest and Core. NEXRAD owns both acquisition and render
children. All workers share one configuration tree and runtime root.

## Storage and readiness

Base-directory precedence is CLI, `EDGEWARN_BASE_DIR`, legacy `BASE_DIR`, then
`filesystem.yaml`. Defaults are `~/EdgeWARN_input` on POSIX and
`C:\EdgeWARN_input` on Windows. Runtime layout:

```text
<BASE_DIR>/data/                         weather inputs, analysis and alerts
<BASE_DIR>/gui/NEXRAD/                   compressed polar fields and manifests
<BASE_DIR>/state/realtime/ingest/v1/      inventory and Core phase records
<BASE_DIR>/state/realtime/consumers/      Core consumer state
<BASE_DIR>/state/realtime/leases/         activity and input leases
<BASE_DIR>/state/realtime/services/       heartbeats, registry agreement and locks
```

Ingest releases detection/check readiness first, mandatory integration next,
and an immutable final optional-input snapshot last. Required missing sources
fail closed. Reference-aware retention preserves live selections and history.
Heartbeats describe availability and progress; exact records and fingerprints
establish correctness. Publication is atomic across processes.

## References

- [Acquisition](ingestion.md)
- [Configuration](configuration.md)
- [Service registry](service_registry.md)
- [Detection](detection.md)
- [Integration](integration.md)
- [CTAM](../ctam/README.md)
- [API v3](../api/unified_v3.md)
