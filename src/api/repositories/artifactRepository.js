import fs from 'fs/promises';
import { constants } from 'fs';
import path from 'path';
import { LRUCache } from 'lru-cache';

export class ArtifactError extends Error {
  constructor(code, message, { cause } = {}) {
    super(message, { cause });
    this.code = code;
    this.status = code === 'NOT_FOUND' ? 404 : ['INVALID_ARTIFACT', 'IN_PROGRESS', 'MODULE_ROUTE_UNAVAILABLE'].includes(code) ? 503 : 400;
  }
}

const ARTIFACT_KINDS = Object.freeze(['json', 'binary', 'image']);
const SAFE_SEGMENT = /^[A-Za-z0-9_.-]+$/;

function assertSegments(segments) {
  if (!Array.isArray(segments) || !segments.length || segments.some((segment) => typeof segment !== 'string' || !SAFE_SEGMENT.test(segment) || segment === '.' || segment === '..')) {
    throw new ArtifactError('INVALID_PATH', 'Invalid artifact path');
  }
}

export class ArtifactRepository {
  constructor(roots, limits, cache, listLimit) {
    this.roots = Object.freeze({ ...roots });
    // A missing kind would leave `stat.size > this.limits[kind]` vacuously false and
    // so read an unbounded artifact; api.yaml is the only base default, so an
    // incomplete map is a caller error rather than something to fill in here.
    const missing = ARTIFACT_KINDS.filter((kind) => typeof limits?.[kind] !== 'number');
    if (missing.length) throw new TypeError(`ArtifactRepository requires numeric size limits for: ${missing.join(', ')}`);
    if (typeof listLimit !== 'number') throw new TypeError('ArtifactRepository requires a numeric listLimit');
    if (typeof cache?.max_entries !== 'number' || typeof cache?.max_size_bytes !== 'number') {
      throw new TypeError('ArtifactRepository requires numeric cache max_entries and max_size_bytes');
    }
    this.limits = Object.freeze({ ...limits });
    this.listLimit = listLimit;
    this.realRoots = new Map();
    this.jsonCache = new LRUCache({
      max: cache.max_entries,
      maxSize: cache.max_size_bytes,
      sizeCalculation: (entry) => entry.size,
    });
  }

  async root(rootName) {
    if (!Object.hasOwn(this.roots, rootName)) throw new ArtifactError('INVALID_PATH', 'Unknown artifact root');
    if (this.realRoots.has(rootName)) return this.realRoots.get(rootName);
    try {
      const real = await fs.realpath(this.roots[rootName]);
      const stat = await fs.lstat(real);
      if (!stat.isDirectory() || stat.isSymbolicLink()) throw new ArtifactError('INVALID_PATH', 'Invalid artifact root');
      this.realRoots.set(rootName, real);
      return real;
    } catch (error) {
      if (error instanceof ArtifactError) throw error;
      if (error.code === 'ENOENT') throw new ArtifactError('NOT_FOUND', 'Artifact root not found', { cause: error });
      throw new ArtifactError('INVALID_PATH', 'Artifact root unavailable', { cause: error });
    }
  }

  async open(rootName, segments, { kind = 'binary' } = {}) {
    assertSegments(segments);
    const root = await this.root(rootName);
    const filePath = path.join(root, ...segments);
    if (path.relative(root, filePath).startsWith('..')) throw new ArtifactError('INVALID_PATH', 'Artifact escapes root');
    let handle;
    try {
      let current = root;
      for (let index = 0; index < segments.length; index += 1) {
        current = path.join(current, segments[index]);
        const stat = await fs.lstat(current);
        if (stat.isSymbolicLink() || (index < segments.length - 1 && !stat.isDirectory())) {
          throw new ArtifactError('INVALID_PATH', 'Symbolic links and non-directory path components are not allowed');
        }
      }
      handle = await fs.open(filePath, constants.O_RDONLY | constants.O_NOFOLLOW);
      const stat = await handle.stat();
      if (!stat.isFile()) throw new ArtifactError('INVALID_PATH', 'Artifact is not a regular file');
      if (stat.size > this.limits[kind]) throw new ArtifactError('INVALID_ARTIFACT', 'Artifact exceeds maximum size');
      return { handle, size: stat.size, path: filePath, etag: `W/\"${stat.size}-${Math.trunc(stat.mtimeMs)}-${stat.ino}\"` };
    } catch (error) {
      await handle?.close();
      if (error instanceof ArtifactError) throw error;
      if (error.code === 'ENOENT') throw new ArtifactError('NOT_FOUND', 'Artifact not found', { cause: error });
      if (error.code === 'ELOOP') throw new ArtifactError('INVALID_PATH', 'Symbolic links are not allowed', { cause: error });
      throw new ArtifactError('INVALID_PATH', 'Artifact cannot be opened', { cause: error });
    }
  }

  async list(rootName, segments = [], { limit = this.listLimit } = {}) {
    if (segments.length) assertSegments(segments);
    const root = await this.root(rootName);
    const directory = path.join(root, ...segments);
    if (path.relative(root, directory).startsWith('..')) throw new ArtifactError('INVALID_PATH', 'Artifact escapes root');
    try {
      let current = root;
      let stat;
      for (const segment of segments) {
        current = path.join(current, segment);
        stat = await fs.lstat(current);
        if (!stat.isDirectory() || stat.isSymbolicLink()) throw new ArtifactError('INVALID_PATH', 'Invalid artifact directory');
      }
      stat = stat || await fs.lstat(directory);
      if (!stat.isDirectory() || stat.isSymbolicLink()) throw new ArtifactError('INVALID_PATH', 'Invalid artifact directory');
      const entries = await fs.readdir(directory, { withFileTypes: true });
      return entries.filter((entry) => !entry.isSymbolicLink()).slice(0, limit);
    } catch (error) {
      if (error instanceof ArtifactError) throw error;
      if (error.code === 'ENOENT') throw new ArtifactError('NOT_FOUND', 'Artifact directory not found', { cause: error });
      throw new ArtifactError('INVALID_PATH', 'Artifact directory unavailable', { cause: error });
    }
  }

  async readJson(rootName, segments, options = {}) {
    const opened = await this.open(rootName, segments, { kind: 'json', ...options });
    try {
      const key = `${rootName}:${opened.path}`;
      const cached = this.jsonCache.get(key);
      if (cached?.etag === opened.etag) return cached.value;
      const text = await opened.handle.readFile({ encoding: 'utf8' });
      const value = JSON.parse(text);
      this.jsonCache.set(key, { etag: opened.etag, value, size: Buffer.byteLength(text) });
      return value;
    } catch (error) {
      if (error instanceof ArtifactError) throw error;
      throw new ArtifactError('IN_PROGRESS', 'Artifact is malformed or still being published', { cause: error });
    } finally {
      await opened.handle.close();
    }
  }
}
