import { afterAll, beforeAll, expect, it } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import request from 'supertest';
import { createApp } from '../../src/api/app.js';

let baseDir;
let app;
beforeAll(async () => {
  baseDir = await fs.mkdtemp(path.join(os.tmpdir(), 'retired-products-'));
  // Legacy files cannot restore retired routes or influence service discovery.
  for (const relative of ['gui/RAP/CAPE/index.json', 'gui/MRMS_QPE/index.json',
    'wpc/surface_analysis/latest.geojson', 'state/realtime/services/retired.json']) {
    const target = path.join(baseDir, relative);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, '{}');
  }
  ({ app } = await createApp({ argv: [], env: { EDGEWARN_BASE_DIR: baseDir,
    RATE_LIMIT_MAX_SEC: '0', RATE_LIMIT_MAX_MIN: '0' } }));
});
afterAll(async () => { await fs.rm(baseDir, { recursive: true, force: true }); });
it.each(['/api/v3/render-products', '/api/v3/render-products/MRMS_QPE',
  '/api/v3/render-products/MRMS_QPE/snapshots/20260317-200000/chunks/0/0',
  '/api/v3/models/rap/layers', '/api/v3/models/rap/layers/CAPE/snapshots/20260317-200000/data',
  '/api/v3/analyses/wpc/surface', '/api/v3/observations/metar'])('%s is retired', async (url) => {
  await request(app).get(url).expect(404).expect('Content-Type', /application\/problem\+json/);
  await request(app).post(url).expect(404);
});
it('reports only the deployed service names', async () => {
  const response = await request(app).get('/health/ready').expect(503);
  expect(Object.keys(response.body.services).sort()).toEqual(['edgewarn', 'ingest', 'nexrad']);
});
