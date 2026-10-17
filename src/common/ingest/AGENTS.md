# AGENTS Guide for `src/common/ingest/`

## Scope
Shared ingestion implementations for MRMS, NWS, Synoptic, and NEXRAD inputs.

## Agent guidance
- Keep async-first ingest behavior with safe sync/HTTPS fallback.
- Be careful with timestamp handling, file naming, and base-directory output layout.
- Performance-sensitive changes here can affect both real-time and historical flows.
