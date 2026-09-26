# EdgeWARN API Data Keys

This document describes backing-file shapes used by the v3 API. The v3 API wraps most JSON resources in `data` and
`meta`, transforms METAR responses, and serves WPC detail as native GeoJSON;
those v3 contracts are documented in `docs/api/unified_v3.md` and OpenAPI.

When a route serves a file directly, the response shape is usually the same as the file shape. Some files are passed through as-is, so producer-specific keys may appear in addition to the fields listed here.

## Cells

### `cells/cell_index.json`

Used by `GET /api/v3/cells`.

- `cellIds` (`number[]`): Sorted list of known cell IDs.
- `lastUpdated` (`string`): ISO 8601 timestamp for when the index was last written.

### `cells/{id}.json`

Used by `GET /api/v3/cells/:cellId` and the backing cell history lookup.

This file is a history array served as-is. Each array item is a detection or
tracking snapshot; the exact fields depend on the pipeline. Common keys include:

- `id` (`number|string`): Cell identifier.
- `timestamp` (`string`, optional): Scan timestamp for the record.
- `event_type` (`string`, optional): Tracking event label such as `ACTIVE`, `MERGE`, `SPLIT`, or `DISSIPATED`.
- `tracking_mode` (`string`, optional): Tracking state such as `active`, `predicted`, `decaying`, or `dissipated`.
- `centroid` (`number[2]`, optional): `[latitude, longitude]` center point.
- `bbox` (`number[2][]`, optional): Bounding polygon points as `[latitude, longitude]` pairs.
- `num_gates` (`number`, optional): Number of radar gates assigned to the cell.
- `max_refl` (`number`, optional): Maximum reflectivity value for the cell.
- `merged_cells` (`number[]`, optional): Non-dominant parent IDs merged into this cell.
- `merged_to` (`number`, optional): Child ID this dissipated cell merged into.
- `parent_ids` (`number[]`, optional): Parent IDs associated with merge/split lineage.
- `split_from` (`number|null`, optional): Parent ID for a split child.

## Stormcell Snapshots

### `stormcells/stormcell_index.json`

Used by `GET /api/v3/storm-snapshots`.

- `timestamps` (`string[]`): Sorted list of available snapshot timestamps in `YYYYMMDD-HHMMSS` format.
- `lastUpdated` (`string`): ISO 8601 timestamp for when the index was last written.

### `stormcells/stormcells_{timestamp}.json`

Used by `GET /api/v3/storm-snapshots/:timestamp`.

This file is served as-is. The current producer writes a wrapper with
`source`, `product`, `version`, `modified`, `latest_timestamp`, and `features`;
the storm-cell records are the items in `features[]`.

- `modified` (`string`): UTC ISO 8601 timestamp ending in `Z`, refreshed when
  the snapshot is published. This is the snapshot publication time.
- `latest_timestamp` (`string`): Timestamp of the latest storm-cell data in
  the snapshot; it may differ from `modified`.

Do not assume a top-level `timestamp` or `cells` member.

## Official NWS Alerts

### `Alerts/official/ids/{safe_alert_id}.json`

Used by `GET /api/v3/alerts/:alertId?source=official`.

The stored file is a registry entry. The API normally returns the nested `feature` object when it exists.

- `id` (`string`): Source alert ID, typically the full NWS alert URL. The
  extracted `urn_oid` is used separately for registry keys and safe filenames.
- `first_seen` (`string`): ISO 8601 timestamp for when the alert was first observed.
- `last_seen` (`string`): ISO 8601 timestamp for when the alert was last observed.
- `expires` (`string|null`): ISO 8601 expiration timestamp when available.
- `feature` (`object`): GeoJSON feature payload from the NWS feed.

Common keys inside `feature`:

- `id` (`string`, optional): Source alert identifier.
- `type` (`string`, optional): Usually `Feature`.
- `geometry` (`object|null`, optional): GeoJSON geometry.
- `properties` (`object`, optional): NWS CAP metadata such as `event`, `headline`, `severity`, `effective`, and `expires`.

### `Alerts/official/timestamps/{timestamp}.json`

Backs `GET /api/v3/alert-snapshots/:timestamp?source=official`.

The on-disk file is a wrapper object, but the API response returns only the `alerts` array from that object. If the timestamp file is absent, the API returns `[]`.

- `count` (`number`): Number of summarized alerts in the snapshot.
- `alerts` (`object[]`): Alert summary rows for that timestamp.

Each item in `alerts` contains:

- `id` (`string`): Alert identifier.
- `name` (`string|null`): Alert/event name.
- `urn_oid` (`string`): `urn:oid` identifier used by the API.
- `effective` (`string|null`): ISO 8601 effective timestamp.
- `expires` (`string|null`): ISO 8601 expiration timestamp.
- `severity` (`string|null`, optional): Severity label when available.
- `geometry` (`object|null`): GeoJSON geometry for the alert.

## EdgeWARN Alerts

### `Alerts/EdgeWARN/ids/{safe_alert_id}.json`

Used by `GET /api/v3/alerts/:alertId?source=edgewarn`.

- `alert_type` (`string`): Alert category such as `severe_weather` or `flash_flood`.
- `source` (`string`): Producing CTAM module.
- `id` (`string`): Stable alert identifier.
- `cell_id` (`string`): Cell identifier associated with the alert.
- `geometry` (`number[2][]`): Polygon as `[latitude, longitude]` coordinate pairs.
- `effective` (`string`): ISO 8601 activation timestamp.
- `expires` (`string`): ISO 8601 expiration timestamp.
- `severity` (`string`): Severity label, defaulting to `warning`.
- `threats` (`object`): Module-specific threat metadata.

### `Alerts/EdgeWARN/timestamps/{timestamp}.json`

Backs `GET /api/v3/alert-snapshots/:timestamp?source=edgewarn`.

The on-disk file is a wrapper object, but the API response returns only the `alerts` array from that object. If the timestamp file is absent, the API returns `[]`.

- `timestamp` (`string`): ISO 8601 timestamp for the snapshot time.
- `count` (`number`): Number of active alerts in the snapshot.
- `alerts` (`object[]`): Active alert summaries.

Each item in `alerts` contains:

- `id` (`string`): Alert identifier.
- `severity` (`string`): Severity label.

## METAR

### `METAR/METAR_{YYYYMMDD-HH}z.json`

Used by `GET /api/v3/observations/metar/:timestamp`.

The producer writes an array of parsed observation objects. Common fields are
`observation_time`, `station`, `coordinates`, `wind`, `visibility`,
`temperature`, `dewpoint`, `pressure`, `clouds`, `weather`, and `remarks`.

### API wrapper for METAR responses

The API wraps the underlying METAR file as:

- `type` (`string`): Always `metar`.
- `timestamp` (`string`): Requested timestamp in `YYYYMMDD-HHMMSS` format.
- `data` (`object[]`): Raw observation array from the corresponding
  `METAR_{YYYYMMDD-HH}z.json` file.
