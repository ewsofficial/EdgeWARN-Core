import express from 'express';
import { page, timestamp } from '../../services/validation.js';
import { productCatalog } from '../../config/productCatalog.js';
import { createServiceGate, problemJsonResponder } from '../../middleware/serviceGate.js';
import { streamArtifact } from '../../streamArtifact.js';

const listOptions = (req) => ({ cursor: typeof req.query.cursor === 'string' ? req.query.cursor : undefined, limit: req.query.limit ? Number(req.query.limit) : undefined });
const COLLECTION_PATHS = new Set(['/cells', '/storm-snapshots', '/alert-snapshots', '/observations/metar', '/render-products', '/radar-sites', '/models/rap/layers', '/analyses/wpc/surface', '/modules']);

function validateQuery(apiConfig) {
  const limitPattern = new RegExp(apiConfig.query.limit_pattern);
  return (req, res, next) => {
  const isCollection = COLLECTION_PATHS.has(req.path) || /\/render-products\/[^/]+\/snapshots$/.test(req.path) || /\/models\/rap\/layers\/[^/]+\/snapshots$/.test(req.path);
  const allowed = new Set(isCollection ? apiConfig.query.allowed_params : []);
  if (req.path === '/alert-snapshots' || /^\/alert-snapshots\/[^/]+$/.test(req.path) || /^\/alerts\/[^/]+$/.test(req.path)) allowed.add('source');
  for (const [key, value] of Object.entries(req.query)) {
    if (!allowed.has(key) || Array.isArray(value) || typeof value !== 'string' || value.length > apiConfig.query.max_value_length) return res.status(400).type('application/problem+json').json({ type: 'about:blank', title: 'Bad Request', status: 400, detail: `Invalid query parameter: ${key}`, instance: req.originalUrl, requestId: req.requestId });
    if (key === 'limit' && !limitPattern.test(value)) return res.status(400).type('application/problem+json').json({ type: 'about:blank', title: 'Bad Request', status: 400, detail: 'Invalid query parameter: limit', instance: req.originalUrl, requestId: req.requestId });
  }
  next();
  };
}

function methodNotAllowed(openApi) {
  const paths = Object.keys(JSON.parse(openApi).paths).map((route) => {
    const localRoute = route.replace(/^\/api\/v3/, '') || '/';
    return new RegExp(`^${localRoute.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\{[^}]+\\\}/g, '[^/]+')}$`);
  });
  return (req, res, next) => {
    if (paths.some((pattern) => pattern.test(req.path))) return res.set('Allow', 'GET, HEAD').status(405).type('application/problem+json').json({ type: 'about:blank', title: 'Method Not Allowed', status: 405, detail: 'This resource only supports GET and HEAD.', instance: req.originalUrl, requestId: req.requestId });
    return next();
  };
}

