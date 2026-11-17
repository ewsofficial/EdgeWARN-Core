import { afterAll, beforeAll, describe, expect, it } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import zlib from 'zlib';
import request from 'supertest';
import { createApp } from '../../src/api/app.js';

// Phase 9: the Python production encoders emit the committed inventory under
// tests/fixtures/serialization/ (regenerate with
// `PYTHONPATH=src python tests/fixtures/serialization/generate_fixtures.py`).
// These cases serve those exact artifacts through the real Express stack and
// independently validate headers plus raw-byte passthrough -- they never share
// any decoding implementation with the Python suite.

const FIXTURES = new URL('../fixtures/serialization/', import.meta.url);
const TIMESTAMP = '20260317-200000';

// [fixture-relative source, runtime-tree destination]
const COPIES = [
  ['nexrad/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz', 'gui/NEXRAD/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz'],
];

const fixtureBytes = (relative) => fs.readFile(new URL(relative, FIXTURES));
const fixtureJson = async (relative) => JSON.parse(await fs.readFile(new URL(relative, FIXTURES), 'utf8'));

// IEEE-754 binary16 -> float64. Buffer has no half-precision read, so the
// independent struct decode converts each half word explicitly instead of
// misreading the payload with float32 reads.
const halfToFloat = (bits) => {
  const sign = bits & 0x8000 ? -1 : 1;
  const exponent = (bits >> 10) & 0x1f;
  const fraction = bits & 0x3ff;
  if (exponent === 0) return sign * (fraction / 1024) * 2 ** -14;
  if (exponent === 0x1f) return fraction === 0 ? sign * Infinity : NaN;
  return sign * (1 + fraction / 1024) * 2 ** (exponent - 15);
};

describe('binary producer/consumer contracts through the real API', () => {
  let app;
  let baseDir;

  beforeAll(async () => {
    baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'binary-contracts-'));
    for (const [source, destination] of COPIES) {
      const target = path.join(baseDir, destination);
      await fs.mkdir(path.dirname(target), { recursive: true });
      await fs.copyFile(new URL(source, FIXTURES), target);
    }
    // v3 owner gates require an active heartbeat per service (decomposition
    // phases 3-5); without them the nexrad routes return 503.
    const heartbeat = (service) => JSON.stringify({ schema_version: 1, service, pid: 1, run_id: 'test-run', updated_at: new Date().toISOString(), phase: 'supervising', degraded_children: [] });
    await fs.mkdir(path.join(baseDir, 'state', 'realtime', 'services'), { recursive: true });
    await fs.writeFile(path.join(baseDir, 'state', 'realtime', 'services', 'nexrad.json'), heartbeat('nexrad'));
    ({ app } = await createApp({ env: { EDGEWARN_BASE_DIR: baseDir, RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' }, argv: [] }));
  });

  afterAll(async () => {
    if (baseDir) await fs.rm(baseDir, { recursive: true, force: true });
  });

  

  

  

  it('serves the NEXRAD variable bin gzip bytes and decodes its documented layout', async () => {
    const reference = await fixtureBytes('nexrad/KTLH/0.5/KTLH_DBZH_0.5_20260317-200000.bin.gz');
    const url = `/api/v3/radar-sites/KTLH/scans/${TIMESTAMP}/elevations/0.5/products/DBZH`;
    const response = await request(app).get(url).expect(200).expect('Content-Type', /application\/gzip/);
    expect(Buffer.compare(Buffer.from(response.body), reference)).toBe(0);

    // Independent struct decode: magic, counts, float16 grid, float32 axes.
    const stream = zlib.gunzipSync(reference);
    expect(stream.subarray(0, 8).toString('latin1')).toBe('EWFFv1S0');
    let offset = 8;
    const azimuthLines = stream.readUInt32LE(offset); offset += 4;
    const rangeGates = stream.readUInt32LE(offset); offset += 4;
    expect([azimuthLines, rangeGates]).toEqual([2, 3]);
    const values = [];
    for (let i = 0; i < azimuthLines * rangeGates; i += 1) values.push(halfToFloat(stream.readUInt16LE(offset + i * 2))); offset += azimuthLines * rangeGates * 2;
    expect(values.slice(0, 5)).toEqual([1, 2, 3, 4, 5]);
    expect(Number.isNaN(values[5])).toBe(true);
    const azimuths = [];
    for (let i = 0; i < azimuthLines; i += 1) azimuths.push(stream.readFloatLE(offset + i * 4)); offset += azimuthLines * 4;
    const ranges = [];
    for (let i = 0; i < rangeGates; i += 1) ranges.push(stream.readFloatLE(offset + i * 4));
    expect(azimuths).toEqual([0.5, 1.5]);
    expect(ranges).toEqual([125, 250, 375]);

  });

  
});
