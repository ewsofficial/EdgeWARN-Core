import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import request from 'supertest';
import { jest } from '@jest/globals';
import { createApp } from '../../src/api/app.js';
import { ArtifactRepository } from '../../src/api/repositories/artifactRepository.js';
import { createModulesService } from '../../src/api/services/modules.js';

const limits = { json: 2_000_000, binary: 2_000_000, image: 2_000_000 };
const cache = { max_entries: 10, max_size_bytes: 4_000_000 };

function heartbeat(service = 'edgewarn') {
  return JSON.stringify({ schema_version: 1, service, pid: 1, run_id: 'test', updated_at: new Date().toISOString(), phase: 'cycling', degraded_children: [] });
}

async function makeBase({ modules, payloads = {}, heartbeatService = 'edgewarn' } = {}) {
  const baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'ctam-modules-phase7-'));
  await Promise.all(['data/ctam/public', 'gui', 'wpc', 'state/realtime/services'].map((e) => fs.mkdir(path.join(baseDir, e), { recursive: true })));
  if (heartbeatService) {
    await fs.writeFile(path.join(baseDir, 'state/realtime/services/edgewarn.json'), heartbeat(heartbeatService));
  }
  if (modules) {
    await fs.writeFile(path.join(baseDir, 'data/ctam/public/registry.json'), JSON.stringify({ schema_version: 1, modules }));
    for (const [rel, value] of Object.entries(payloads)) {
      const target = path.join(baseDir, 'data/ctam/public', rel);
      await fs.mkdir(path.dirname(target), { recursive: true });
      await fs.writeFile(target, typeof value === 'string' ? value : JSON.stringify(value));
    }
  }
  return baseDir;
}

const mod = (id, routes) => ({ id, name: `${id} name`, version: '1.0.0', href: `/api/v3/modules/${id}`, routes });
const route = (mid, rid, available = true) => ({ id: rid, description: `${rid} desc`, href: `/api/v3/modules/${mid}/${rid}`, available });
const wrapper = (mid, rid, data = { ok: true }) => ({ schema_version: 1, module_id: mid, module_version: '1.0.0', route_id: rid, cycle_id: '20260915-120000', published_at: '2026-09-15T12:00:05Z', data });

