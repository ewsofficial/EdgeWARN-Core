import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import request from 'supertest';
import { createApp } from '../../src/api/app.js';

describe('CTAM public module routes', () => {
  let baseDir;
  beforeEach(async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'ctam-module-routes-'));
    await Promise.all(['data/ctam/public/modules/cellstats', 'gui', 'wpc', 'state/realtime/services'].map((entry) => fs.mkdir(path.join(baseDir, entry), { recursive: true })));
    await fs.writeFile(path.join(baseDir, 'state/realtime/services/edgewarn.json'), JSON.stringify({ schema_version: 1, service: 'edgewarn', pid: 1, run_id: 'test', updated_at: new Date().toISOString(), phase: 'cycling', degraded_children: [] }));
    const module = { id: 'cellstats', name: 'Cell Stats', version: '1.0.0', href: '/api/v3/modules/cellstats', routes: [{ id: 'summary', description: 'Latest summary', href: '/api/v3/modules/cellstats/summary', available: true }] };
    await fs.writeFile(path.join(baseDir, 'data/ctam/public/registry.json'), JSON.stringify({ schema_version: 1, modules: [module] }));
    await fs.writeFile(path.join(baseDir, 'data/ctam/public/modules/cellstats/summary.json'), JSON.stringify({ schema_version: 1, module_id: 'cellstats', module_version: '1.0.0', route_id: 'summary', cycle_id: '20260915-120000', published_at: '2026-09-15T12:00:05Z', data: { risk: 'elevated' } }));
  });
  afterEach(async () => fs.rm(baseDir, { recursive: true, force: true }));

  it('mounts collection, descriptor, payload, HEAD, and read-only policy', async () => {
    const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] });
    expect((await request(app).get('/api/v3')).body.data.links.modules).toBe('/api/v3/modules');
    expect((await request(app).get('/api/v3/modules?limit=1')).body.data[0].id).toBe('cellstats');
    expect((await request(app).get('/api/v3/modules/cellstats')).body.data.routes[0].id).toBe('summary');
    const payload = await request(app).get('/api/v3/modules/cellstats/summary').expect(200).expect('Cache-Control', /public/);
    expect(payload.body).toMatchObject({ data: { risk: 'elevated' }, meta: { moduleId: 'cellstats', routeId: 'summary' } });
    await request(app).head('/api/v3/modules/cellstats/summary').expect(200);
    for (const method of ['post', 'put', 'patch', 'delete']) await request(app)[method]('/api/v3/modules/cellstats/summary').expect(405).expect('Allow', 'GET, HEAD');
  });
});
