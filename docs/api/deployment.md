# Service deployment

Deploy ingest, Core, and NEXRAD independently or through `edgewarn run`.
Start ingest before Core. NEXRAD supervises its acquisition/render children.
Each artifact family has one owner; single-instance service locks reject
competing writers. Use the same complete configuration tree and runtime root.

```bash
edgewarn run --config-path /etc/edgewarn/config
npm run api
```

The Node API deploys separately. It reads only published files and classifies
`state/realtime/services/{ingest,edgewarn,nexrad}.json`. Configure
`api.server.enabled_services` for the deployed topology. Runtime readiness
requires `data/`, `gui/`, and healthy enabled services. Core/ingest progress can
be stalled despite live PIDs; radar routes retain service gates.

Use a shared umask and group permissions so the API can read atomic outputs.
Set `EDGEWARN_BASE_DIR`, `EDGEWARN_CONFIG_DIR`, browser origins, and trusted proxy
IPs explicitly. Keep configuration read-only during operation. Restart after
configuration changes. Supervisors forward signals, bound termination, and
reap process groups; service managers should allow at least the configured
shutdown bound before force-killing.

Core resumes from durable readiness and consumer state. Dependency disagreement
fails visibly; it never converts stale records into success. Raw retention
protects live worker/history pins. Existing retired files and service records
are ignored by retired readers; operators may remove them explicitly.

For container setup, configuration editing, upgrade and rollback see
[installation](../../INSTALLATION.md) and [the changelog](../../CHANGELOG.md).
