import { afterEach, describe, expect, it, jest } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import request from 'supertest';
import { createConfig, parseTrustProxy } from '../../src/api/config/index.js';
import { createApp } from '../../src/api/app.js';
import { resetCache } from '../../src/config/loader.js';
import { ArtifactRepository, ArtifactError } from '../../src/api/repositories/artifactRepository.js';

// api.yaml is the only base default for these, so the constructor no longer fills
// them in and every caller -- including a test -- states them.
const REPOSITORY_LIMITS = { json: 8 * 1024 * 1024, binary: 128 * 1024 * 1024, image: 32 * 1024 * 1024 };
const REPOSITORY_CACHE = { max_entries: 256, max_size_bytes: 32 * 1024 * 1024 };
const REPOSITORY_LIST_LIMIT = 1000;

describe('unified API configuration', () => {
  it('uses one resolved base directory with documented precedence', () => {
    const config = createConfig({ env: { EDGEWARN_BASE_DIR: '/tmp/edgewarn', BASE_DIR: '/tmp/edgewarn', PORT: '5001' }, argv: [] });
    expect(config.baseDir).toBe('/tmp/edgewarn');
    expect(config.port).toBe(5001);
    expect(createConfig({ env: { EDGEWARN_BASE_DIR: '/tmp/a', BASE_DIR: '/tmp/b' }, argv: [] }).baseDir).toBe('/tmp/a');
    expect(createConfig({ env: { EDGEWARN_BASE_DIR: '/tmp/a' }, argv: ['--base_dir', '/tmp/cli'] }).baseDir).toBe('/tmp/cli');
  });

  it('uses exact origins and rejects unsafe production proxy shorthand', () => {
    const config = createConfig({ env: { ALLOWED_ORIGINS: 'https://one.example,https://two.example' }, argv: [] });
    expect(config.allowedOrigins).toEqual(['https://one.example', 'https://two.example']);
    expect(() => createConfig({ env: { NODE_ENV: 'production', TRUST_PROXY: 'true' }, argv: [] })).toThrow('TRUST_PROXY=true');
  });

  it('parses every trust-proxy input shape consistently', () => {
    expect(parseTrustProxy(false)).toBe(false);
    expect(parseTrustProxy(undefined)).toBe(false);
    expect(parseTrustProxy('')).toBe(false);
    expect(parseTrustProxy(2)).toBe(2);
    expect(parseTrustProxy(['loopback'])).toEqual(['loopback']);
    expect(parseTrustProxy(true)).toBe(1);
    expect(parseTrustProxy(' True ')).toBe(1);
    expect(parseTrustProxy('2')).toBe(2);
    expect(parseTrustProxy('false')).toBe(false);
    expect(parseTrustProxy('loopback, 10.0.0.1')).toEqual(['loopback', '10.0.0.1']);
    expect(() => parseTrustProxy(true, { NODE_ENV: 'production' })).toThrow('TRUST_PROXY=true');
    expect(() => parseTrustProxy('9')).toThrow('Invalid TRUST_PROXY hop count');
    expect(createConfig({ env: { TRUST_PROXY_IPS: '' }, argv: [] }).diagnostics.overrides).not.toContain('TRUST_PROXY_IPS');
  });

  it('uses api.yaml defaults from an explicitly selected config root', async () => {
    const parent = await fs.mkdtemp(path.join(os.tmpdir(), 'edgewarn-api-config-'));
    const configDir = path.join(parent, 'config');
    try {
      await fs.cp(path.resolve('config'), configDir, { recursive: true });
      const apiYaml = path.join(configDir, 'api.yaml');
      const yaml = await fs.readFile(apiYaml, 'utf8');
      await fs.writeFile(apiYaml, yaml.replace('port: 5000', 'port: 5100'));

      const config = createConfig({ env: {}, argv: ['--config-dir', configDir] });
      expect(config.port).toBe(5100);
      expect(config.configDir).toBe(configDir);
      expect(config.api.server.port).toBe(5100);
      expect(config.openApiPath).toBe(path.resolve('src/api/openapi/v3.yaml'));
    } finally {
      await fs.rm(parent, { recursive: true, force: true });
    }
  });

  it('serves OpenAPI from the installed source tree with an external config directory', async () => {
    const parent = await fs.mkdtemp(path.join(os.tmpdir(), 'edgewarn-api-config-'));
    const configDir = path.join(parent, 'config');
    const baseDir = path.join(parent, 'runtime');
    try {
      await fs.cp(path.resolve('config'), configDir, { recursive: true });
      await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory), { recursive: true })));
      const { app, config } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir }, argv: ['--config-dir', configDir] });
      expect(config.openApiPath).toBe(path.resolve('src/api/openapi/v3.yaml'));
      await request(app).get('/api/v3/openapi.json').expect(200).expect('Content-Type', /application\/json/);
    } finally {
      resetCache();
      await fs.rm(parent, { recursive: true, force: true });
    }
  });

  it('can disable access logging through api.yaml', async () => {
    const parent = await fs.mkdtemp(path.join(os.tmpdir(), 'edgewarn-api-config-'));
    const configDir = path.join(parent, 'config');
    const baseDir = path.join(parent, 'runtime');
    const log = jest.spyOn(console, 'info').mockImplementation(() => {});
    try {
      await fs.cp(path.resolve('config'), configDir, { recursive: true });
      const apiYaml = path.join(configDir, 'api.yaml');
      await fs.writeFile(apiYaml, (await fs.readFile(apiYaml, 'utf8')).replace('access_log_enabled: true', 'access_log_enabled: false'));
      await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory), { recursive: true })));
      const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir }, argv: ['--config-dir', configDir] });
      await request(app).get('/').expect(200);
      expect(log).not.toHaveBeenCalled();
    } finally {
      log.mockRestore();
      resetCache();
      await fs.rm(parent, { recursive: true, force: true });
    }
  });

  it('sends the HSTS max age configured in api.yaml', async () => {
    const parent = await fs.mkdtemp(path.join(os.tmpdir(), 'edgewarn-api-config-'));
    const configDir = path.join(parent, 'config');
    const baseDir = path.join(parent, 'runtime');
    try {
      await fs.cp(path.resolve('config'), configDir, { recursive: true });
      const apiYaml = path.join(configDir, 'api.yaml');
      await fs.writeFile(apiYaml, (await fs.readFile(apiYaml, 'utf8')).replace('hsts_max_age_seconds: 31536000', 'hsts_max_age_seconds: 120'));
      await Promise.all(['data', 'gui', 'wpc'].map((directory) => fs.mkdir(path.join(baseDir, directory), { recursive: true })));
      const { app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir }, argv: ['--config-dir', configDir] });
      await request(app).get('/').expect(200).expect('Strict-Transport-Security', 'max-age=120; includeSubDomains');
    } finally {
      resetCache();
      await fs.rm(parent, { recursive: true, force: true });
    }
  });

  it('rejects invalid YAML trust-proxy forms before API startup', async () => {
    const parent = await fs.mkdtemp(path.join(os.tmpdir(), 'edgewarn-api-config-'));
    const configDir = path.join(parent, 'config');
    try {
      await fs.cp(path.resolve('config'), configDir, { recursive: true });
      const apiYaml = path.join(configDir, 'api.yaml');
      const yaml = await fs.readFile(apiYaml, 'utf8');

      await fs.writeFile(apiYaml, yaml.replace('trust_proxy: false', 'trust_proxy: true'));
      expect(createConfig({ env: {}, argv: ['--config-dir', configDir] }).trustProxy).toBe(1);
      expect(() => createConfig({ env: { NODE_ENV: 'production' }, argv: ['--config-dir', configDir] })).toThrow('TRUST_PROXY=true');

      await fs.writeFile(apiYaml, yaml.replace('trust_proxy: false', 'trust_proxy: []'));
      resetCache();
      expect(() => createConfig({ env: {}, argv: ['--config-dir', configDir] })).toThrow();
    } finally {
      await fs.rm(parent, { recursive: true, force: true });
    }
  });
});

