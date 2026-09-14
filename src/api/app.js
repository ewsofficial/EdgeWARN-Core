import express from 'express';
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import { createConfig } from './config/index.js';
import { createCors } from './middleware/cors.js';
import { errorHandler, notFound } from './middleware/errors.js';
import { createRateLimiters } from './middleware/rateLimit.js';
import { requestId } from './middleware/requestId.js';
import { createAccessLog } from './middleware/logging.js';
import { requestTimeout, securityMiddleware } from './middleware/security.js';
import { ArtifactRepository } from './repositories/artifactRepository.js';
import { createAnalysisService } from './services/analysis.js';
import { createRenderService } from './services/renders.js';
import { createAncillaryServices } from './services/ancillary.js';
import { createServiceRegistry } from './services/serviceRegistry.js';
import { createV3Router } from './routes/v3/index.js';
import { createCompatibilityRouter } from './routes/compatibility/index.js';

export async function createApp(options = {}) {
  const apiDirectory = path.dirname(fileURLToPath(import.meta.url));
  const packageManifest = JSON.parse(await fs.readFile(path.join(apiDirectory, '..', '..', 'package.json'), 'utf8'));
  const config = options.config || createConfig({ ...options, packageVersion: packageManifest.version });
  const openApi = await fs.readFile(config.openApiPath, 'utf8');
  const routeTemplates = Object.keys(JSON.parse(openApi).paths);
  const repository = new ArtifactRepository(
    { data: config.dataDir, gui: config.guiDir, wpc: config.wpcDir },
    config.api.artifacts.size_limits_bytes,
    config.api.artifacts.json_cache,
    config.api.artifacts.list_limit,
  );
  const app = express();
  app.set('trust proxy', config.trustProxy);
  const exposedVersion = config.isProduction ? config.api.server.production_version_label : config.packageVersion;
  app.use(requestId);
  if (config.api.logging.access_log_enabled) app.use(createAccessLog(routeTemplates));
  app.use(...securityMiddleware(config.api.security), createCors(config.allowedOrigins, config.api.security.cors), ...createRateLimiters(config.rateLimits), requestTimeout(config.requestTimeoutMs));
  app.get('/', (req, res) => res.json({ service: 'EdgeWARN Unified API', version: exposedVersion, links: { api: '/api/v3', openapi: '/api/v3/openapi.json' } }));
  app.get('/robots.txt', (req, res) => res.type('text/plain').send("# No clankers\nUser-agent: *\nDisallow: /\n"));
  app.get('/health/live', (req, res) => res.json({ status: 'ok', requestId: req.requestId, config: config.diagnostics }));
  app.get('/health/ready', async (req, res) => {
    const checks = await Promise.all([config.dataDir, config.guiDir, config.wpcDir].map(async (dir) => { try { return (await fs.stat(dir)).isDirectory(); } catch { return false; } }));
    // Diagnostic only (decomposition Phase 3): readiness keeps its
    // directory-based contract and never flips on an optional disabled or
    // stale service.
    res.status(checks.every(Boolean) ? 200 : 503).json({
      status: checks.every(Boolean) ? 'ready' : 'not-ready',
      requestId: req.requestId,
      config: config.diagnostics,
      services: await servicesSummary(),
    });
  });
  const analysis = createAnalysisService(repository); const renders = createRenderService(repository, config.api.render_defaults, config.api.artifacts.chunk_length_slack_bytes); const ancillary = createAncillaryServices(repository, { validation: config.api.validation, wpc: config.wpc });
  // Service-state visibility (decomposition Phase 3): the scanner classifies
  // the canonical heartbeats under <baseDir>/state/realtime/services/. The
  // API staleness is intentionally independent of child restart tuning.
  const serviceRegistry = createServiceRegistry({
    baseDir: config.baseDir,
    staleAfterSeconds: config.api.server.service_stale_after_seconds,
  });
  const servicesSummary = async () => Object.fromEntries(Object.entries(await serviceRegistry.states()).map(([name, entry]) => [name, {
    state: entry.state,
    phase: entry.heartbeat?.phase ?? null,
    lastSeen: entry.heartbeat ? entry.heartbeat.updatedAt.toISOString() : null,
    degradedChildren: entry.heartbeat?.degradedChildren ?? [],
  }]));
  app.use('/api/v3', createV3Router({ analysis, renders, ancillary, openApi, apiConfig: config.api, serviceRegistry }));
  app.use(createCompatibilityRouter({ analysis, renders, ancillary, packageVersion: exposedVersion, serviceRegistry }));
  app.use(notFound); app.use(errorHandler);
  return { app, config };
}
