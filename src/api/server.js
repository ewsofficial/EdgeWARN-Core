import { pathToFileURL } from 'url';
import { createApp } from './app.js';

/** The same five elements /health/live serves, so a crash before the first request still records them.
 *
 * Reports the winning layer per override rather than the value, matching
 * report_effective_config in src/run.py: a key holding a credential must not be
 * disclosed by a diagnostic.
 */
function reportEffectiveConfig(config, port) {
  const { source, overrides, effective } = config.diagnostics;
  console.log(`[API] Config root: ${source.root}`);
  console.log(`[API] Catalogs loaded: api@${source.schemaVersion} (${source.file})`);
  console.log(`[API] Active overrides: ${overrides.length ? overrides.join(', ') : 'none; every resolved value came from YAML'}`);
  console.log(`[API] Enabled products: radar=${effective.radarProductCount}, allowed origins=${effective.allowedOriginCount}`);
  console.log(`[API] Listening on port ${port}, base dir ${effective.baseDir}. Configuration changes require a restart to take effect.`);
}

export async function startServer(options = {}) {
  const { app, config } = await createApp(options);
  const debug = (options.argv || process.argv.slice(2)).includes('--debug-server');
  const port = options.port || (debug ? config.api.server.debug_port : config.port);
  const server = app.listen(port, options.host || config.api.server.host, () => reportEffectiveConfig(config, port));
  return { app, server, port, config };
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  if (process.argv.includes('--compat=edgewarn')) console.warn('[Deprecation] Use npm run api; compatibility start commands now launch the unified service.');
  startServer().catch((error) => { console.error(error); process.exitCode = 1; });
}
