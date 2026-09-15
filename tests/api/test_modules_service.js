import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import { ArtifactRepository } from '../../src/api/repositories/artifactRepository.js';
import { createModulesService } from '../../src/api/services/modules.js';

const limits = { json: 2_000_000, binary: 2_000_000, image: 2_000_000 };
const cache = { max_entries: 10, max_size_bytes: 4_000_000 };

describe('CTAM public modules service', () => {
  let root;
  let service;
  beforeEach(async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'ctam-modules-'));
    await fs.mkdir(path.join(root, 'ctam', 'public', 'modules', 'cellstats'), { recursive: true });
    service = createModulesService(new ArtifactRepository({ data: root }, limits, cache, 100));
  });
  afterEach(async () => fs.rm(root, { recursive: true, force: true }));

  it('returns an empty collection before publication', async () => {
    expect(await service.listModules()).toEqual([]);
  });

  it('uses the registry as an allowlist and unwraps host metadata', async () => {
    const module = { id: 'cellstats', name: 'Cell Stats', version: '1.0.0', href: '/api/v3/modules/cellstats', routes: [{ id: 'summary', description: 'Latest summary', href: '/api/v3/modules/cellstats/summary', available: true }] };
    await fs.writeFile(path.join(root, 'ctam', 'public', 'registry.json'), JSON.stringify({ schema_version: 1, modules: [module] }));
    await fs.writeFile(path.join(root, 'ctam', 'public', 'modules', 'cellstats', 'summary.json'), JSON.stringify({ schema_version: 1, module_id: 'cellstats', module_version: '1.0.0', route_id: 'summary', cycle_id: '20260915-120000', published_at: '2026-09-15T12:00:05Z', data: { risk: 'elevated' } }));
    expect(await service.getModule('cellstats')).toEqual(module);
    expect(await service.getRoute('cellstats', 'summary')).toEqual({ data: { risk: 'elevated' }, meta: { moduleId: 'cellstats', moduleVersion: '1.0.0', routeId: 'summary', cycleId: '20260915-120000', publishedAt: '2026-09-15T12:00:05Z' } });
    await expect(service.getRoute('cellstats', 'undeclared')).rejects.toMatchObject({ code: 'NOT_FOUND' });
  });

  it('rejects malformed identifiers, unavailable routes, and mismatched wrappers', async () => {
    const module = { id: 'cellstats', name: 'Cell Stats', version: '1.0.0', href: '/api/v3/modules/cellstats', routes: [{ id: 'summary', description: 'Latest summary', href: '/api/v3/modules/cellstats/summary', available: false }] };
    await fs.writeFile(path.join(root, 'ctam', 'public', 'registry.json'), JSON.stringify({ schema_version: 1, modules: [module] }));
    await expect(service.getModule('%2F')).rejects.toMatchObject({ code: 'INVALID_PATH' });
    await expect(service.getRoute('cellstats', 'summary')).rejects.toMatchObject({ code: 'MODULE_ROUTE_UNAVAILABLE', status: 503 });
    module.routes[0].available = true;
    await fs.writeFile(path.join(root, 'ctam', 'public', 'registry.json'), JSON.stringify({ schema_version: 1, modules: [module] }));
    await fs.writeFile(path.join(root, 'ctam', 'public', 'modules', 'cellstats', 'summary.json'), JSON.stringify({ schema_version: 1, module_id: 'other', module_version: '1.0.0', route_id: 'summary', cycle_id: 'cycle', published_at: '2026-09-15T12:00:05Z', data: {} }));
    service = createModulesService(new ArtifactRepository({ data: root }, limits, cache, 100));
    await expect(service.getRoute('cellstats', 'summary')).rejects.toMatchObject({ code: 'INVALID_ARTIFACT' });
  });
});
