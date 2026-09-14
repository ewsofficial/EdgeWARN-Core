import { afterEach, beforeEach, describe, expect, it } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import request from 'supertest';
import { createApp } from '../../src/api/app.js';
import {
  classifyHeartbeatState,
  requiredServiceForRoute,
  resetServiceStateCache,
} from '../../src/api/services/serviceRegistry.js';

const OPENAPI_PATH = path.resolve('src/api/openapi/v3.yaml');

const STALE_AFTER_SECONDS = 60;

const freshHeartbeat = (overrides = {}) => JSON.stringify({
  schema_version: 1,
  service: 'nexrad',
  pid: 4321,
  run_id: 'run-abc',
  updated_at: new Date().toISOString(),
  phase: 'supervising',
  degraded_children: [],
  ...overrides,
});

async function writeHeartbeat(baseDir, name, payload) {
  const target = path.join(baseDir, 'state', 'realtime', 'services', `${name}.json`);
  await fs.mkdir(path.dirname(target), { recursive: true });
  if (payload === null) {
    await fs.rm(target, { force: true });
    return;
  }
  await fs.writeFile(target, typeof payload === 'string' ? payload : JSON.stringify(payload));
}

describe('service registry scanner', () => {
  let baseDir;
  const now = new Date();

  beforeEach(() => { baseDir = null; });
  afterEach(async () => {
    resetServiceStateCache();
    if (baseDir) await fs.rm(baseDir, { recursive: true, force: true });
  });

  it('classifies disabled, active, stale, degraded, and unsupported-schema states', async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'svc-registry-'));
    const read = async (name) => fs.readFile(path.join(baseDir, 'state', 'realtime', 'services', `${name}.json`), 'utf8').catch(() => null);

    // disabled: no file at all
    expect(classifyHeartbeatState(await read('nexrad'), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now }).state).toBe('disabled');

    // active: fresh well-formed record
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat());
    let result = classifyHeartbeatState(await read('nexrad'), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now });
    expect(result.state).toBe('active');
    expect(result.heartbeat.pid).toBe(4321);

    // stale: updated_at beyond the threshold
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat({ updated_at: new Date(now.getTime() - 5 * 60 * 1000).toISOString() }));
    result = classifyHeartbeatState(await read('nexrad'), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now });
    expect(result.state).toBe('stale');
    expect(result.heartbeat.updatedAt.getTime()).toBe(now.getTime() - 5 * 60 * 1000);

    // degraded: fresh but reporting degraded children
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat({ degraded_children: ['NEXRAD Ingest'] }));
    result = classifyHeartbeatState(await read('nexrad'), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now });
    expect(result.state).toBe('degraded');

    // unsupported-schema: wrong schema version and malformed JSON
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat({ schema_version: 99 }));
    expect(classifyHeartbeatState(await read('nexrad'), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now }).state).toBe('unsupported-schema');
    await writeHeartbeat(baseDir, 'nexrad', '{not json');
    expect(classifyHeartbeatState(await read('nexrad'), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now }).state).toBe('unsupported-schema');

    // a mismatched service field never classifies as that file's service
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat({ service: 'edgewarn' }));
    expect(classifyHeartbeatState(await read('nexrad'), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now }).state).toBe('unsupported-schema');
  });

  it('uses UTC for offset-less ISO timestamps and preserves Python-compatible fields', () => {
    const now = new Date('2026-08-25T12:00:30Z');
    const result = classifyHeartbeatState(freshHeartbeat({
      run_id: 99,
      updated_at: '2026-08-25T12:00:00',
      phase: 7,
      version: 3,
    }), { service: 'nexrad', staleAfterSeconds: STALE_AFTER_SECONDS, now });

    expect(result.state).toBe('active');
    expect(result.heartbeat.updatedAt.toISOString()).toBe('2026-08-25T12:00:00.000Z');
    expect(result.heartbeat.phase).toBe(7);
    expect(result.heartbeat.version).toBe(3);
  });

  it('maps route families to exactly one required service by longest prefix', () => {
    expect(requiredServiceForRoute('/api/v3/radar-sites')).toBe('nexrad');
    expect(requiredServiceForRoute('/api/v3/radar-sites/KTLX/scans/20240101-120000/elevations/0.5/products/DBZH')).toBe('nexrad');
    expect(requiredServiceForRoute('/nexrad/KTLX')).toBe('nexrad');
    expect(requiredServiceForRoute('/api/v3/cells')).toBe('edgewarn');
    expect(requiredServiceForRoute('/api/v3/render-products')).toBe('ewmrs');
    expect(requiredServiceForRoute('/api/v3/models/rap/layers')).toBe('ewmrs');
    expect(requiredServiceForRoute('/api/v3/analyses/wpc/surface')).toBe('ewmrs');
    expect(requiredServiceForRoute('/renders/get-items')).toBe('ewmrs');
    expect(requiredServiceForRoute('/health/ready')).toBeNull();
  });
});

describe('OpenAPI chunk endpoint failures', () => {
  it('documents both service gating and invalid render artifacts as 503 causes', async () => {
    const spec = await fs.readFile(OPENAPI_PATH, 'utf8');
    const openApi = JSON.parse(spec);
    for (const route of [
      '/api/v3/render-products/{productId}/snapshots/{timestamp}/chunks',
      '/api/v3/render-products/{productId}/snapshots/{timestamp}/chunks/{x}/{y}',
    ]) {
      const response = openApi.paths[route].get.responses['503'];
      expect(response.description).toContain('SERVICE_NOT_ENABLED');
      expect(response.description).toContain('INVALID_ARTIFACT');
    }
  });
});

