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
    await request(app).get('/robots.txt').expect(200).expect('Content-Type', /text\/plain/);
    const live = await request(app).get('/health/live').expect(200);
    expect(live.body).toEqual({ status: 'ok', requestId: expect.any(String) });
    expect(JSON.stringify(live.body)).not.toContain(baseDir);
    const ready = await request(app).get('/health/ready').expect(503);
    expect(ready.body).not.toHaveProperty('config');
    expect(JSON.stringify(ready.body)).not.toContain(baseDir);
    const cells = await request(app).get('/api/v3/cells').expect(200);
    expect(cells.body.data).toEqual(['4']);
    expect(cells.body.meta.requestId).toBeUndefined();
    await request(app).get('/api/v3/cells').set('If-None-Match', cells.headers.etag).expect(304);
    await request(app).get('/api/v3/cells?limit=0').expect(400).expect('Content-Type', /application\/problem\+json/);
    await request(app).get('/api/v3/cells?cursor=4&cursor=5').expect(400);
    await request(app).get('/api/v3/cells?unexpected=yes').expect(400);
    await request(app).post('/api/v3/cells').expect(405).expect('Allow', 'GET, HEAD');
    const missing = await request(app).get('/nope').expect(404);
    expect(missing.headers['content-type']).toContain('application/problem+json');
    const discovery = await request(app).get('/api/v3').expect(200);
    const discoverySchema = openApi.components.schemas.ResourceEnvelope;
    const problemSchema = openApi.components.schemas.Problem;
    expectObjectToMatchSchema(discovery.body, discoverySchema);
    expectObjectToMatchSchema(missing.body, problemSchema);
  });

  it('serves promoted StormProb leads without private model inputs', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'stormprob-api-'));
    const snapshotDir = path.join(baseDir, 'data', 'stormcells');
    const serviceDir = path.join(baseDir, 'state', 'realtime', 'services');
    await fs.mkdir(snapshotDir, { recursive: true });
    await fs.mkdir(serviceDir, { recursive: true });
    await fs.writeFile(path.join(serviceDir, 'edgewarn.json'), JSON.stringify({
      schema_version: 1, service: 'edgewarn', pid: 1, run_id: 'test-run',
      updated_at: new Date().toISOString(), phase: 'cycling', degraded_children: []
    }));
    const leads = [15, 30, 45, 60].map((lead) => ({
      lead_minutes: lead, model_version: 'stormprob/v1', status: 'ok',
      east_km: lead / 3, north_km: 0, predicted_centroid: [35, 265 + lead / 300],
      polygon: { type: 'MultiPolygon', coordinates: [[[[265, 35], [265.01, 35], [265.01, 35.01], [265, 35]]]] },
      probability_threshold: 0.25,
      metadata: { geometry_kind: 'instantaneous-probability-contour' }
    }));
    await fs.writeFile(path.join(snapshotDir, 'stormcells_20260317-200000.json'),
      JSON.stringify({ features: [{ id: 4, modules: { StormProb: {
        status: 'success', model_version: 'stormprob/v1', leads
      } } }] }));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir,
      RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const response = await request(app).get('/api/v3/storm-snapshots/20260317-200000').expect(200);
    const cell = response.body.data.cells.features[0];
    expect(cell.stormprob).toBeUndefined();
    expect(cell.modules.StormProb.leads.map((lead) => lead.lead_minutes)).toEqual([15, 30, 45, 60]);
    expect(cell.modules.StormProb.leads.every((lead) => lead.metadata.geometry_kind === 'instantaneous-probability-contour')).toBe(true);
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

  

  it('masks the exact version in production on all version surfaces', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-version-'));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0', NODE_ENV: 'production' }, argv: [] });
    const root = await request(app).get('/').expect(200);
    expect(root.body.version).toBe('2.x');
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
    await request(app).get('/health/live').expect(429);
  });

  it('returns 404 for every removed legacy route, including former 410 handlers', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'unified-api-health-'));
    await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory))));
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const paths = [
      '/api/v2', '/api/v2/features/cells', '/api/v2/features/cells?id=4',
      '/api/v2/features/timestamps', '/api/v2/features/timestamps?timestamp=20260317-200000',
      '/api/v2/features/alerts/official', '/api/v2/features/alerts/edgewarn?id=4',
      '/api/v2/data/metar', '/api/v2/data/metar?timestamp=20260317-200000',
      '/renders/get-items', '/renders/fetch?product=MRMS_MergedReflectivityQC',
      '/renders/download', '/renders/tile', '/renders/tile-info',
      '/nexrad', '/nexrad/KTLH', '/nexrad/KTLH/20260317-200000/0.5?product=DBZH',
      '/rap/layers', '/rap/fetch', '/rap/metadata', '/rap/data',
      '/wpc/fetch?type=sfc', '/wpc/download?type=sfc', '/colormaps',
      '/health', '/healthz', '/features', '/features/cells', '/data', '/data/metar',
      '/api/v1', '/api/v1/features',
    ];
    for (const endpoint of paths) {
      for (const method of ['get', 'head', 'post', 'put', 'patch', 'delete']) {
        const response = await request(app)[method](endpoint).expect(404);
        expect(response.headers['content-type']).toContain('application/problem+json');
        expect(response.headers.deprecation).toBeUndefined();
        expect(response.headers.location).toBeUndefined();
      }
    }
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
