import { validateMrmsDocument, MIGRATION_HINT } from './mrms-products.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import yaml from 'js-yaml';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const ENV_CONFIG_DIR = 'EDGEWARN_CONFIG_DIR';

export const CONFIG_NAMES = Object.freeze([
  'runtime', 'historical', 'filesystem', 'detection', 'lineage',
  'integration', 'scheduler', 'api_index', 'ingest', 'nexrad', 'synoptic_rap',
  'wpc', 'metar', 'nws', 'ewmrs_render', 'ewmrs_pipeline',
  'api', 'kalman',
]);

export class ConfigError extends Error {
  constructor(filename, dottedPath, message) {
    super(dottedPath ? `${filename}: ${dottedPath}: ${message}` : `${filename}: ${message}`);
    this.name = 'ConfigError';
    this.filename = filename;
    this.dottedPath = dottedPath;
  }
}

const configCache = new Map();
const provenanceCache = new Map();

// Hand-rolled schema walker, mirroring src/common/config/loader.py's _walk.
// Supports exactly the keywords used by config/schema/*.schema.json today;
// anything else is a startup error rather than a silently-unenforced constraint.
const KNOWN_SCHEMA_KEYWORDS = new Set([
  '$schema', 'title', 'description',
  'type', 'properties', 'required', 'additionalProperties',
  'items', 'minItems', 'maxItems', 'uniqueItems',
  'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
  'const', 'enum', 'pattern',
]);

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, index) => deepEqual(item, b[index]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const aKeys = Object.keys(a);
    const bKeys = Object.keys(b);
    return aKeys.length === bKeys.length
      && aKeys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && deepEqual(a[key], b[key]));
  }
  return false;
}

function hasDuplicates(items) {
  for (let i = 0; i < items.length; i += 1) {
    for (let j = i + 1; j < items.length; j += 1) {
      if (deepEqual(items[i], items[j])) return true;
    }
  }
  return false;
}

function typeMatches(value, typeName) {
  switch (typeName) {
    case 'object': return isPlainObject(value);
    case 'array': return Array.isArray(value);
    case 'string': return typeof value === 'string';
    case 'boolean': return typeof value === 'boolean';
    case 'integer': return typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value);
    case 'number': return typeof value === 'number' && Number.isFinite(value);
    case 'null': return value === null;
    default: throw new ConfigError('<schema>', null, `unsupported schema type ${JSON.stringify(typeName)}`);
  }
}

function checkSupportedKeywords(schemaPath, node, pathParts) {
  if (!isPlainObject(node)) return;
  const unknown = Object.keys(node).filter((key) => !KNOWN_SCHEMA_KEYWORDS.has(key)).sort();
  if (unknown.length > 0) {
    throw new ConfigError(schemaPath, dottedPath(pathParts), `unsupported schema keyword(s) ${JSON.stringify(unknown)}`);
  }
  for (const [propName, propSchema] of Object.entries(node.properties || {})) {
    checkSupportedKeywords(schemaPath, propSchema, [...pathParts, propName]);
  }
  if (isPlainObject(node.additionalProperties)) {
    checkSupportedKeywords(schemaPath, node.additionalProperties, [...pathParts, 'additionalProperties']);
  }
  if (isPlainObject(node.items)) {
    checkSupportedKeywords(schemaPath, node.items, [...pathParts, 'items']);
  }
}

function walk(schema, value, pathParts, errors) {
  const typeSpec = schema.type;
  if (typeSpec !== undefined) {
    const typeNames = Array.isArray(typeSpec) ? typeSpec : [typeSpec];
    if (!typeNames.some((typeName) => typeMatches(value, typeName))) {
      errors.push([pathParts, `${JSON.stringify(value)} is not of type ${typeNames.join(' or ')}`]);
      return;
    }
  }

  if ('const' in schema && !deepEqual(value, schema.const)) {
    errors.push([pathParts, `must equal ${JSON.stringify(schema.const)}`]);
  }
  if ('enum' in schema && !schema.enum.some((option) => deepEqual(value, option))) {
    errors.push([pathParts, `must be one of ${JSON.stringify(schema.enum)}`]);
  }

  if (isPlainObject(value)) {
    for (const key of schema.required || []) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        errors.push([[...pathParts, key], 'is a required property']);
      }
    }
    const properties = schema.properties || {};
    const additional = 'additionalProperties' in schema ? schema.additionalProperties : true;
    for (const [key, subValue] of Object.entries(value)) {
      if (Object.prototype.hasOwnProperty.call(properties, key)) {
        walk(properties[key], subValue, [...pathParts, key], errors);
      } else if (additional === false) {
        errors.push([[...pathParts, key], 'additional properties are not allowed']);
      } else if (isPlainObject(additional)) {
        walk(additional, subValue, [...pathParts, key], errors);
      }
    }
  } else if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      errors.push([pathParts, `must have at least ${schema.minItems} item(s)`]);
    }
    if (schema.maxItems !== undefined && value.length > schema.maxItems) {
      errors.push([pathParts, `must have at most ${schema.maxItems} item(s)`]);
    }
    if (schema.uniqueItems && hasDuplicates(value)) {
      errors.push([pathParts, 'items must be unique']);
    }
    if (schema.items !== undefined) {
      value.forEach((item, index) => walk(schema.items, item, [...pathParts, index], errors));
    }
  } else if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) {
      errors.push([pathParts, `must be >= ${schema.minimum}`]);
    }
    if (schema.maximum !== undefined && value > schema.maximum) {
      errors.push([pathParts, `must be <= ${schema.maximum}`]);
    }
    if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
      errors.push([pathParts, `must be > ${schema.exclusiveMinimum}`]);
    }
    if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) {
      errors.push([pathParts, `must be < ${schema.exclusiveMaximum}`]);
    }
  } else if (typeof value === 'string' && schema.pattern !== undefined && !new RegExp(schema.pattern).test(value)) {
    errors.push([pathParts, `does not match pattern ${JSON.stringify(schema.pattern)}`]);
  }
}

