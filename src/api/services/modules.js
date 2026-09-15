import { ArtifactError } from '../repositories/artifactRepository.js';

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const MAX_ROUTES = 16;

function validId(value) {
  if (typeof value !== 'string' || !SAFE_ID.test(value) || value === '.' || value === '..') return false;
  try { return decodeURIComponent(value) === value; } catch { return false; }
}

function invalidArtifact(message = 'Malformed CTAM public route registry') {
  throw new ArtifactError('INVALID_ARTIFACT', message);
}

function hasOnlyKeys(value, keys) {
  const actual = Object.keys(value).sort();
  return actual.length === keys.length && actual.every((key, index) => key === [...keys].sort()[index]);
}

function validateRegistry(value) {
  if (!value || !hasOnlyKeys(value, ['schema_version', 'modules']) || value.schema_version !== 1 || !Array.isArray(value.modules)) invalidArtifact();
  const seen = new Set();
  for (const module of value.modules) {
    if (!module || !hasOnlyKeys(module, ['id', 'name', 'version', 'href', 'routes']) || !validId(module.id) || seen.has(module.id) || typeof module.name !== 'string' || !module.name || typeof module.version !== 'string' || !module.version || module.href !== `/api/v3/modules/${module.id}` || !Array.isArray(module.routes) || module.routes.length < 1 || module.routes.length > MAX_ROUTES) invalidArtifact();
    seen.add(module.id);
    const routeIds = new Set();
    for (const route of module.routes) {
      if (!route || !hasOnlyKeys(route, ['id', 'description', 'href', 'available']) || !validId(route.id) || routeIds.has(route.id) || typeof route.description !== 'string' || route.description.length < 1 || route.description.length > 256 || /[\u0000-\u001f\u007f]/.test(route.description) || route.href !== `${module.href}/${route.id}` || typeof route.available !== 'boolean') invalidArtifact();
      routeIds.add(route.id);
    }
  }
  return value.modules;
}

export function createModulesService(repository) {
  async function registry() {
    try { return validateRegistry(await repository.readJson('data', ['ctam', 'public', 'registry.json'])); }
    catch (error) { if (error.code === 'NOT_FOUND') return []; throw error; }
  }
  return {
    async listModules() { return registry(); },
    async getModule(moduleId) {
      if (!validId(moduleId)) throw new ArtifactError('INVALID_PATH', 'Invalid module ID');
      const module = (await registry()).find((item) => item.id === moduleId);
      if (!module) throw new ArtifactError('NOT_FOUND', 'Module not found');
      return module;
    },
    async getRoute(moduleId, routeId) {
      if (!validId(moduleId) || !validId(routeId)) throw new ArtifactError('INVALID_PATH', 'Invalid module route');
      const module = (await registry()).find((item) => item.id === moduleId);
      if (!module) throw new ArtifactError('NOT_FOUND', 'Module not found');
      const route = module.routes.find((item) => item.id === routeId);
      if (!route) throw new ArtifactError('NOT_FOUND', 'Module route not found');
      if (!route.available) throw new ArtifactError('MODULE_ROUTE_UNAVAILABLE', 'The module route has no committed representation');
      let artifact;
      try { artifact = await repository.readJson('data', ['ctam', 'public', 'modules', module.id, `${route.id}.json`]); }
      catch (error) { if (error.code === 'NOT_FOUND') throw new ArtifactError('INVALID_ARTIFACT', 'Available module route artifact is missing', { cause: error }); throw error; }
      if (!artifact || !hasOnlyKeys(artifact, ['schema_version', 'module_id', 'module_version', 'route_id', 'cycle_id', 'published_at', 'data']) || artifact.schema_version !== 1 || artifact.module_id !== module.id || artifact.module_version !== module.version || artifact.route_id !== route.id || typeof artifact.cycle_id !== 'string' || !artifact.cycle_id || typeof artifact.published_at !== 'string' || !Number.isFinite(Date.parse(artifact.published_at))) invalidArtifact('Malformed CTAM public route artifact');
      return { data: artifact.data, meta: { moduleId: artifact.module_id, moduleVersion: artifact.module_version, routeId: artifact.route_id, cycleId: artifact.cycle_id, publishedAt: artifact.published_at } };
    },
  };
}
