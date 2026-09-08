import { afterEach, describe, expect, it } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import yaml from 'js-yaml';
import { ConfigError, loadConfig, resetCache } from '../../src/config/loader.js';

const validatorVectors = JSON.parse(
  await fs.readFile(new URL('../fixtures/config/validator_vectors.json', import.meta.url), 'utf8'),
);

// Mirrors tests/core/config/test_loader.py -- same hand-rolled validator,
// same keyword set, ported test-for-test so the two loaders stay in sync.

async function makeConfigDir() {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'edgewarn-config-'));
  await fs.writeFile(path.join(dir, 'runtime.yaml'), 'schema_version: 1\n');
  await fs.mkdir(path.join(dir, 'schema'));
  return dir;
}

async function writeSample(dir, document, schema) {
  await fs.writeFile(path.join(dir, 'sample.yaml'), yaml.dump(document));
  await fs.writeFile(path.join(dir, 'schema', 'sample.schema.json'), JSON.stringify(schema));
}

describe('config loader hand-rolled validator', () => {
  let dir;
  afterEach(async () => {
    resetCache();
    if (dir) await fs.rm(dir, { recursive: true, force: true });
  });

  it('accepts a document that satisfies its schema', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { a: 1 }, {
      type: 'object', required: ['a'], additionalProperties: false, properties: { a: { type: 'integer' } },
    });

    const loaded = loadConfig('sample', { configDir: dir });

    expect(loaded.a).toBe(1);
  });

  it.each(validatorVectors)('matches the shared vector: $name', async (vector) => {
    dir = await makeConfigDir();
    const document = vector.document_token === 'nan' ? { value: Number.NaN } : vector.document;
    await writeSample(dir, document, vector.schema);

    if (vector.valid) {
      expect(loadConfig('sample', { configDir: dir }).value).toBe(0);
    } else {
      expect(() => loadConfig('sample', { configDir: dir })).toThrow(ConfigError);
    }
  });

  it('rejects a missing required property', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, {}, {
      type: 'object', required: ['a'], additionalProperties: false, properties: { a: { type: 'integer' } },
    });

    expect(() => loadConfig('sample', { configDir: dir })).toThrow(ConfigError);
    try {
      loadConfig('sample', { configDir: dir });
    } catch (error) {
      expect(error.dottedPath).toBe('a');
    }
  });

  it('rejects an unexpected property when additionalProperties is false', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { a: 1, typo: 2 }, {
      type: 'object', additionalProperties: false, properties: { a: { type: 'integer' } },
    });

    try {
      loadConfig('sample', { configDir: dir });
      throw new Error('expected loadConfig to throw');
    } catch (error) {
      expect(error).toBeInstanceOf(ConfigError);
      expect(error.dottedPath).toBe('typo');
    }
  });

  it('rejects a type mismatch', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { a: 'not-an-int' }, {
      type: 'object', additionalProperties: false, properties: { a: { type: 'integer' } },
    });

    try {
      loadConfig('sample', { configDir: dir });
      throw new Error('expected loadConfig to throw');
    } catch (error) {
      expect(error.dottedPath).toBe('a');
      expect(error.message).toMatch(/not of type/);
    }
  });

  it('does not accept a boolean as an integer', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { a: true }, {
      type: 'object', additionalProperties: false, properties: { a: { type: 'integer' } },
    });

    expect(() => loadConfig('sample', { configDir: dir })).toThrow(ConfigError);
  });

  it('validates map values via a schema-typed additionalProperties', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { env_overrides: { A: '1', B: 2 } }, {
      type: 'object',
      additionalProperties: false,
      properties: { env_overrides: { type: 'object', additionalProperties: { type: 'string' } } },
    });

    try {
      loadConfig('sample', { configDir: dir });
      throw new Error('expected loadConfig to throw');
    } catch (error) {
      expect(error.dottedPath).toBe('env_overrides.B');
    }
  });

  it('enforces minItems/maxItems and uniqueItems', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { bounds: [1, 1] }, {
      type: 'object',
      additionalProperties: false,
      properties: {
        bounds: { type: 'array', items: { type: 'number' }, minItems: 2, maxItems: 2, uniqueItems: true },
      },
    });

    try {
      loadConfig('sample', { configDir: dir });
      throw new Error('expected loadConfig to throw');
    } catch (error) {
      expect(error.message).toMatch(/unique/);
    }
  });

  it('enforces numeric minimum/maximum/exclusiveMinimum', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { count: 11, ratio: 0 }, {
      type: 'object',
      additionalProperties: false,
      properties: {
        count: { type: 'integer', minimum: 0, maximum: 10 },
        ratio: { type: 'number', exclusiveMinimum: 0 },
      },
    });

    expect(() => loadConfig('sample', { configDir: dir })).toThrow(ConfigError);
  });

  it('enforces string pattern', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { color: 'not-a-hex-color' }, {
      type: 'object',
      additionalProperties: false,
      properties: { color: { type: 'string', pattern: '^#[0-9A-Fa-f]{6}$' } },
    });

    try {
      loadConfig('sample', { configDir: dir });
      throw new Error('expected loadConfig to throw');
    } catch (error) {
      expect(error.message).toMatch(/pattern/);
    }
  });

  it('enforces const and enum', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { schema_version: 2, level: 'extreme' }, {
      type: 'object',
      additionalProperties: false,
      properties: {
        schema_version: { const: 1 },
        level: { enum: ['low', 'medium', 'high'] },
      },
    });

    expect(() => loadConfig('sample', { configDir: dir })).toThrow(ConfigError);
  });

  it('rejects an unsupported schema keyword at load time', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { a: 1 }, {
      type: 'object',
      properties: { a: { oneOf: [{ type: 'integer' }, { type: 'string' }] } },
    });

    try {
      loadConfig('sample', { configDir: dir });
      throw new Error('expected loadConfig to throw');
    } catch (error) {
      expect(error.message).toMatch(/unsupported schema keyword/);
      expect(error.filename).toMatch(/sample\.schema\.json$/);
    }
  });

  it('freezes the loaded document recursively', async () => {
    dir = await makeConfigDir();
    await writeSample(dir, { nested: { items: [1, 2, 3] } }, {
      type: 'object',
      additionalProperties: false,
      properties: {
        nested: {
          type: 'object',
          additionalProperties: false,
          properties: { items: { type: 'array', items: { type: 'integer' } } },
        },
      },
    });

    const loaded = loadConfig('sample', { configDir: dir });

    expect(Object.isFrozen(loaded.nested.items)).toBe(true);
    expect(() => { loaded.nested.items.push(4); }).toThrow();
  });
});