function findConfigRootByWalkingUp() {
  let current = __dirname;
  while (true) {
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
    const configDir = path.join(current, 'config');
    if (fs.existsSync(path.join(configDir, 'runtime.yaml'))) {
      return configDir;
    }
  }
  throw new ConfigError('config/', null, `could not locate a config/ directory containing runtime.yaml by walking up from ${__dirname}`);
}

export function configRoot(cliDir = null) {
  if (cliDir) return resolveGivenRoot(cliDir, '--config-dir does not contain runtime.yaml');
  const envDir = process.env[ENV_CONFIG_DIR];
  if (envDir) return resolveGivenRoot(envDir, `${ENV_CONFIG_DIR} does not contain runtime.yaml`);
  return findConfigRootByWalkingUp();
}

function resolveGivenRoot(raw, message) {
  const resolved = path.resolve(raw);
  if (!fs.existsSync(path.join(resolved, 'runtime.yaml'))) {
    throw new ConfigError(resolved, null, message);
  }
  return resolved;
}

export function repoRoot(cliDir = null) {
  return path.dirname(configRoot(cliDir));
}

export function srcRoot() {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
}

// Mirrors PATH_TOKENS and expand_path in src/common/config/loader.py. An
// allowlist rather than a scan for bare `<`/`>`, which would false-positive on
// comment lines and on synoptic_rap.yaml's named capture groups.
export const PATH_TOKENS = Object.freeze(['base_dir', 'gui_dir', 'src_dir']);

export function expandPath(template, roots, { filename, dottedPath }) {
  if (typeof template !== 'string') {
    throw new ConfigError(filename, dottedPath, `expected a path string, got ${JSON.stringify(template)}`);
  }
  // Checked before the prefix match so a Windows-style template is reported as
  // the separator problem it is, rather than as a malformed remainder.
  if (template.includes('\\')) {
    throw new ConfigError(filename, dottedPath, `${JSON.stringify(template)} must use '/' separators`);
  }
  // A NUL is never valid in a path. Node throws only once one reaches an fs call,
  // and expandPath returns a string that may be stored or logged well before that,
  // so it is rejected at the same point the Python side rejects it.
  if (template.includes('\0')) {
    throw new ConfigError(filename, dottedPath, 'path contains a NUL byte');
  }
  const match = /^<([a-z_]+)>\//.exec(template);
  if (match === null) {
    const expected = PATH_TOKENS.map((token) => `<${token}>/`).join(', ');
    throw new ConfigError(filename, dottedPath, `${JSON.stringify(template)} must begin with one of ${expected}`);
  }
  const token = match[1];
  if (!PATH_TOKENS.includes(token)) {
    throw new ConfigError(filename, dottedPath, `<${token}> is not an expandable path token`);
  }
  if (!Object.prototype.hasOwnProperty.call(roots, token)) {
    throw new ConfigError(filename, dottedPath, `<${token}> has no value in this context`);
  }
  const remainder = template.slice(match[0].length);
  const segments = remainder.split('/');
  if (remainder === '' || remainder.startsWith('/') || segments.includes('..')) {
    throw new ConfigError(filename, dottedPath, `${JSON.stringify(remainder)} is not a relative path below <${token}>`);
  }
  // Kept after the textual check rather than instead of it. This is the guard
  // the API config has always used, and it stays as the backstop that fails on
  // anything path.resolve normalizes out of the root; the check above is what
  // rejects a hostile template before any path is built from it. Unlike the
  // Python side, path.resolve does not follow symlinks, so this is defense in
  // depth rather than a second class of input.
  // An empty or relative root would make path.resolve fall back to process.cwd(),
  // reintroducing the working-directory dependence the mandatory token exists to
  // prevent -- and silently, since the result is still a plausible path.
  const givenRoot = roots[token];
  if (!givenRoot || !path.isAbsolute(givenRoot)) {
    throw new ConfigError(filename, dottedPath, `<${token}> must be an absolute directory, got ${JSON.stringify(givenRoot)}`);
  }
  const root = fs.realpathSync(givenRoot);
  const candidate = path.resolve(root, remainder);
  // realpath requires the final path to exist. Resolve the deepest existing
  // ancestor, then append the as-yet-uncreated suffix so future artifact paths
  // get the same symlink-containment protection as existing files.
  let existing = candidate;
  const suffix = [];
  while (!fs.existsSync(existing)) {
    const parent = path.dirname(existing);
    if (parent === existing) break;
    suffix.unshift(path.basename(existing));
    existing = parent;
  }
  const resolved = path.join(fs.realpathSync(existing), ...suffix);
  const relative = path.relative(root, resolved);
  if (relative === '' || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new ConfigError(filename, dottedPath, `${JSON.stringify(template)} resolves outside <${token}>`);
  }
  return resolved;
}

