import { afterEach, describe, expect, it, jest } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import { PassThrough, Readable } from 'stream';
import { ArtifactRepository } from '../../src/api/repositories/artifactRepository.js';
import { createAnalysisService } from '../../src/api/services/analysis.js';
import { createAncillaryServices } from '../../src/api/services/ancillary.js';
import { streamArtifact } from '../../src/api/streamArtifact.js';

// api.yaml is the only base default for the repository budgets, so the
// constructor no longer fills them in and every caller states them.
const REPOSITORY_LIMITS = { json: 8 * 1024 * 1024, binary: 128 * 1024 * 1024, image: 32 * 1024 * 1024 };
const REPOSITORY_CACHE = { max_entries: 256, max_size_bytes: 32 * 1024 * 1024 };
const REPOSITORY_LIST_LIMIT = 1000;

const RENDER_DEFAULTS = {
  grid: { rows: 5, cols: 10, tile_size: 700 },
  grid_maxima: { rows: 100, cols: 100, tile_size: 4096 },
};

// Deliberately one product short of api.yaml's `validation.radar_products`. A
// service that still carried its own allowlist would accept VRADH regardless of
// what it was handed, so the narrow fixture is what proves the catalog is read.
const ANCILLARY_CONFIG = {
  validation: { radar_products: ['DBZH'] },
  wpc: { surface_filename_prefix: 'wpc_sfc_', surface_filename_suffix: '.geojson' },
};

describe('unified API services', () => {
  let root;
  afterEach(async () => { if (root) await fs.rm(root, { recursive: true, force: true }); });

  it('keeps legacy analysis artifacts behind one service boundary', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'api-services-'));
    await fs.mkdir(path.join(root, 'data', 'cells'), { recursive: true });
    await fs.writeFile(path.join(root, 'data', 'cells', 'cell_index.json'), '{"cellIds":["7"]}');
    await fs.writeFile(path.join(root, 'data', 'cells', '7.json'), '{"id":7}');
    const service = createAnalysisService(new ArtifactRepository({ data: path.join(root, 'data') }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT));
    await expect(service.listCells()).resolves.toEqual(['7']);
    await expect(service.getCell('7')).resolves.toEqual({ id: 7 });
    await expect(service.getCell('../7')).rejects.toMatchObject({ code: 'INVALID_PATH' });
  });

  it('lists storm snapshots newest first, degrading to empty when the index is absent', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'api-services-'));
    const service = createAnalysisService(new ArtifactRepository({ data: path.join(root, 'data') }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT));
    // Missing index degrades to an empty collection rather than a NOT_FOUND error.
    const empty = await service.listStormSnapshots();
    expect(empty).toEqual([]);
    // Descending by the YYYYMMDD-HHMMSS key (the index builds ascending); 'reverse' is derived.
    await fs.mkdir(path.join(root, 'data', 'stormcells'), { recursive: true });
    await fs.writeFile(path.join(root, 'data', 'stormcells', 'stormcell_index.json'),
      JSON.stringify({ timestamps: ['20260317-200000', '20260318-000000', '20260319-120000'], lastUpdated: '2026-03-19T00:00:00Z' }));
    const listed = await service.listStormSnapshots();
    expect(listed).toEqual(['20260319-120000', '20260318-000000', '20260317-200000']);
  });

  

  

  

  it('closes an artifact handle when a disconnected stream fails', async () => {
    const close = jest.fn().mockResolvedValue();
    const source = new Readable({
      read() { this.destroy(new Error('client disconnected')); },
    });
    const response = new PassThrough();
    response.set = jest.fn(() => response);
    response.type = jest.fn(() => response);
    response.destroyed = true;
    const request = { destroyed: true, fresh: false, method: 'GET' };

    await expect(streamArtifact(request, response, {
      handle: { createReadStream: () => source, close },
      headers: {},
      size: 4,
    }, 'application/octet-stream')).resolves.toBeUndefined();
    expect(close).toHaveBeenCalledTimes(1);
  });

  // KDP is a real NEXRAD moment absent from every copy of the allowlist; VRADH is
  // present in api.yaml but not in ANCILLARY_CONFIG, so it is the leg that fails if
  // the service stops consulting what it was handed. The DBZH leg is the control:
  // without it this would pass for any reason radarField rejects, a malformed
  // elevation or timestamp included.
  it('refuses a radar moment outside the injected allowlist before any file lookup', async () => {
    const opened = { handle: { close: jest.fn() }, size: 1 };
    const repository = { open: jest.fn().mockResolvedValue(opened) };
    const service = createAncillaryServices(repository, ANCILLARY_CONFIG);
    await expect(service.radarField('KTLH', '20260317-200000', '0.5', 'KDP')).rejects.toMatchObject({ code: 'INVALID_PATH' });
    await expect(service.radarField('KTLH', '20260317-200000', '0.5', 'VRADH')).rejects.toMatchObject({ code: 'INVALID_PATH' });
    expect(repository.open).not.toHaveBeenCalled();
    await expect(service.radarField('KTLH', '20260317-200000', '0.5', 'DBZH')).resolves.toBe(opened);
  });

  
});


it('rejects pruned pagination cursors instead of restarting a collection', async () => {
  const { page } = await import('../../src/api/services/validation.js');
  expect(() => page(['2', '3'], { cursor: '1' }, { default_limit: 1, max_limit: 10 })).toThrow(/Cursor no longer exists/);
});
