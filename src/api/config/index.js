import { readFileSync } from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { configRoot, expandPath, getProvenance, loadConfig, repoRoot, srcRoot, validateAllConfigs } from '../../config/loader.js';

// package.json is the sole owner of the version. This was a literal default,
// which agreed with the manifest only until one of the two was bumped.
const PACKAGE_VERSION = JSON.parse(readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../package.json'),
  'utf8',
)).version;

const INTEGER = /^(?:0|[1-9][0-9]*)$/;

function readFlag(argv, names) {
  const values = [];
  for (let index = 0; index < argv.length; index += 1) {
    for (const name of names) {
      if (argv[index] === name && argv[index + 1] !== undefined) values.push(argv[index + 1]);
      if (argv[index].startsWith(`${name}=`)) values.push(argv[index].slice(name.length + 1));
    }
  }
  return values;
}

function oneValue(values, label) {
  const distinct = [...new Set(values.filter((value) => value !== undefined && value !== ''))];
  if (distinct.length > 1) throw new Error(`Conflicting ${label} values`);
  return distinct[0];
}

function parseInteger(value, fallback, label, { minimum = 0 } = {}) {
  if (value === undefined) return fallback;
  if (typeof value !== 'string' || !INTEGER.test(value)) throw new Error(`Invalid ${label}`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum) throw new Error(`Invalid ${label}`);
  return parsed;
}

function parseIntegerOverride(value, fallback, label, options) {
  return Object.freeze({
    value: parseInteger(value, fallback, label, options),
    overridden: value !== undefined && value !== '',
  });
}

function parseOrigins(value) {
  if (Array.isArray(value)) {
    for (const origin of value) {
      if (origin === '*') continue;
      let url;
      try { url = new URL(origin); } catch { throw new Error(`Invalid allowed origin: ${origin}`); }
      if (url.origin !== origin || !['http:', 'https:'].includes(url.protocol)) throw new Error(`Invalid allowed origin: ${origin}`);
    }
    return Object.freeze([...new Set(value)]);
  }
  if (!value) return [];
  const origins = value.split(',').map((origin) => origin.trim()).filter(Boolean);
  for (const origin of origins) {
    if (origin === '*') continue;
    let url;
    try { url = new URL(origin); } catch { throw new Error(`Invalid allowed origin: ${origin}`); }
    if (url.origin !== origin || !['http:', 'https:'].includes(url.protocol)) throw new Error(`Invalid allowed origin: ${origin}`);
  }
  return Object.freeze([...new Set(origins)]);
}

export function parseTrustProxy(value, env = {}) {
  if (value === false || value === undefined || value === '') return false;
  if (Number.isInteger(value) || Array.isArray(value)) return value;
  if (value === true || typeof value === 'string') {
    const normalized = typeof value === 'string' ? value.trim().toLowerCase() : 'true';
    if (normalized === '' || normalized === 'false') return false;
    if (normalized === 'true') {
      if (env.NODE_ENV === 'production') throw new Error('TRUST_PROXY=true is unsafe in production; set TRUST_PROXY_IPS');
      return 1;
    }
    if (/^\d+$/.test(normalized)) {
      const hops = Number(normalized);
      if (Number.isSafeInteger(hops) && hops >= 0 && hops <= 8) return hops;
      throw new Error('Invalid TRUST_PROXY hop count');
    }
    return Object.freeze(value.split(',').map((entry) => entry.trim()).filter(Boolean));
  }
  throw new Error('Invalid TRUST_PROXY');
}

function defaultEnvironment() {
  // Keep the canonical shared base-directory variable explicit on the Node
  // surface while still copying the environment for dependency injection.
  return { ...process.env, EDGEWARN_BASE_DIR: process.env.EDGEWARN_BASE_DIR };
}

// The token expansion and traversal rejection moved into the shared loader, so
// Python and Node enforce one contract instead of two. What is left here is the
// only part specific to this caller: which token is in scope, and which key to
// name when the value is bad.
function resolveSourcePath(template, label) {
  return expandPath(template, { src_dir: srcRoot() }, {
    filename: 'api.yaml',
    dottedPath: `server.${label}`,
  });
}