function deepFreeze(value) {
  if (Array.isArray(value)) {
    return Object.freeze(value.map(deepFreeze));
  }
  if (value !== null && typeof value === 'object') {
    const frozen = {};
    for (const [key, val] of Object.entries(value)) {
      frozen[key] = deepFreeze(val);
    }
    return Object.freeze(frozen);
  }
  return value;
}

function dottedPath(pathParts) {
  const parts = [];
  for (const part of pathParts) {
    if (typeof part === 'number') {
      parts[parts.length - 1] = `${parts[parts.length - 1]}[${part}]`;
    } else {
      parts.push(String(part));
    }
  }
  return parts.join('.') || null;
}

export function validateDocument(name, document, schemaPath) {
  if (name === 'ingest' && document.schema_version === 2) schemaPath = path.join(path.dirname(schemaPath), 'ingest.v2.schema.json');
  if (!fs.existsSync(schemaPath)) {
    throw new ConfigError(`${name}.yaml`, null, `missing schema file: ${schemaPath}`);
  }
  const schema = JSON.parse(fs.readFileSync(schemaPath, 'utf8'));
  checkSupportedKeywords(schemaPath, schema, []);

  const errors = [];
  walk(schema, document, [], errors);
  if (errors.length > 0) {
    errors.sort((a, b) => {
      const aKey = a[0].map(String);
      const bKey = b[0].map(String);
      if (aKey.length !== bKey.length) return aKey.length - bKey.length;
      return aKey.join(' ').localeCompare(bKey.join(' '));
    });
    const [firstPath, firstMessage] = errors[0];
    throw new ConfigError(`${name}.yaml`, dottedPath(firstPath), firstMessage + (name === 'ingest' ? ` ${MIGRATION_HINT}` : ''));
  }
  try { validateMrmsDocument(name, document); }
  catch (error) { throw new ConfigError(`${name}.yaml`, null, error.message); }
}

export function resetCache() {
  configCache.clear();
  provenanceCache.clear();
}

export function validateAllConfigs({ configDir = null } = {}) {
  const root = configRoot(configDir);
  for (const name of CONFIG_NAMES) loadConfig(name, { configDir: root });
  return Object.freeze(CONFIG_NAMES.map((name) => loadConfig(name, { configDir: root })));
}

export function loadConfig(name, { configDir = null } = {}) {
  const root = configRoot(configDir);
  const cacheKey = `${root}::${name}`;
  if (configCache.has(cacheKey)) {
    return configCache.get(cacheKey);
  }

  const yamlPath = path.join(root, `${name}.yaml`);
  if (!fs.existsSync(yamlPath)) {
    throw new ConfigError(`${name}.yaml`, null, `missing config file: ${yamlPath}`);
  }

  const document = yaml.load(fs.readFileSync(yamlPath, 'utf8'));
  if (document === null || typeof document !== 'object' || Array.isArray(document)) {
    throw new ConfigError(`${name}.yaml`, null, 'expected a YAML mapping at the document root');
  }

  const schemaPath = path.join(root, 'schema', `${name}.schema.json`);
  validateDocument(name, document, schemaPath);

  const frozen = deepFreeze(document);
  configCache.set(cacheKey, frozen);
  provenanceCache.set(cacheKey, { path: yamlPath, schema_version: document.schema_version });
  return frozen;
}

export function getProvenance(name, { configDir = null } = {}) {
  const root = configRoot(configDir);
  const cacheKey = `${root}::${name}`;
  if (!provenanceCache.has(cacheKey)) {
    throw new ConfigError(`${name}.yaml`, null, 'config has not been loaded yet');
  }
  return provenanceCache.get(cacheKey);
}
