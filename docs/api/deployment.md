# Real-Time Service Deployment

Deployment guidance for the three independently operable Python services and
the unified Node API. Source of truth for the design:
`plans/realtime-runner-decomposition-plan.md`; runtime contracts live in
`docs/core/service_registry.md`.

## Services and ownership (single-writer requirements)

Exactly one writer may exist per artifact family at any time. Never run two
owners concurrently. Roll back to a versioned previous deployment or service
unit; `src/run.py` is retired and is not a previous owner.

| Service | Command | Owns |
| --- | --- | --- |
| Primary (`edgewarn`) | `python src/run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300` | MRMS selection/ingest, scan-time GLM, detection/integration/tracking/CTAM/alerts, cycle state, `mrms-ready`/`rap-ready` records |
| EWMRS (`ewmrs`) | `python src/run_ewmrs.py` | MRMS/RAP rendering from committed records, RAP Uint16 conversion, GOES ABI ingest/render, METAR/NWS/WPC ingest, owned GUI retention |
| NEXRAD (`nexrad`) | `python src/run_nexrad.py` | Level-II ingest, NEXRAD rendering, manifests/indexes/retention/cleanup |
| Unified API | `npm run api` | HTTP serving only; reads heartbeats, never writes service state |

The package command `edgewarn run` is the deployment-facing supervisor and can
start supported service topologies. `python src/run_all.py` is an optional
supervisor. Direct service commands remain independently operable, but the
`ewmrs` topology intentionally starts the primary before its accessory service.

## Dependencies and start order

- The services share only the configured base directory and config tree. The
  primary runs usefully alone; the EWMRS record consumer waits for primary
  records, while accessory ingest/render loops continue independently; NEXRAD
  is fully independent.
- Stopping EWMRS affects aggregate EWMRS availability. METAR compatibility
  routes are currently ungated, so route-specific METAR/NWS degradation is not
  exposed by the API.
- Recommended stop order: launcher/API clients last, services first. Each
  service handles SIGINT/SIGTERM and cleans up only its own children.

## Health files

Each service publishes an atomic heartbeat at
`<BASE_DIR>/state/realtime/services/<name>.json` (`edgewarn`, `ewmrs`,
`nexrad`). Heartbeats carry PID, run ID, phase, version, and degraded
children; the API classifies them as `active`, `stale`, `disabled`,
`degraded`, or `unsupported-schema` and gates route families accordingly
(`SERVICE_NOT_ENABLED`, see `docs/api/api_endpoints.md`). Heartbeats are
diagnostic: correctness uses the durable records and checkpoints beneath
`state/realtime/`.

Single-instance locks live beside the heartbeats (`<name>.lock`, OS flock);
a second instance of any service fails fast.

## Backlog recovery

EWMRS consumes `mrms-ready`/`rap-ready` records in timestamp order with
per-phase checkpoints under `state/realtime/consumers/`. After a crash it
resumes unacknowledged records idempotently. If it falls more than
`cycle.max_backlog_cycles` behind, the excess oldest cycles are abandoned in
logs and the checkpoint is advanced (never rendered under older timestamps);
processing resumes at the oldest still-valid record.

## systemd examples

One unit per service, all sharing `EDGEWARN_BASE_DIR`. Adjust paths/user to
the host.

```ini
# /etc/systemd/system/edgewarn-primary.service
[Unit]
Description=EdgeWARN primary analysis service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=edgewarn
Environment=EDGEWARN_BASE_DIR=/home/edgewarn/EdgeWARN_input
WorkingDirectory=/opt/EdgeWARN-Core/src
ExecStart=/opt/miniconda3/envs/EdgeWARN/bin/python run_edgewarn.py --lat_limits 20 55 --lon_limits 230 300
Restart=on-failure
RestartSec=10s
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/edgewarn-ewmrs.service
[Unit]
Description=EWMRS rendering and accessory service

[Service]
Type=simple
User=edgewarn
Environment=EDGEWARN_BASE_DIR=/home/edgewarn/EdgeWARN_input
WorkingDirectory=/opt/EdgeWARN-Core/src
ExecStart=/opt/miniconda3/envs/EdgeWARN/bin/python run_ewmrs.py
Restart=on-failure
RestartSec=10s
KillSignal=SIGINT
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/edgewarn-nexrad.service
[Unit]
Description=NEXRAD ingest and rendering service

[Service]
Type=simple
User=edgewarn
Environment=EDGEWARN_BASE_DIR=/home/edgewarn/EdgeWARN_input
WorkingDirectory=/opt/EdgeWARN-Core/src
ExecStart=/opt/miniconda3/envs/EdgeWARN/bin/python run_nexrad.py
Restart=on-failure
RestartSec=10s
KillSignal=SIGINT
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
```

Container deployments may use the package supervisor in one container, as the
repository `Dockerfile` and `compose.yaml` do, or separate direct-service
containers. In either case, share the mounted base directory and do not pass
cross-container `multiprocessing` objects. The unified Node API is a separate
process/container running `npm run api`; configure its `PORT`,
`EDGEWARN_BASE_DIR`, `EDGEWARN_CONFIG_DIR`, and health checks explicitly.

## Rollback

1. Stop the new service unit (`systemctl stop edgewarn-ewmrs`).
2. Start the versioned previous service unit or deployment for that artifact
   family.
3. Keep cycle records, consumer checkpoints, and heartbeats in place — they
   are recovery evidence, not cache.

Never run two writers concurrently to "bridge" a rollback; the ownership
matrix above is an invariant, not a recommendation.
