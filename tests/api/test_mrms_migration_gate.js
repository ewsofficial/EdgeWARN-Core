import { describe, expect, test } from '@jest/globals';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { requireCompletedMigration } from '../../src/config/mrms-migration.js';
import { createConfig } from '../../src/api/config/index.js';

function withRoot(callback) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mrms-migration-gate-'));
  try { callback(root); } finally { fs.rmSync(root, { recursive: true, force: true }); }
}

function writeJournal(root, payload) {
  fs.mkdirSync(path.join(root, '.mrms-migration'), { recursive: true });
  fs.writeFileSync(path.join(root, '.mrms-migration/journal.json'), payload);
}

describe('MRMS migration startup gate', () => {
  test('absent journals require no writes', () => withRoot((root) => {
    requireCompletedMigration(root);
    expect(fs.readdirSync(root)).toEqual([]);
  }));

  test.each(['complete', 'rolled-back'])('allows terminal status %s', (status) => withRoot((root) => {
    writeJournal(root, JSON.stringify({ schema_version: 1, status }));
    expect(() => requireCompletedMigration(root)).not.toThrow();
  }));

  test.each(['not JSON', '[]', 'null', '{}',
    '{"schema_version":2,"status":"complete"}',
    '{"schema_version":true,"status":"complete"}',
    '{"schema_version":1,"status":"applying"}',
    '{"schema_version":1,"status":"rolling-back"}',
  ])('refuses invalid or unfinished journal %s', (payload) => withRoot((root) => {
    writeJournal(root, payload);
    expect(() => requireCompletedMigration(root)).toThrow('--resume or --rollback');
    expect(fs.readFileSync(path.join(root, '.mrms-migration/journal.json'), 'utf8')).toBe(payload);
  }));

  test('API startup refuses unfinished migration before loading the catalog', () => withRoot((root) => {
    fs.cpSync(path.resolve('config'), root, { recursive: true });
    writeJournal(root, JSON.stringify({ schema_version: 1, status: 'applying' }));
    expect(() => createConfig({ env: { EDGEWARN_CONFIG_DIR: root }, argv: [] })).toThrow('--resume or --rollback');
  }));

  test('refuses escaping and dangling symlinks', () => withRoot((root) => withRoot((outside) => {
    fs.symlinkSync(outside, path.join(root, '.mrms-migration'), 'dir');
    expect(() => requireCompletedMigration(root)).toThrow('--resume or --rollback');
    fs.writeFileSync(path.join(outside, 'journal.json'), '{"schema_version":1,"status":"complete"}');
    expect(() => requireCompletedMigration(root)).toThrow('--resume or --rollback');
  })));
});
