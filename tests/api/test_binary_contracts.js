import { afterAll, beforeAll, describe, expect, it } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import zlib from 'zlib';
import request from 'supertest';
import { createApp } from '../../src/api/app.js';

// Phase 9: the Python production encoders emit the committed inventory under
// tests/fixtures/serialization/ (regenerate with
// `PYTHONPATH=src python tests/fixtures/serialization/generate_fixtures.py`).
// These cases serve those exact artifacts through the real Express stack and
// independently validate headers plus raw-byte passthrough -- they never share
// any decoding implementation with the Python suite.

const FIXTURES = new URL('../fixtures/serialization/', import.meta.url);
const TIMESTAMP = '20260317-200000';

// [fixture-relative source, runtime-tree destination]
const COPIES = [
  ['RAP/CAPE/index.json', 'gui/RAP/CAPE/index.json'],
  ['RAP/CAPE/20260317-200000/metadata.json', 'gui/RAP/CAPE/20260317-200000/metadata.json'],
  ['RAP/CAPE/20260317-200000/data.u16', 'gui/RAP/CAPE/20260317-200000/data.u16'],
  ['render/CompRefQC/index.json', 'gui/CompRefQC/index.json'],
  ['render/CompRefQC/20260317-200000/index.json', 'gui/CompRefQC/20260317-200000/index.json'],
  ['render/CompRefQC/20260317-200000/chunks/chunk_0_0.f16.gz', 'gui/CompRefQC/20260317-200000/chunks/chunk_0_0.f16.gz'],
  ['nexrad/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz', 'gui/NEXRAD/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz'],
  ['wpc/surface_analysis/wpc_sfc_20260317-200000.geojson', 'wpc/surface_analysis/wpc_sfc_20260317-200000.geojson'],
];

const fixtureBytes = (relative) => fs.readFile(new URL(relative, FIXTURES));
const fixtureJson = async (relative) => JSON.parse(await fs.readFile(new URL(relative, FIXTURES), 'utf8'));

// IEEE-754 binary16 -> float64. Buffer has no half-precision read, so the
// independent struct decode converts each half word explicitly instead of
// misreading the payload with float32 reads.
const halfToFloat = (bits) => {
  const sign = bits & 0x8000 ? -1 : 1;
  const exponent = (bits >> 10) & 0x1f;
  const fraction = bits & 0x3ff;
  if (exponent === 0) return sign * (fraction / 1024) * 2 ** -14;
  if (exponent === 0x1f) return fraction === 0 ? sign * Infinity : NaN;
  return sign * (1 + fraction / 1024) * 2 ** (exponent - 15);
};