describe('CTAM public module routes phase 7', () => {
  let baseDir;
  afterEach(async () => { if (baseDir) { await fs.rm(baseDir, { recursive: true, force: true }); baseDir = undefined; } });

  it('paginates module discovery with standard collection envelopes and cache headers', async () => {
    const modules = Array.from({ length: 5 }, (_, i) => mod(`mod${i}`, [route(`mod${i}`, 'summary')]));
    const payloads = {};
    for (const m of modules) payloads[`modules/${m.id}/summary.json`] = wrapper(m.id, 'summary');
    baseDir = await makeBase({ modules, payloads });
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const first = await request(app).get('/api/v3/modules?limit=2').expect(200);
    expect(first.body.data).toHaveLength(2);
    expect(first.body.meta.nextCursor).toBeTruthy();
    expect(first.headers['cache-control']).toMatch(/public/);
    const second = await request(app).get(`/api/v3/modules?limit=2&cursor=${first.body.meta.nextCursor}`).expect(200);
    expect(second.body.data).toHaveLength(2);
    // Module descriptor uses the resource envelope with cache headers.
    const detail = await request(app).get('/api/v3/modules/mod0').expect(200).expect('Cache-Control', /public/);
    expect(detail.body.data).toMatchObject({ id: 'mod0', href: '/api/v3/modules/mod0' });
    expect(detail.body).toHaveProperty('meta');
    await request(app).head('/api/v3/modules').expect(200);
    await request(app).head('/api/v3/modules/mod0').expect(200);
  });

  it('distinguishes unknown modules/routes (404) from declared-but-unavailable (503)', async () => {
    const modules = [mod('cellstats', [route('cellstats', 'summary', true), route('cellstats', 'pending', false)])];
    baseDir = await makeBase({ modules, payloads: { 'modules/cellstats/summary.json': wrapper('cellstats', 'summary', { risk: 'elevated' }) } });
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    await request(app).get('/api/v3/modules/ghost').expect(404);
    await request(app).get('/api/v3/modules/cellstats/ghost').expect(404);
    await request(app).get('/api/v3/modules/ghost/summary').expect(404);
    const unavailable = await request(app).get('/api/v3/modules/cellstats/pending').expect(503);
    expect(unavailable.body.code).toBe('MODULE_ROUTE_UNAVAILABLE');
    const ok = await request(app).get('/api/v3/modules/cellstats/summary').expect(200);
    expect(ok.body).toMatchObject({ data: { risk: 'elevated' }, meta: { moduleId: 'cellstats', routeId: 'summary', cycleId: '20260915-120000' } });
  });

  it('gates all three routes behind the EdgeWARN service', async () => {
    const modules = [mod('cellstats', [route('cellstats', 'summary')])];
    baseDir = await makeBase({ modules, payloads: { 'modules/cellstats/summary.json': wrapper('cellstats', 'summary') }, heartbeatService: null });
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    for (const p of ['/api/v3/modules', '/api/v3/modules/cellstats', '/api/v3/modules/cellstats/summary']) {
      const res = await request(app).get(p).expect(503);
      expect(res.body.code).toBe('SERVICE_NOT_ENABLED');
    }
  });

  it('rejects traversal, encoded separators, invalid queries, and non-GET methods', async () => {
    const modules = [mod('cellstats', [route('cellstats', 'summary')])];
    baseDir = await makeBase({ modules, payloads: { 'modules/cellstats/summary.json': wrapper('cellstats', 'summary') } });
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    // Invalid queries on the collection.
    await request(app).get('/api/v3/modules?unexpected=yes').expect(400);
    await request(app).get('/api/v3/modules?limit=nope').expect(400);
    await request(app).get('/api/v3/modules?limit=2&limit=3').expect(400);
    // Encoded traversal / separators never resolve to a module.
    for (const bad of ['%2e%2e', '..', '.', '%2F', '%00', 'a%2Fb', 'a%5Cb']) {
      await request(app).get(`/api/v3/modules/${bad}`).expect((res) => expect([400, 404]).toContain(res.status));
    }
    await request(app).get('/api/v3/modules/cellstats/%2e%2e').expect((res) => expect([400, 404]).toContain(res.status));
    // Read-only policy on all three routes.
    for (const target of ['/api/v3/modules', '/api/v3/modules/cellstats', '/api/v3/modules/cellstats/summary']) {
      for (const method of ['post', 'put', 'patch', 'delete']) {
        await request(app)[method](target).expect(405).expect('Allow', 'GET, HEAD');
      }
    }
  });

  it('treats registry/payload mismatch, malformed, and oversized artifacts as 503', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ctam-modules-artifact-'));
    try {
      await fs.mkdir(path.join(root, 'ctam', 'public', 'modules', 'cellstats'), { recursive: true });
      const service = (fresh = false) => createModulesService(new ArtifactRepository({ data: root }, limits, cache, 100));
      const modules = [mod('cellstats', [route('cellstats', 'summary')])];
      await fs.writeFile(path.join(root, 'ctam', 'public', 'registry.json'), JSON.stringify({ schema_version: 1, modules }));
      // Mismatched ownership.
      await fs.writeFile(path.join(root, 'ctam', 'public', 'modules', 'cellstats', 'summary.json'), JSON.stringify(wrapper('other', 'summary')));
      await expect(service().getRoute('cellstats', 'summary')).rejects.toMatchObject({ code: 'INVALID_ARTIFACT' });
      // Malformed JSON surfaces as a 503 (IN_PROGRESS from the repository).
      await fs.writeFile(path.join(root, 'ctam', 'public', 'modules', 'cellstats', 'summary.json'), '{not-json');
      await expect(service().getRoute('cellstats', 'summary')).rejects.toMatchObject({ status: 503 });
      // Oversized artifact is rejected by the repository limit.
      const big = 'x'.repeat(2_000_001);
      await fs.writeFile(path.join(root, 'ctam', 'public', 'modules', 'cellstats', 'summary.json'), JSON.stringify(wrapper('cellstats', 'summary', { blob: big })));
      await expect(service().getRoute('cellstats', 'summary')).rejects.toMatchObject({ status: 503 });
      // Malformed registry is also a 503, never module-controlled output.
      await fs.writeFile(path.join(root, 'ctam', 'public', 'registry.json'), '{bad');
      await expect(service().listModules()).rejects.toMatchObject({ status: 503 });
    } finally {
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('refuses symlinked registry entries and payloads', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ctam-modules-symlink-'));
    try {
      const modules = [mod('cellstats', [route('cellstats', 'summary')])];
      const realDir = path.join(root, 'real');
      await fs.mkdir(path.join(realDir, 'cellstats'), { recursive: true });
      await fs.writeFile(path.join(realDir, 'registry.json'), JSON.stringify({ schema_version: 1, modules }));
      await fs.writeFile(path.join(realDir, 'cellstats', 'summary.json'), JSON.stringify(wrapper('cellstats', 'summary')));
      const publicDir = path.join(root, 'ctam', 'public');
      await fs.mkdir(publicDir, { recursive: true });
      await fs.symlink(path.join(realDir, 'registry.json'), path.join(publicDir, 'registry.json'));
      const service = createModulesService(new ArtifactRepository({ data: root }, limits, cache, 100));
      await expect(service.listModules()).rejects.toMatchObject({ code: 'INVALID_PATH' });
    } finally {
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('covers OpenAPI paths/schemas and redacts access logs to templates', async () => {
    const modules = [mod('cellstats', [route('cellstats', 'summary')])];
    baseDir = await makeBase({ modules, payloads: { 'modules/cellstats/summary.json': wrapper('cellstats', 'summary', { secret: 'payload-must-not-log' }) } });
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    const openApi = await request(app).get('/api/v3/openapi.json').expect(200);
    const doc = typeof openApi.body === 'object' ? openApi.body : JSON.parse(openApi.text);
    for (const p of ['/api/v3/modules', '/api/v3/modules/{moduleId}', '/api/v3/modules/{moduleId}/{routeId}']) {
      expect(doc.paths).toHaveProperty(p);
    }
    expect(doc.components.schemas).toHaveProperty('ModuleRouteEnvelope');
    expect(doc.components.parameters).toHaveProperty('moduleId');
    expect(doc.components.parameters).toHaveProperty('routeId');
    expect((await request(app).get('/api/v3')).body.data.links.modules).toBe('/api/v3/modules');

    const log = jest.spyOn(console, 'info').mockImplementation(() => {});
    await request(app).get('/api/v3/modules/cellstats/summary').expect(200);
    const entry = JSON.parse(log.mock.calls[0][0]);
    expect(entry).toMatchObject({ event: 'api_access', method: 'GET', route: '/api/v3/modules/{moduleId}/{routeId}', status: 200 });
    expect(log.mock.calls[0][0]).not.toContain('payload-must-not-log');
    await request(app).get('/api/v3/modules?secret=not-logged').expect(400);
    const collectionEntry = JSON.parse(log.mock.calls[1][0]);
    expect(collectionEntry).toMatchObject({ event: 'api_access', method: 'GET', route: '/api/v3/modules', status: 400 });
    expect(log.mock.calls[1][0]).not.toContain('not-logged');
    log.mockRestore();
  });
});
