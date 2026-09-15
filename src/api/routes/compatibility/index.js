import express from 'express';
import { getProductByLegacyId } from '../../config/productCatalog.js';
import { createServiceGate, legacyEnvelopeResponder } from '../../middleware/serviceGate.js';
import { streamArtifact } from '../../streamArtifact.js';

const deprecate = (res) => res.set({ Deprecation: 'true', Link: '</api/v3/openapi.json>; rel="deprecation"' });
const send = (req, res, opened, type, headers = {}) => streamArtifact(req, res, opened, type, headers);
const value = (input) => typeof input === 'string' && input ? input : null;
const renderPngGone = (req, res) => deprecate(res).status(410).json({
  error: 'PNG render delivery was removed in API v3; use the float16 chunk resources.',
  code: 'RENDER_PNG_REMOVED',
  documentation: '/api/v3/openapi.json',
  successor: '/api/v3/render-products/{productId}/snapshots/{timestamp}/chunks',
});

export function createCompatibilityRouter({ analysis, renders, ancillary, packageVersion, serviceRegistry }) {
  const requireService = (service) => createServiceGate({
    serviceRegistry,
    service,
    respond: (req, res, error) => {
      deprecate(res);
      return legacyEnvelopeResponder()(req, res, error);
    },
  });
  const router = express.Router();
  // v2 feature data is the legacy view of EdgeWARN-owned artifacts and must
  // obey the same stale-service contract as v3.
  router.use('/api/v2/features', requireService('edgewarn'));
  router.get('/api/v2', (req, res) => deprecate(res).json({ message: 'EdgeWARN API v2', version: packageVersion, endpoints: { features: { cells: '/api/v2/features/cells[?id={int}]', timestamps: '/api/v2/features/timestamps[?timestamp={YYYYMMDD-HHMMSS}]', alerts: { official: '/api/v2/features/alerts/official[?id={id}|timestamp={YYYYMMDD-HHMMSS}]', edgewarn: '/api/v2/features/alerts/edgewarn[?id={id}|timestamp={YYYYMMDD-HHMMSS}]' } }, data: { metar: '/api/v2/data/metar[?timestamp={YYYYMMDD-HHMMSS}]' } } }));
  router.get('/api/v2/features/cells', async (req, res, next) => { try { deprecate(res); const id = value(req.query.id); res.json(id ? await analysis.getCell(id) : await analysis.listCells()); } catch (e) { next(e); } });
  router.get('/api/v2/features/timestamps', async (req, res, next) => { try { deprecate(res); const ts = value(req.query.timestamp); res.json(ts ? await analysis.getStormSnapshot(ts) : await analysis.listStormSnapshots()); } catch (e) { next(e); } });
  router.get('/api/v2/features/alerts/:source', async (req, res, next) => { try { deprecate(res); const { source } = req.params; const id = value(req.query.id); const ts = value(req.query.timestamp); if (id && ts) return res.status(400).json({ success: false, error: { code: 'INVALID_INPUT', message: 'Parameters timestamp and id cannot be specified at the same time' } }); if (id) return res.json(await analysis.getAlert(source, id)); if (ts) return res.json(await analysis.getAlertSnapshot(source, ts)); return res.json(await analysis.listAlertSnapshots(source)); } catch (e) { next(e); } });
  router.get('/api/v2/data/metar', async (req, res, next) => { try { deprecate(res); const ts = value(req.query.timestamp); if (!ts) return res.json(await analysis.listMetarHours()); const result = await analysis.getMetar(ts); return res.json({ type: 'metar', timestamp: result.requestedTimestamp, data: result.observations }); } catch (e) { next(e); } });
  router.get('/renders/get-items', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); res.json((await renders.listProducts()).map((item) => item.storageDirectory)); } catch (e) { next(e); } });
  router.get('/renders/fetch', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); const product = getProductByLegacyId(value(req.query.product)); if (!product) return res.status(404).json({ error: 'Unknown product or no mapping found' }); res.json(await renders.listSnapshots(product.id)); } catch (e) { next(e); } });
  router.get('/renders/download', renderPngGone);
  router.get('/renders/tile', renderPngGone);
  router.get('/renders/tile-info', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); const product = getProductByLegacyId(value(req.query.product)); if (!product) return res.status(404).json({ error: 'Unknown product or no mapping found' }); const details = await renders.getProduct(product.id); const timestamps = await renders.listSnapshots(product.id); res.json({ product: product.storageDirectory, rows: details.grid.rows, cols: details.grid.cols, tile_size: details.grid.tileSize, timestamps }); } catch (e) { next(e); } });
  router.get('/nexrad', requireService('nexrad'), async (req, res, next) => { try { deprecate(res); res.json(await ancillary.listRadarSites()); } catch (e) { next(e); } });
  router.get('/nexrad/:site/:timestamp/:elevation', requireService('nexrad'), async (req, res, next) => { try { deprecate(res); const product = value(req.query.product); const opened = await ancillary.radarField(req.params.site, req.params.timestamp, req.params.elevation, product); await send(req, res, opened, 'application/gzip', { 'Content-Disposition': `attachment; filename="${req.params.site}_${req.params.timestamp}_${req.params.elevation}_${product}.bin.gz"` }); } catch (e) { next(e); } });
  router.get('/nexrad/:site', requireService('nexrad'), async (req, res, next) => { try { deprecate(res); const available = await ancillary.radarAvailability(req.params.site.toUpperCase()); res.json(Object.fromEntries(Object.entries(available).map(([elevation, scans]) => [elevation, scans.map((scan) => scan.timestamp)]))); } catch (e) { next(e); } });
  router.get('/rap/layers', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); res.json(await ancillary.listRapLayers()); } catch (e) { next(e); } });
  router.get('/rap/fetch', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); res.json(await ancillary.rapSnapshots(value(req.query.layer))); } catch (e) { next(e); } });
  router.get('/rap/metadata', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); res.json(await ancillary.rapMetadata(value(req.query.layer), value(req.query.timestamp))); } catch (e) { next(e); } });
  router.get('/rap/data', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); await send(req, res, await ancillary.rapData(value(req.query.layer), value(req.query.timestamp)), 'application/octet-stream'); } catch (e) { next(e); } });
  router.get('/wpc/fetch', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); if (req.query.type !== 'sfc') return res.status(400).json({ error: 'Invalid type' }); res.json(await ancillary.listWpcSurface()); } catch (e) { next(e); } });
  router.get('/wpc/download', requireService('ewmrs'), async (req, res, next) => { try { deprecate(res); if (req.query.type !== 'sfc') return res.status(400).json({ error: 'Invalid type' }); res.json(await ancillary.wpcSurface(value(req.query.timestamp))); } catch (e) { next(e); } });
  router.get('/health', (req, res) => deprecate(res).json({ status: 'OK', timestamp: new Date().toISOString() }));
  router.get('/healthz', (req, res) => deprecate(res).json({ ok: true }));
  router.use(['/features', '/data', '/api/v1'], (req, res) => res.status(410).json({ error: 'API v1 has been removed. Please use API v2.', documentation: '/api/v2' }));
  return router;
}