describe('binary producer/consumer contracts through the real API', () => {
  let app;
  let baseDir;

  beforeAll(async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'binary-contracts-'));
    for (const [source, destination] of COPIES) {
      const target = path.join(baseDir, destination);
      await fs.mkdir(path.dirname(target), { recursive: true });
      await fs.copyFile(new URL(source, FIXTURES), target);
    }
    // v3 owner gates require an active heartbeat per service (decomposition
    // phases 3-5); without them the ewmrs/nexrad routes return 503.
    const heartbeat = (service) => JSON.stringify({ schema_version: 1, service, pid: 1, run_id: 'test-run', updated_at: new Date().toISOString(), phase: 'supervising', degraded_children: [] });
    await fs.mkdir(path.join(baseDir, 'state', 'realtime', 'services'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'state', 'realtime', 'services', 'ewmrs.json'), heartbeat('ewmrs'));
    await fs.writeFile(path.join(baseDir, 'state', 'realtime', 'services', 'nexrad.json'), heartbeat('nexrad'));
    ({ app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] }));
  });

  afterAll(async () => {
    if (baseDir) await fs.rm(baseDir, { recursive: true, force: true });
  });

  it('serves RAP uint16 bytes with the derived metadata headers and passes them through untouched', async () => {
    const reference = await fixtureBytes('RAP/CAPE/20260317-200000/data.u16');
    const metadata = await fixtureJson('RAP/CAPE/20260317-200000/metadata.json');

    const dataUrl = `/api/v3/models/rap/layers/CAPE/snapshots/${TIMESTAMP}/data`;
    const response = await request(app).get(dataUrl).expect(200).expect('Content-Type', /application\/octet-stream/);
    expect(response.headers['x-data-type']).toBe('uint16');
    expect(response.headers['x-byte-order']).toBe('little_endian');
    expect(response.headers['x-missing-value']).toBe(String(metadata.missing_value));
    expect(response.headers['x-grid-ni']).toBe('3');
    expect(response.headers['x-grid-nj']).toBe('2');
    expect(response.headers['x-scale-min']).toBe('0');
    expect(response.headers['x-scale-max']).toBe('100');
    expect(response.headers['x-units']).toBe('J/kg');
    expect(response.headers['content-length']).toBe(String(reference.length));
    expect(Buffer.compare(Buffer.from(response.body), reference)).toBe(0);

    // Independent little-endian struct read: shape [2,3] from the metadata,
    // scale maps 0..100 to 0..65535, NaN reserves 65535.
    const grid = Buffer.from(response.body);
    expect(metadata.shape).toEqual([2, 3]);
    expect(metadata.dtype).toBe('uint16');
    expect(metadata.byte_order).toBe('little_endian');
    expect(grid.readUInt16LE(0)).toBe(0); // 0.0 -> scale min
    // 100.0 saturates at valid_max (65534); 65535 is reserved for nodata --
    // see the `uint16` block in config/ewmrs_pipeline.yaml.
    expect(grid.readUInt16LE(4)).toBe(65534); // 100.0 -> scale max
    expect(grid.readUInt16LE(6)).toBe(65535); // NaN -> nodata
    expect(grid.length).toBe(2 * 3 * 2);

    // The legacy v2-style adapter must serve the identical bytes.
    const legacy = await request(app)
      .get('/rap/data')
      .query({ layer: 'CAPE', timestamp: TIMESTAMP })
      .expect(200)
      .expect('Content-Type', /application\/octet-stream/);
    expect(Buffer.compare(Buffer.from(legacy.body), reference)).toBe(0);
    expect(legacy.headers.deprecation).toBe('true');
  });

  it('serves RAP JSON metadata as the envelope and honors the metadata route contract', async () => {
    const metadata = await fixtureJson('RAP/CAPE/20260317-200000/metadata.json');
    const response = await request(app)
      .get(`/api/v3/models/rap/layers/CAPE/snapshots/${TIMESTAMP}/metadata`)
      .expect(200)
      .expect('Content-Type', /application\/json/);
    expect(response.body.data).toEqual(metadata);
    expect(response.body.data.grid).toEqual({ ni: 3, nj: 2, point_count: 6 });
  });

  it('serves the float16 chunk gzip payload with float16/no-data headers', async () => {
    const reference = await fixtureBytes('render/CompRefQC/20260317-200000/chunks/chunk_0_0.f16.gz');
    const response = await request(app)
      .get(`/api/v3/render-products/comp-ref-qc/snapshots/${TIMESTAMP}/chunks/0/0`)
      .expect(200)
      .expect('Content-Type', /application\/octet-stream/);
    expect(response.headers['x-data-type']).toBe('float16');
    expect(response.headers['x-value-kind']).toBe('scalar');
    expect(response.headers['x-channel-count']).toBe('1');
    expect(response.headers['x-no-data']).toBe('nan');
    expect(response.headers['x-chunk-width']).toBe('2');
    expect(response.headers['x-chunk-height']).toBe('2');
    expect(response.headers['content-encoding']).toBe('gzip');
    // superagent transparently gunzips (content-encoding: gzip), so the
    // passthrough check compares against the decompressed reference bytes.
    expect(Buffer.compare(Buffer.from(response.body), zlib.gunzipSync(reference))).toBe(0);

    // Independent decode: headerless tight float16 payload, NaN as nodata.
    // Decoded at the half-word level (IEEE-754 binary16 bit patterns), not
    // with float32 reads, so a width mismatch cannot hide here.
    const payload = zlib.gunzipSync(reference);
    expect(payload.length).toBe(2 * 2 * 2); // no header, 2 bytes per component
    expect(payload.readUInt16LE(0)).toBe(0x4920); // 10.25
    expect(payload.readUInt16LE(2)).toBe(0x7e00); // NaN nodata
    expect(payload.readUInt16LE(4)).toBe(0x3c00); // 1.0
    expect(payload.readUInt16LE(6)).toBe(0x4000); // 2.0
  });

  it('serves the NEXRAD variable bin gzip bytes and decodes its documented layout', async () => {
    const reference = await fixtureBytes('nexrad/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz');
    const url = `/api/v3/radar-sites/KTLH/scans/${TIMESTAMP}/elevations/0.5/products/DBZH`;
    const response = await request(app).get(url).expect(200).expect('Content-Type', /application\/gzip/);
    expect(Buffer.compare(Buffer.from(response.body), reference)).toBe(0);

    // Independent struct decode: magic, counts, float16 grid, float32 axes.
    const stream = zlib.gunzipSync(reference);
    expect(stream.subarray(0, 8).toString('latin1')).toBe('EWFFv1S0');
    let offset = 8;
    const azimuthLines = stream.readUInt32LE(offset); offset += 4;
    const rangeGates = stream.readUInt32LE(offset); offset += 4;
    expect([azimuthLines, rangeGates]).toEqual([2, 3]);
    const values = [];
    for (let i = 0; i < azimuthLines * rangeGates; i += 1) values.push(halfToFloat(stream.readUInt16LE(offset + i * 2))); offset += azimuthLines * rangeGates * 2;
    expect(values.slice(0, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(Number.isNaN(values[5])).toBe(true);
    const azimuths = [];
    for (let i = 0; i < azimuthLines; i += 1) azimuths.push(stream.readFloatLE(offset + i * 4)); offset += azimuthLines * 4;
    const ranges = [];
    for (let i = 0; i < rangeGates; i += 1) ranges.push(stream.readFloatLE(offset + i * 4));
    expect(azimuths).toEqual([0.5, 1.5]);
    expect(ranges).toEqual([125, 250, 375]);

    // The legacy NEXRAD adapter serves the same artifact.
    const legacy = await request(app)
      .get(`/nexrad/KTLH/${TIMESTAMP}/0.5`)
      .query({ product: 'DBZH' })
      .expect(200)
      .expect('Content-Type', /application\/gzip/);
    expect(Buffer.compare(Buffer.from(legacy.body), reference)).toBe(0);
  });

  it('serves the WPC GeoJSON generated by the Python parser and converter', async () => {
    const reference = await fixtureJson('wpc/surface_analysis/wpc_sfc_20260317-200000.geojson');
    const response = await request(app)
      .get(`/api/v3/analyses/wpc/surface/${TIMESTAMP}`)
      .expect(200)
      .expect('Content-Type', /geo\+json/);
    expect(response.body).toEqual(reference);
    expect(response.body.type).toBe('FeatureCollection');
    expect(response.body.properties.valid_time).toBe('2026-03-17T20:00:00+00:00');
    const types = response.body.features.map((feature) => feature.geometry.type);
    expect(types.filter((type) => type === 'LineString')).toHaveLength(5);
    expect(types.filter((type) => type === 'Point')).toHaveLength(3);

    const legacy = await request(app)
      .get('/wpc/download')
      .query({ type: 'sfc', timestamp: TIMESTAMP })
      .expect(200);
    expect(legacy.body).toEqual(reference);
  });
});