describe('ArtifactRepository', () => {
  let root;
  afterEach(async () => { if (root) await fs.rm(root, { recursive: true, force: true }); });

  it('reads a bounded regular JSON artifact and rejects symlink escapes', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'artifact-repository-'));
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'artifact-outside-'));
    await fs.writeFile(path.join(root, 'valid.json'), '{"ok":true}');
    await fs.writeFile(path.join(outside, 'secret.json'), '{"secret":true}');
    await fs.symlink(path.join(outside, 'secret.json'), path.join(root, 'escape.json'));
    const repository = new ArtifactRepository({ runtime: root }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT);
    await expect(repository.readJson('runtime', ['valid.json'])).resolves.toEqual({ ok: true });
    await expect(repository.readJson('runtime', ['escape.json'])).rejects.toMatchObject({ code: 'INVALID_PATH' });
    await fs.rm(outside, { recursive: true, force: true });
  });

  it('rejects an intermediate directory symlink during artifact discovery', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'artifact-repository-'));
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'artifact-outside-'));
    await fs.writeFile(path.join(outside, 'index.json'), '{"private":true}');
    await fs.symlink(outside, path.join(root, 'linked-directory'));
    const repository = new ArtifactRepository({ runtime: root }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT);
    await expect(repository.list('runtime', ['linked-directory'])).rejects.toMatchObject({ code: 'INVALID_PATH' });
    await fs.rm(outside, { recursive: true, force: true });
  });

  it('turns malformed and oversized publishing artifacts into bounded errors', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'artifact-repository-'));
    await fs.writeFile(path.join(root, 'broken.json'), '{');
    await fs.writeFile(path.join(root, 'large.json'), 'x'.repeat(64));
    const repository = new ArtifactRepository({ runtime: root }, { ...REPOSITORY_LIMITS, json: 32 }, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT);
    await expect(repository.readJson('runtime', ['broken.json'])).rejects.toBeInstanceOf(ArtifactError);
    await expect(repository.readJson('runtime', ['large.json'])).rejects.toMatchObject({ code: 'INVALID_ARTIFACT' });
  });

  it('caches only a matching artifact identity and observes replacement', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'artifact-repository-'));
    const target = path.join(root, 'index.json');
    await fs.writeFile(target, '{"generation":1}');
    const repository = new ArtifactRepository({ runtime: root }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT);
    await expect(repository.readJson('runtime', ['index.json'])).resolves.toEqual({ generation: 1 });
    await new Promise((resolve) => setTimeout(resolve, 2));
    await fs.writeFile(target, '{"generation":2}');
    await expect(repository.readJson('runtime', ['index.json'])).resolves.toEqual({ generation: 2 });
  });
});