describe('SERVICE_NOT_ENABLED gating for the ewmrs route family', () => {
  let baseDir;
  afterEach(async () => {
    resetServiceStateCache();
    if (baseDir) await fs.rm(baseDir, { recursive: true, force: true });
  });

  async function createAppWithBaseDir() {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'svc-gate-ewmrs-'));
    for (const dir of ['data', 'gui', 'wpc']) {
      await fs.mkdir(path.join(baseDir, dir), { recursive: true });
    }
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    return app;
  }

  it('returns the legacy envelope on /renders and problem+json on v3 render routes when ewmrs is disabled', async () => {
    const app = await createAppWithBaseDir();
    const v3 = await request(app).get('/api/v3/render-products').expect(503).expect('Content-Type', /application\/problem\+json/);
    expect(v3.body).toMatchObject({ code: 'SERVICE_NOT_ENABLED', service: 'ewmrs', state: 'disabled' });

    const legacy = await request(app).get('/renders/get-items').expect(503);
    expect(legacy.body.success).toBe(false);
    expect(legacy.body.error).toMatchObject({ code: 'SERVICE_NOT_ENABLED', service: 'ewmrs', state: 'disabled', last_seen: null });
  });

  it('serves render routes normally when ewmrs heartbeats as active', async () => {
    const app = await createAppWithBaseDir();
    await writeHeartbeat(baseDir, 'ewmrs', freshHeartbeat({ service: 'ewmrs' }));
    await request(app).get('/api/v3/render-products').expect(200);
  });
});

describe('SERVICE_NOT_ENABLED gating for the edgewarn route family', () => {
  let baseDir;
  afterEach(async () => {
    resetServiceStateCache();
    if (baseDir) await fs.rm(baseDir, { recursive: true, force: true });
  });

  async function createAppWithBaseDir() {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'svc-gate-edgewarn-'));
    for (const dir of ['data', 'gui', 'wpc']) {
      await fs.mkdir(path.join(baseDir, dir), { recursive: true });
    }
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    return app;
  }

  it('returns 503 problem+json on analysis routes when the service is disabled', async () => {
    const app = await createAppWithBaseDir();
    for (const endpoint of ['/api/v3/cells', '/api/v3/storm-snapshots', '/api/v3/alert-snapshots']) {
      const response = await request(app).get(endpoint).expect(503).expect('Content-Type', /application\/problem\+json/);
      expect(response.body).toMatchObject({
        code: 'SERVICE_NOT_ENABLED',
        service: 'edgewarn',
        state: 'disabled',
      });
    }
  });

  it('serves analysis routes normally when the service heartbeats as active', async () => {
    const app = await createAppWithBaseDir();
    await writeHeartbeat(baseDir, 'edgewarn', freshHeartbeat({ service: 'edgewarn' }));
    await request(app).get('/api/v3/cells').expect(200);
  });
});

describe('SERVICE_NOT_ENABLED gating for the nexrad route family', () => {
  let baseDir;
  afterEach(async () => {
    resetServiceStateCache();
    if (baseDir) await fs.rm(baseDir, { recursive: true, force: true });
  });

  async function createAppWithBaseDir() {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'svc-gate-'));
    for (const dir of ['data', 'gui', 'wpc']) {
      await fs.mkdir(path.join(baseDir, dir), { recursive: true });
    }
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    return app;
  }

  it('returns 503 problem+json on v3 radar routes when the service is disabled', async () => {
    const app = await createAppWithBaseDir();
    const response = await request(app).get('/api/v3/radar-sites').expect(503).expect('Content-Type', /application\/problem\+json/);
    expect(response.body).toMatchObject({
      type: 'about:blank',
      title: 'Service Not Enabled',
      status: 503,
      code: 'SERVICE_NOT_ENABLED',
      service: 'nexrad',
      state: 'disabled',
      lastSeen: null,
    });
    await request(app).get('/api/v3/radar-sites/KTLX/availability').expect(503);
  });

  it('distinguishes stale from disabled with last_seen evidence', async () => {
    const app = await createAppWithBaseDir();
    const staleTime = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat({ updated_at: staleTime }));
    resetServiceStateCache();

    const legacy = await request(app).get('/nexrad').expect(503);
    expect(legacy.body.success).toBe(false);
    expect(legacy.body.error).toEqual({
      code: 'SERVICE_NOT_ENABLED',
      message: 'Required service is not active',
      service: 'nexrad',
      state: 'stale',
      last_seen: staleTime,
    });
  });

  it('serves radar routes normally when the service heartbeats as active', async () => {
    const app = await createAppWithBaseDir();
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat());
    const response = await request(app).get('/api/v3/radar-sites').expect(200);
    expect(response.body.data).toEqual([]);
  });

  it('degraded services still serve but surface their degraded children', async () => {
    const app = await createAppWithBaseDir();
    await writeHeartbeat(baseDir, 'nexrad', freshHeartbeat({ degraded_children: ['NEXRAD Ingest'] }));
    await request(app).get('/api/v3/radar-sites').expect(200);

    const ready = await request(app).get('/health/ready').expect(200);
    expect(ready.body.services.nexrad).toEqual({
      state: 'degraded',
      phase: 'supervising',
      lastSeen: expect.any(String),
      degradedChildren: ['NEXRAD Ingest'],
    });
    // Readiness keeps its directory-based contract regardless of services.
    expect(ready.body.status).toBe('ready');
  });

  it('reports every canonical service in the health services block', async () => {
    const app = await createAppWithBaseDir();
    const ready = await request(app).get('/health/ready').expect(200);
    expect(Object.keys(ready.body.services).sort()).toEqual(['edgewarn', 'ewmrs', 'nexrad']);
    expect(ready.body.services.edgewarn).toEqual({ state: 'disabled', phase: null, lastSeen: null, degradedChildren: [] });
  });
});
