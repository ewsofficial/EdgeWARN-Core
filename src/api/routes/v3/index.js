import express from 'express';
import { page, timestamp } from '../../services/validation.js';
import { createServiceGate, problemJsonResponder } from '../../middleware/serviceGate.js';
import { streamArtifact } from '../../streamArtifact.js';

const listOptions = (req) => ({ cursor: typeof req.query.cursor === 'string' ? req.query.cursor : undefined, limit: req.query.limit ? Number(req.query.limit) : undefined });
const COLLECTION_PATHS = new Set(['/cells', '/storm-snapshots', '/alert-snapshots', '/radar-sites', '/modules']);

function validateQuery(apiConfig, paths) {
  const limitPattern = new RegExp(apiConfig.query.limit_pattern);
  return (req, res, next) => {
  if (!paths.some((pattern) => pattern.test(req.path))) return next();
  const isCollection = COLLECTION_PATHS.has(req.path);
  const allowed = new Set(isCollection ? apiConfig.query.allowed_params : []);
  if (req.path === '/alert-snapshots' || /^\/alert-snapshots\/[^/]+$/.test(req.path) || /^\/alerts\/[^/]+$/.test(req.path)) allowed.add('source');
  for (const [key, value] of Object.entries(req.query)) {
    if (!allowed.has(key) || Array.isArray(value) || typeof value !== 'string' || value.length > apiConfig.query.max_value_length) return res.status(400).type('application/problem+json').json({ type: 'about:blank', title: 'Bad Request', status: 400, detail: `Invalid query parameter: ${key}`, instance: req.originalUrl, requestId: req.requestId });
    if (key === 'limit' && !limitPattern.test(value)) return res.status(400).type('application/problem+json').json({ type: 'about:blank', title: 'Bad Request', status: 400, detail: 'Invalid query parameter: limit', instance: req.originalUrl, requestId: req.requestId });
  }
  next();
  };
}

function resourcePaths(openApi) {
  return Object.keys(JSON.parse(openApi).paths).map((route) => {
    const localRoute = route.replace(/^\/api\/v3/, '') || '/';
    return new RegExp(`^${localRoute.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\{[^}]+\\\}/g, '[^/]+')}$`);
  });
}

function methodNotAllowed(paths) {
  return (req, res, next) => {
    if (paths.some((pattern) => pattern.test(req.path))) return res.set('Allow', 'GET, HEAD').status(405).type('application/problem+json').json({ type: 'about:blank', title: 'Method Not Allowed', status: 405, detail: 'This resource only supports GET and HEAD.', instance: req.originalUrl, requestId: req.requestId });
    return next();
  };
}

export function createV3Router({ analysis, ancillary, modules, openApi, apiConfig, serviceRegistry }) {
  const requireService = (service) => createServiceGate({
    serviceRegistry,
    service,
    respond: problemJsonResponder(apiConfig),
  });
  const collection = (req, res, items) => { const result = page(items, listOptions(req), apiConfig.pagination); res.set('Cache-Control', `public, max-age=${apiConfig.cache_control_max_age.collection}`).json({ data: result.data, meta: { nextCursor: result.nextCursor } }); };
  const resource = (req, res, data) => res.set('Cache-Control', `public, max-age=${apiConfig.cache_control_max_age.resource}`).json({ data, meta: {} });
  const send = (req, res, opened, type, headers = {}) => streamArtifact(req, res, opened, type, headers, { 'Cache-Control': `public, max-age=${apiConfig.cache_control_max_age.asset}, immutable`, ETag: opened.etag });
  const router = express.Router();
  const paths = resourcePaths(openApi);
  router.use(validateQuery(apiConfig, paths));
  router.get('/', (req, res) => resource(req, res, { version: apiConfig.server.v3_api_version, links: { openapi: '/api/v3/openapi.json', cells: '/api/v3/cells', modules: '/api/v3/modules' } }));
  router.get('/openapi.json', (req, res) => res.type('application/json').send(openApi));
  router.get('/cells', requireService('edgewarn'), async (req, res, next) => { try { collection(req, res, await analysis.listCells()); } catch (error) { next(error); } });
  router.get('/cells/:cellId', requireService('edgewarn'), async (req, res, next) => { try { resource(req, res, await analysis.getCell(req.params.cellId)); } catch (error) { next(error); } });
  router.get('/storm-snapshots', requireService('edgewarn'), async (req, res, next) => { try { collection(req, res, await analysis.listStormSnapshots()); } catch (error) { next(error); } });
  router.get('/storm-snapshots/:timestamp', requireService('edgewarn'), async (req, res, next) => { try { resource(req, res, { timestamp: req.params.timestamp, validTime: timestamp(req.params.timestamp), cells: await analysis.getStormSnapshot(req.params.timestamp) }); } catch (error) { next(error); } });
  router.get('/alert-snapshots', requireService('edgewarn'), async (req, res, next) => { try { collection(req, res, await analysis.listAlertSnapshots(req.query.source)); } catch (error) { next(error); } });
  router.get('/alert-snapshots/:timestamp', requireService('edgewarn'), async (req, res, next) => { try { resource(req, res, { timestamp: req.params.timestamp, validTime: timestamp(req.params.timestamp), alerts: await analysis.getAlertSnapshot(req.query.source, req.params.timestamp) }); } catch (error) { next(error); } });
  router.get('/alerts/:alertId', requireService('edgewarn'), async (req, res, next) => { try { resource(req, res, await analysis.getAlert(req.query.source, req.params.alertId)); } catch (error) { next(error); } });
  router.get('/modules', requireService('edgewarn'), async (req, res, next) => { try { collection(req, res, await modules.listModules()); } catch (error) { next(error); } });
  router.get('/modules/:moduleId', requireService('edgewarn'), async (req, res, next) => { try { resource(req, res, await modules.getModule(req.params.moduleId)); } catch (error) { next(error); } });
  router.get('/modules/:moduleId/:routeId', requireService('edgewarn'), async (req, res, next) => { try { const result = await modules.getRoute(req.params.moduleId, req.params.routeId); res.set('Cache-Control', `public, max-age=${apiConfig.cache_control_max_age.resource}`).json(result); } catch (error) { next(error); } });
  router.get('/radar-sites', requireService('nexrad'), async (req, res, next) => { try { collection(req, res, await ancillary.listRadarSites()); } catch (error) { next(error); } });
  router.get('/radar-sites/:siteId/availability', requireService('nexrad'), async (req, res, next) => { try { resource(req, res, await ancillary.radarAvailability(req.params.siteId.toUpperCase())); } catch (error) { next(error); } });
  router.get('/radar-sites/:siteId/scans/:timestamp/elevations/:elevation/products/:productId', requireService('nexrad'), async (req, res, next) => { try { await send(req, res, await ancillary.radarField(req.params.siteId, req.params.timestamp, req.params.elevation, req.params.productId), 'application/gzip'); } catch (error) { next(error); } });
  router.use(methodNotAllowed(paths));
  return router;
}