export function createV3Router({ analysis, renders, ancillary, modules, openApi, apiConfig, serviceRegistry }) {
  const requireService = (service) => createServiceGate({
    serviceRegistry,
    service,
    respond: problemJsonResponder(apiConfig),
  });
  const collection = (req, res, items) => { const result = page(items, listOptions(req), apiConfig.pagination); res.set('Cache-Control', `public, max-age=${apiConfig.cache_control_max_age.collection}`).json({ data: result.data, meta: { nextCursor: result.nextCursor } }); };
  const resource = (req, res, data) => res.set('Cache-Control', `public, max-age=${apiConfig.cache_control_max_age.resource}`).json({ data, meta: {} });
  const geojson = (req, res, data) => res.set('Cache-Control', `public, max-age=${apiConfig.cache_control_max_age.resource}`).type('application/geo+json').json(data);
  const send = (req, res, opened, type, headers = {}) => streamArtifact(req, res, opened, type, headers, { 'Cache-Control': `public, max-age=${apiConfig.cache_control_max_age.asset}, immutable`, ETag: opened.etag });
  const router = express.Router();
  router.use(validateQuery(apiConfig));
  router.get('/', (req, res) => resource(req, res, { version: apiConfig.server.v3_api_version, links: { openapi: '/api/v3/openapi.json', cells: '/api/v3/cells', modules: '/api/v3/modules', renderProducts: '/api/v3/render-products' } }));
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
  router.get('/observations/metar', async (req, res, next) => { try { collection(req, res, await analysis.listMetarHours()); } catch (error) { next(error); } });
  router.get('/observations/metar/:timestamp', async (req, res, next) => { try { resource(req, res, await analysis.getMetar(req.params.timestamp)); } catch (error) { next(error); } });
  router.get('/render-products', requireService('ewmrs'), async (req, res, next) => { try { const available = new Set((await renders.listProducts()).map((item) => item.id)); collection(req, res, await Promise.all(productCatalog.filter((item) => available.has(item.id)).map((item) => renders.getProduct(item.id)))); } catch (error) { next(error); } });
  router.get('/render-products/:productId', requireService('ewmrs'), async (req, res, next) => { try { resource(req, res, await renders.getProduct(req.params.productId)); } catch (error) { next(error); } });
  router.get('/render-products/:productId/snapshots', requireService('ewmrs'), async (req, res, next) => { try { collection(req, res, await renders.listSnapshots(req.params.productId)); } catch (error) { next(error); } });
  router.get('/render-products/:productId/snapshots/:timestamp/chunks', requireService('ewmrs'), async (req, res, next) => { try { resource(req, res, await renders.chunks(req.params.productId, req.params.timestamp)); } catch (error) { next(error); } });
  router.get('/render-products/:productId/snapshots/:timestamp/chunks/:x/:y', requireService('ewmrs'), async (req, res, next) => { try {
    const opened = await renders.chunk(req.params.productId, req.params.timestamp, Number(req.params.x), Number(req.params.y)); const { grid: chunkGrid } = opened.chunk;
    await send(req, res, opened, 'application/octet-stream', {
      'X-EWMRS-Format-Version': '2', 'X-Data-Type': 'float16', 'X-Value-Kind': opened.chunk.format.value_kind,
      'X-Channel-Count': String(opened.chunk.format.channels), 'X-No-Data': 'nan', 'Content-Encoding': 'gzip',
      'X-Chunk-Width': String(chunkGrid.tileSize), 'X-Chunk-Height': String(chunkGrid.tileSize),
      'X-Grid-Origin': 'bottom-left', 'X-Pixel-Row-Order': 'top-to-bottom'
    });
  } catch (error) { next(error); } });
  router.get('/radar-sites', requireService('nexrad'), async (req, res, next) => { try { collection(req, res, await ancillary.listRadarSites()); } catch (error) { next(error); } });
  router.get('/radar-sites/:siteId/availability', requireService('nexrad'), async (req, res, next) => { try { resource(req, res, await ancillary.radarAvailability(req.params.siteId.toUpperCase())); } catch (error) { next(error); } });
  router.get('/radar-sites/:siteId/scans/:timestamp/elevations/:elevation/products/:productId', requireService('nexrad'), async (req, res, next) => { try { await send(req, res, await ancillary.radarField(req.params.siteId, req.params.timestamp, req.params.elevation, req.params.productId), 'application/gzip'); } catch (error) { next(error); } });
  router.get('/models/rap/layers', requireService('ewmrs'), async (req, res, next) => { try { collection(req, res, await ancillary.listRapLayers()); } catch (error) { next(error); } });
  router.get('/models/rap/layers/:layerId/snapshots', requireService('ewmrs'), async (req, res, next) => { try { collection(req, res, await ancillary.rapSnapshots(req.params.layerId)); } catch (error) { next(error); } });
  router.get('/models/rap/layers/:layerId/snapshots/:timestamp/metadata', requireService('ewmrs'), async (req, res, next) => { try { resource(req, res, await ancillary.rapMetadata(req.params.layerId, req.params.timestamp)); } catch (error) { next(error); } });
  router.get('/models/rap/layers/:layerId/snapshots/:timestamp/data', requireService('ewmrs'), async (req, res, next) => { try { await send(req, res, await ancillary.rapData(req.params.layerId, req.params.timestamp), 'application/octet-stream'); } catch (error) { next(error); } });
  router.get('/analyses/wpc/surface', requireService('ewmrs'), async (req, res, next) => { try { collection(req, res, await ancillary.listWpcSurface()); } catch (error) { next(error); } });
  router.get('/analyses/wpc/surface/:timestamp', requireService('ewmrs'), async (req, res, next) => { try { geojson(req, res, await ancillary.wpcSurface(req.params.timestamp)); } catch (error) { next(error); } });
  router.use(methodNotAllowed(openApi));
  return router;
}
