import { readFileSync } from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { configRoot, expandPath, getProvenance, loadConfig, repoRoot, srcRoot, validateAllConfigs } from '../../config/loader.js';
import { requireCompletedMigration } from '../../config/mrms-migration.js';

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

// proxy-addr masks a configured trust subnet to its prefix. A subnet whose
// masked network is all-zero therefore matches every peer: `::ffff:10.0.0.0/8`
// (the correct spelling is /104) and `::/1` both compile to `::`. Express then
// believes any client's X-Forwarded-For, which defeats rate limiting and audit
// logging. Fail at startup instead of silently trusting the internet as a proxy.
function ipv6Words(address) {
  let text = address.toLowerCase();
  if (!/^[0-9a-f:.]+$/.test(text)) return null;
  let embedded = [];
  if (text.includes('.')) {
    const lastColon = text.lastIndexOf(':');
    if (lastColon === -1) return null; // plain IPv4 is handled by proxy-addr
    const octets = text.slice(lastColon + 1).split('.').map(Number);
    if (octets.length !== 4 || octets.some((octet) => !Number.isInteger(octet) || octet < 0 || octet > 255)) return null;
    embedded = [(octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3]];
    text = `${text.slice(0, lastColon + 1)}0:0`;
  }
  const halves = text.split('::');
  if (halves.length > 2) return null;
  const parse = (part) => (part ? part.split(':') : []);
  const before = parse(halves[0]);
  const after = halves.length === 2 ? parse(halves[1]) : [];
  const missing = 8 - before.length - after.length;
  if (halves.length === 2 && missing < 1) return null;
  const groups = halves.length === 2
    ? [...before, ...Array(missing).fill('0'), ...after]
    : [...before, ...after];
  if (groups.length !== 8 || groups.some((group) => !/^[0-9a-f]{1,4}$/.test(group))) return null;
  const words = groups.map((group) => parseInt(group, 16));
  if (embedded.length) {
    words[6] = embedded[0];
    words[7] = embedded[1];
  }
  return words;
}

function networkIsZero(words, prefix) {
  return words.every((word, index) => {
    const bits = Math.min(16, Math.max(0, prefix - index * 16));
    const mask = bits === 0 ? 0 : (0xffff << (16 - bits)) & 0xffff;
    return (word & mask) === 0;
  });
}

function validateTrustSubnet(entry) {
  if (typeof entry !== 'string' || !entry.includes(':') || !entry.includes('/')) return;
  const [address, prefixText, ...rest] = entry.split('/');
  if (rest.length) throw new Error(`Invalid TRUST_PROXY subnet: ${entry}`);
  const prefix = prefixText === '' ? 128 : Number(prefixText);
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 128) throw new Error(`Invalid TRUST_PROXY subnet: ${entry}`);
  const words = ipv6Words(address);
  if (words && prefix < 128 && networkIsZero(words, prefix)) {
    throw new Error(`Invalid TRUST_PROXY subnet ${entry}: it masks to :: and would trust every client; use plain IPv4 notation (for example 10.0.0.0/8) or a specific IPv6 subnet`);
  }
}

function validateTrustEntries(entries) {
  for (const entry of entries) validateTrustSubnet(entry);
  return entries;
}

export function parseTrustProxy(value, env = {}) {
  if (value === false || value === undefined || value === '') return false;
  if (Number.isInteger(value)) return value;
  if (Array.isArray(value)) return validateTrustEntries(value);
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
    return Object.freeze(validateTrustEntries(value.split(',').map((entry) => entry.trim()).filter(Boolean)));
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
  requireCompletedMigration(configRoot(selectedConfigDir));
  validateAllConfigs({ configDir: selectedConfigDir });
  const api = loadConfig('api', { configDir: selectedConfigDir });
  const filesystem = loadConfig('filesystem', { configDir: selectedConfigDir });
  const resolvedConfigRoot = configRoot(selectedConfigDir);
  const apiProvenance = getProvenance('api', { configDir: selectedConfigDir });
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
    }),
    overrides: Object.freeze(activeOverrides),
    restartRequired: true,
    effective: Object.freeze({
      baseDir,
      port,
      requestTimeoutMs,
      rateLimits: Object.freeze({ perSecond: rateLimitMaxSec, perMinute: rateLimitMaxMin }),
      allowedOriginCount: env.ALLOWED_ORIGINS === undefined ? api.security.allowed_origins.length : parseOrigins(env.ALLOWED_ORIGINS).length,
      radarProductCount: api.validation.radar_products.length,
    }),
  });
  return Object.freeze({
    baseDir,
    dataDir: path.join(baseDir, 'data'),
    guiDir: path.join(baseDir, 'gui'),
    configDir: resolvedConfigRoot,
    repoDir: repoRoot(selectedConfigDir),
    openApiPath: resolveSourcePath(api.server.openapi_spec, 'openapi_spec'),
    api,
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
