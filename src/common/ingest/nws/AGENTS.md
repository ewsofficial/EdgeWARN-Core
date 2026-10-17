# AGENTS Guide for `src/common/ingest/nws/`

## Purpose
NWS alert/zone ingestion and geometry mapping.

## Agent guidance
- Geometry and registry behavior must stay aligned with the zone assets in `assets/nws_zones/`.
- Prefer deterministic handling of malformed or partial alert data.
