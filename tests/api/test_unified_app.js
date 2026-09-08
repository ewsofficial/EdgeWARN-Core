import { afterEach, describe, expect, it, jest } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import request from 'supertest';
import { createApp } from '../../src/api/app.js';

const openApi = JSON.parse(await fs.readFile(new URL('../../src/api/openapi/v3.yaml', import.meta.url), 'utf8'));

function expectObjectToMatchSchema(value, schema) {
  expect(value).not.toBeNull();
  expect(Array.isArray(value)).toBe(false);
  expect(typeof value).toBe('object');
  for (const required of schema.required || []) expect(value).toHaveProperty(required);
  for (const [name, property] of Object.entries(schema.properties || {})) {
    if (!(name in value)) continue;
    if (property.type === 'integer') expect(Number.isInteger(value[name])).toBe(true);
    else expect(typeof value[name]).toBe(property.type);
  }
  if (schema.additionalProperties === false) {
    expect(Object.keys(value).sort()).toEqual(Object.keys(schema.properties).filter((key) => key in value).sort());
  }
}

describe('unified API app', () => {
  let baseDir;
  afterEach(async () => { if (baseDir) await fs.rm(baseDir, { recursive: true, force: true }); });

  it('serves discovery, health, v3 envelopes, and redacted problems', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-'));
    await fs.mkdir(path.join(baseDir, 'data', 'cells'), { recursive: true });
    await fs.mkdir(path.join(baseDir, 'gui'), { recursive: true });
    await fs.mkdir(path.join(baseDir, 'wpc'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'data', 'cells', 'cell_index.json'), '{"cellIds":["4"]}');
    await fs.writeFile(path.join(baseDir, 'data', 'cells', '4.json'), '{"id":4}');
    // Analysis route families require an active `edgewarn` heartbeat (Phase 5).
    await fs.mkdir(path.join(baseDir, 'state', 'realtime', 'services'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'state', 'realtime', 'services', 'edgewarn.json'), JSON.stringify({ schema_version: 1, service: 'edgewarn', pid: 1, run_id: 'test-run', updated_at: new Date().toISOString(), phase: 'cycling', degraded_children: [] }));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const root = await request(app).get('/').expect(200);
    expect(root.body.links.api).toBe('/api/v3');
    expect(root.headers['strict-transport-security']).toBe('max-age=31536000; includeSubDomains');
    await request(app).get('/api/v2').expect(200).expect((response) => expect(response.body.version).toBe(root.body.version));
    await request(app).get('/robots.txt').expect(200).expect('Content-Type', /text\/plain/);
    const live = await request(app).get('/health/live').expect(200);
    expect(live.body.config).toMatchObject({
      source: { schemaVersion: 1 },
      overrides: ['EDGEWARN_BASE_DIR', 'RATE_LIMIT_MAX_SEC', 'RATE_LIMIT_MAX_MIN'],
      restartRequired: true,
      effective: { baseDir, renderProductCount: 31, radarProductCount: 7 },
    });
    const ready = await request(app).get('/health/ready').expect(200);
    expect(ready.body.config).toEqual(live.body.config);
    const cells = await request(app).get('/api/v3/cells').expect(200);
    expect(cells.body.data).toEqual(['4']);
    expect(cells.body.meta.requestId).toBeUndefined();
    await request(app).get('/api/v3/cells').set('If-None-Match', cells.headers.etag).expect(304);
    await request(app).get('/api/v3/cells?limit=0').expect(400).expect('Content-Type', /application\/problem\+json/);
    await request(app).get('/api/v3/cells?cursor=4&cursor=5').expect(400);
    await request(app).get('/api/v3/cells?unexpected=yes').expect(400);
    await request(app).post('/api/v3/cells').expect(405).expect('Allow', 'GET, HEAD');
    const legacyCells = await request(app).get('/api/v2/features/cells').expect(200);
    expect(legacyCells.body).toEqual(['4']);
    expect(legacyCells.headers.deprecation).toBe('true');
    await request(app).get('/api/v1').expect(410);
    await request(app).get('/api/v1/features').expect(410);
    const missing = await request(app).get('/nope').expect(404);
    expect(missing.headers['content-type']).toContain('application/problem+json');
    const discovery = await request(app).get('/api/v3').expect(200);
    const discoverySchema = openApi.components.schemas.ResourceEnvelope;
    const problemSchema = openApi.components.schemas.Problem;
    expectObjectToMatchSchema(discovery.body, discoverySchema);
    expectObjectToMatchSchema(missing.body, problemSchema);
  });

  it('serves every v3 resource family from one configured runtime tree', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-all-'));
    const write = async (relative, contents) => { const target = path.join(baseDir, relative); await fs.mkdir(path.dirname(target), { recursive: true }); await fs.writeFile(target, contents); };
    await write('data/stormcells/stormcell_index.json', '{"timestamps":["20260317-200000"]}');
    await write('data/stormcells/stormcells_20260317-200000.json', '[]');
    await write('data/Alerts/official/timestamps/20260317-200000.json', '{"alerts":[]}');
    await write('data/METAR/METAR_20260317-20z.json', '[]');
    const renderFormat = { version: 2, encoding: 'float16', file_suffix: '.f16.gz', compression: 'gzip', channels: 1, value_kind: 'scalar', no_data: 'nan', bytes_per_component: 2, pixel_row_order: 'top_to_bottom', grid_origin: 'bottom_left' };
    await write('gui/CompRefQC/index.json', JSON.stringify({ schema_version: 2, timestamps: ['20260317-200000'], representation: 'binary_chunks', chunk_format: { ...renderFormat, media_type: 'application/octet-stream' }, tile_grid: { rows: 1, cols: 1, tile_size: 2 } }));
    await write('gui/CompRefQC/20260317-200000/index.json', JSON.stringify({ schema_version: 2, timestamp: '20260317-200000', representation: 'binary_chunks', chunk_format: renderFormat, tile_grid: { rows: 1, cols: 1, tile_size: 2 }, chunks: [[0, 0]] }));
    await write('gui/CompRefQC/20260317-200000/chunks/chunk_0_0.f16.gz', Buffer.from('H4sIAAAAAAAC/2NggAAAad8iZQgAAAA=', 'base64'));
    await write('gui/NEXRAD/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz', 'gzip');
    // Route families require an active service heartbeat for their owner
    // (decomposition Phases 3-5); without them these routes return 503.
    const heartbeat = (service) => JSON.stringify({ schema_version: 1, service, pid: 1, run_id: 'test-run', updated_at: new Date().toISOString(), phase: 'supervising', degraded_children: [] });
    await write('state/realtime/services/nexrad.json', heartbeat('nexrad'));
    await write('state/realtime/services/ewmrs.json', heartbeat('ewmrs'));
    await write('state/realtime/services/edgewarn.json', heartbeat('edgewarn'));
    await write('gui/RAP/CAPE/index.json', '["20260317-200000"]');
    await write('gui/RAP/CAPE/20260317-200000/metadata.json', '{"units":"J/kg","grid":{"ni":1,"nj":1}}');
    await write('gui/RAP/CAPE/20260317-200000/data.u16', 'u16');
    await write('wpc/surface_analysis/wpc_sfc_20260317-200000.geojson', '{"type":"FeatureCollection","features":[]}');
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const paths = [
      '/api/v3/storm-snapshots', '/api/v3/storm-snapshots/20260317-200000',
      '/api/v3/alert-snapshots?source=official', '/api/v3/observations/metar',
      '/api/v3/render-products', '/api/v3/render-products/comp-ref-qc/snapshots',
      '/api/v3/render-products/comp-ref-qc/snapshots/20260317-200000/chunks',
      '/api/v3/radar-sites', '/api/v3/radar-sites/KTLH/availability',
      '/api/v3/models/rap/layers', '/api/v3/models/rap/layers/CAPE/snapshots',
      '/api/v3/models/rap/layers/CAPE/snapshots/20260317-200000/metadata',
      '/api/v3/models/rap/layer-mappings', '/api/v3/analyses/wpc/surface',
      '/api/v3/analyses/wpc/surface/20260317-200000', '/api/v3/styles/colormaps'
    ];
    for (const endpoint of paths) await request(app).get(endpoint).expect(200);
    const chunk = await request(app).get('/api/v3/render-products/comp-ref-qc/snapshots/20260317-200000/chunks/0/0').expect(200).expect('Content-Type', /application\/octet-stream/);
    expect(chunk.headers['cache-control']).toContain('immutable');
    expect(chunk.headers['x-data-type']).toBe('float16');
    expect(chunk.headers['content-length']).toBe('23');
    await request(app).head('/api/v3/render-products/comp-ref-qc/snapshots/20260317-200000/chunks/0/0').expect(200).expect('Content-Length', '23');
    await request(app).get('/api/v3/render-products/comp-ref-qc/snapshots/20260317-200000/chunks/0/0').set('If-None-Match', chunk.headers.etag).expect(304);
    await request(app).get('/api/v3/radar-sites/KTLH/scans/20260317-200000/elevations/0.5/products/DBZH').expect(200).expect('Content-Type', /application\/gzip/);
    const rap = await request(app).get('/api/v3/models/rap/layers/CAPE/snapshots/20260317-200000/data').expect(200);
    expect(rap.headers['x-units']).toBe('J/kg');
  });

  it('paginates numeric cell IDs with string cursors', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-pages-'));
    await fs.mkdir(path.join(baseDir, 'data', 'cells'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'data', 'cells', 'cell_index.json'), JSON.stringify({ cellIds: Array.from({ length: 120 }, (_, i) => i + 1) }));
    await fs.mkdir(path.join(baseDir, 'state', 'realtime', 'services'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'state', 'realtime', 'services', 'edgewarn.json'), JSON.stringify({ schema_version: 1, service: 'edgewarn', pid: 1, run_id: 'test-run', updated_at: new Date().toISOString(), phase: 'cycling', degraded_children: [] }));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const first = await request(app).get('/api/v3/cells?limit=100').expect(200);
    expect(first.body.data).toHaveLength(100);
    expect(first.body.meta.nextCursor).toBe('100');
    const second = await request(app).get(`/api/v3/cells?limit=100&cursor=${first.body.meta.nextCursor}`).expect(200);
    expect(second.body.data).toEqual(Array.from({ length: 20 }, (_, i) => 101 + i));
    expect(second.body.meta.nextCursor).toBeNull();
  });

  it('serves WPC surface analyses as native GeoJSON', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-wpc-'));
    await fs.mkdir(path.join(baseDir, 'wpc', 'surface_analysis'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'wpc', 'surface_analysis', 'wpc_sfc_20260317-200000.geojson'), '{"type":"FeatureCollection","features":[]}');
    // WPC analyses are an EWMRS-owned route family (Phase 4 gating).
    await fs.mkdir(path.join(baseDir, 'state', 'realtime', 'services'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'state', 'realtime', 'services', 'ewmrs.json'), JSON.stringify({ schema_version: 1, service: 'ewmrs', pid: 1, run_id: 'test-run', updated_at: new Date().toISOString(), phase: 'supervising', degraded_children: [] }));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const wpc = await request(app).get('/api/v3/analyses/wpc/surface/20260317-200000').expect(200).expect('Content-Type', /application\/geo\+json/);
    expect(wpc.body).toEqual({ type: 'FeatureCollection', features: [] });
  });

  it('masks the exact version in production on all version surfaces', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-version-'));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0', NODE_ENV: 'production' }, argv: [] });
    const root = await request(app).get('/').expect(200);
    expect(root.body.version).toBe('2.x');
    const v2 = await request(app).get('/api/v2').expect(200);
    expect(v2.body.version).toBe('2.x');
  });

  it('allows configured CORS origins and rejects other origins independently', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-security-'));
    await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory))));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, ALLOWED_ORIGINS: 'https://console.example', RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const cors = await request(app).get('/').set('Origin', 'https://console.example').expect(200);
    expect(cors.headers['access-control-allow-origin']).toBe('https://console.example');
    expect(cors.headers['access-control-allow-credentials']).toBeUndefined();
    const denied = await request(app).get('/').set('Origin', 'https://other.example').expect(403).expect('Content-Type', /application\/problem\+json/);
    expect(denied.body).toMatchObject({ status: 403, code: 'CORS_ORIGIN_DENIED', title: 'Forbidden' });
  });

  it('answers allowed CORS preflight with configured methods', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-preflight-'));
    await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory))));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, ALLOWED_ORIGINS: 'https://console.example', RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const response = await request(app).options('/api/v3').set('Origin', 'https://console.example').set('Access-Control-Request-Method', 'GET').expect(204);
    expect(response.headers['access-control-allow-origin']).toBe('https://console.example');
    expect(response.headers['access-control-allow-methods']).toContain('GET');
  });

  it('rate limits independently of CORS and route aliases', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-rate-limit-'));
    await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory))));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '1', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    await request(app).get('/').expect(200);
    await request(app).get('/health').expect(429);
  });

  it('preserves distinct legacy health response shapes', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-health-'));
    await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory))));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const edgewarn = await request(app).get('/health').expect(200);
    expect(edgewarn.body).toMatchObject({ status: 'OK' });
    expect(edgewarn.body.timestamp).toEqual(expect.any(String));
    await request(app).get('/healthz').expect(200).expect({ ok: true });
  });

  it('logs a template route without request query data', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-log-'));
    await fs.mkdir(path.join(baseDir, 'data', 'cells'), { recursive: true });
    await Promise.all(['gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory))));
    await fs.writeFile(path.join(baseDir, 'data', 'cells', 'cell_index.json'), '{"cellIds":["4"]}');
    const log = jest.spyOn(console, 'info').mockImplementation(() => {});
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    await request(app).get('/api/v3/cells?secret=not-logged').expect(400);
    expect(JSON.parse(log.mock.calls[0][0])).toMatchObject({ event: 'api_access', method: 'GET', route: '/api/v3/cells', status: 400 });
    expect(log.mock.calls[0][0]).not.toContain('not-logged');
    log.mockRestore();
  });
});