export function createConfig({ env = defaultEnvironment(), argv = process.argv.slice(2), packageVersion = PACKAGE_VERSION } = {}) {
  const configDirCli = oneValue(readFlag(argv, ['--config-dir']), '--config-dir');
  const configDirEnv = env.EDGEWARN_CONFIG_DIR;
  const selectedConfigDir = configDirCli || configDirEnv;
  validateAllConfigs({ configDir: selectedConfigDir });
  const api = loadConfig('api', { configDir: selectedConfigDir });
  const filesystem = loadConfig('filesystem', { configDir: selectedConfigDir });
  // The API serves WPC surface analyses out of a directory the ingest side names,
  // so it reads that file's naming keys rather than restating them here.
  const wpc = loadConfig('wpc', { configDir: selectedConfigDir }).wpc;
  const resolvedConfigRoot = configRoot(selectedConfigDir);
  const apiProvenance = getProvenance('api', { configDir: selectedConfigDir });
  const wpcProvenance = getProvenance('wpc', { configDir: selectedConfigDir });
  const canonicalCli = oneValue(readFlag(argv, ['--base-dir']), '--base-dir');
  const deprecatedCli = oneValue(readFlag(argv, ['--base_dir']), '--base_dir');
  const canonicalEnv = env.EDGEWARN_BASE_DIR;
  const deprecatedEnv = env.BASE_DIR;
  const baseCli = canonicalCli || deprecatedCli;
  const baseEnv = canonicalEnv || deprecatedEnv;
  const configuredBaseDir = process.platform === 'win32' ? filesystem.base_dir.windows : filesystem.base_dir.posix;
  const baseDir = path.resolve((baseCli || baseEnv || configuredBaseDir).replace(/^~(?=$|[\\/])/, os.homedir()));
  const portSetting = parseIntegerOverride(env.PORT, api.server.port, 'PORT', { minimum: 1 });
  const requestTimeoutSetting = parseIntegerOverride(env.REQUEST_TIMEOUT_MS, api.server.request_timeout_ms, 'REQUEST_TIMEOUT_MS', { minimum: 1 });
  const rateLimitMaxSecSetting = parseIntegerOverride(env.RATE_LIMIT_MAX_SEC, api.rate_limits.per_second.max, 'RATE_LIMIT_MAX_SEC');
  const rateLimitMaxMinSetting = parseIntegerOverride(env.RATE_LIMIT_MAX_MIN, api.rate_limits.per_minute.max, 'RATE_LIMIT_MAX_MIN');
  const { value: port } = portSetting;
  const { value: requestTimeoutMs } = requestTimeoutSetting;
  const { value: rateLimitMaxSec } = rateLimitMaxSecSetting;
  const { value: rateLimitMaxMin } = rateLimitMaxMinSetting;
  const activeOverrides = [
    ...(configDirCli ? ['--config-dir'] : configDirEnv ? ['EDGEWARN_CONFIG_DIR'] : []),
    ...(canonicalCli ? ['--base-dir'] : canonicalEnv ? ['EDGEWARN_BASE_DIR'] : deprecatedCli ? ['--base_dir'] : deprecatedEnv ? ['BASE_DIR'] : []),
    ...(portSetting.overridden ? ['PORT'] : []),
    ...(requestTimeoutSetting.overridden ? ['REQUEST_TIMEOUT_MS'] : []),
    ...(rateLimitMaxSecSetting.overridden ? ['RATE_LIMIT_MAX_SEC'] : []),
    ...(rateLimitMaxMinSetting.overridden ? ['RATE_LIMIT_MAX_MIN'] : []),
    ...(env.ALLOWED_ORIGINS === undefined ? [] : ['ALLOWED_ORIGINS']),
    ...(env.TRUST_PROXY_IPS ? ['TRUST_PROXY_IPS'] : env.TRUST_PROXY ? ['TRUST_PROXY'] : []),
  ];
  const diagnostics = Object.freeze({
    source: Object.freeze({
      file: apiProvenance.path,
      schemaVersion: apiProvenance.schema_version,
      root: resolvedConfigRoot,
      // ancillary.js derives the surface-analysis filenames from wpc.yaml, making it
      // a second effective source that /health/live would otherwise not name.
      wpc: Object.freeze({ file: wpcProvenance.path, schemaVersion: wpcProvenance.schema_version }),
    }),
    overrides: Object.freeze(activeOverrides),
    restartRequired: true,
    effective: Object.freeze({
      baseDir,
      port,
      requestTimeoutMs,
      rateLimits: Object.freeze({ perSecond: rateLimitMaxSec, perMinute: rateLimitMaxMin }),
      allowedOriginCount: env.ALLOWED_ORIGINS === undefined ? api.security.allowed_origins.length : parseOrigins(env.ALLOWED_ORIGINS).length,
      renderProductCount: api.product_catalog.entries,
      radarProductCount: api.validation.radar_products.length,
    }),
  });
  return Object.freeze({
    baseDir,
    dataDir: path.join(baseDir, 'data'),
    guiDir: path.join(baseDir, 'gui'),
    wpcDir: path.join(baseDir, 'wpc'),
    configDir: resolvedConfigRoot,
    repoDir: repoRoot(selectedConfigDir),
    openApiPath: resolveSourcePath(api.server.openapi_spec, 'openapi_spec'),
    api,
    wpc,
    diagnostics,
    port, packageVersion, isProduction: env.NODE_ENV === 'production', requestTimeoutMs,
    allowedOrigins: parseOrigins(env.ALLOWED_ORIGINS === undefined ? api.security.allowed_origins : env.ALLOWED_ORIGINS),
    trustProxy: parseTrustProxy(env.TRUST_PROXY_IPS || env.TRUST_PROXY || api.security.trust_proxy, env),
    rateLimits: Object.freeze({
      perSecond: rateLimitMaxSec,
      perMinute: rateLimitMaxMin,
      perSecondWindowMs: api.rate_limits.per_second.window_ms,
      perMinuteWindowMs: api.rate_limits.per_minute.window_ms,
      standardHeaders: api.rate_limits.standard_headers,
      legacyHeaders: api.rate_limits.legacy_headers,
    })
  });
}
