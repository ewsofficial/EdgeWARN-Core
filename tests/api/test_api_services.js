import { afterEach, describe, expect, it, jest } from '@jest/globals';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';
import { PassThrough, Readable } from 'stream';
import { ArtifactRepository } from '../../src/api/repositories/artifactRepository.js';
import { createAnalysisService } from '../../src/api/services/analysis.js';
import { createRenderService } from '../../src/api/services/renders.js';
import { createAncillaryServices } from '../../src/api/services/ancillary.js';
import { streamArtifact } from '../../src/api/streamArtifact.js';

// api.yaml is the only base default for the repository budgets, so the
// constructor no longer fills them in and every caller states them.
const REPOSITORY_LIMITS = { json: 8 * 1024 * 1024, binary: 128 * 1024 * 1024, image: 32 * 1024 * 1024 };
const REPOSITORY_CACHE = { max_entries: 256, max_size_bytes: 32 * 1024 * 1024 };
const REPOSITORY_LIST_LIMIT = 1000;

const RENDER_DEFAULTS = {
  grid: { rows: 10, cols: 20, tile_size: 350 },
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

  it('uses canonical render IDs while preserving storage prefixes', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'api-services-'));
    const product = path.join(root, 'gui', 'CompRefQC');
    await fs.mkdir(product, { recursive: true });
    await fs.writeFile(path.join(product, 'index.json'), '{"timestamps":["20260317-200000"]}');
    await fs.writeFile(path.join(product, 'MRMS_MergedReflectivityQC_20260317-200000.png'), 'png');
    const service = createRenderService(new ArtifactRepository({ gui: path.join(root, 'gui') }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT), RENDER_DEFAULTS, 1024);
    await expect(service.listSnapshots('comp-ref-qc')).resolves.toEqual(['20260317-200000']);
    const opened = await service.image('comp-ref-qc', '20260317-200000');
    await expect(opened.handle.readFile()).resolves.toEqual(Buffer.from('png'));
    await opened.handle.close();
  });

  it('uses a product tile grid when a timestamp index omits grid metadata', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'api-services-'));
    const product = path.join(root, 'gui', 'CompRefQC');
    await fs.mkdir(path.join(product, '20260317-200000'), { recursive: true });
    await fs.writeFile(path.join(product, 'index.json'), '{"tile_grid":{"rows":2,"cols":3,"tile_size":256}}');
    await fs.writeFile(path.join(product, '20260317-200000', 'index.json'), '{"tiles":[[2,1]]}');
    const service = createRenderService(new ArtifactRepository({ gui: path.join(root, 'gui') }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT), RENDER_DEFAULTS, 1024);
    await expect(service.tiles('comp-ref-qc', '20260317-200000')).resolves.toEqual({ grid: { rows: 2, cols: 3, tileSize: 256 }, tiles: [[2, 1]] });
  });

  it('opens only indexed RGBA chunks with their exact expected length', async () => {
    root = await fs.mkdtemp(path.join(os.tmpdir(), 'api-services-'));
    const product = path.join(root, 'gui', 'CompRefQC'); const timestamp = '20260317-200000';
    await fs.mkdir(path.join(product, timestamp, 'chunks'), { recursive: true });
    const format = { version: 2, encoding: 'float16', file_suffix: '.f16.gz', compression: 'gzip', channels: 1, value_kind: 'scalar', no_data: 'nan', bytes_per_component: 2, pixel_row_order: 'top_to_bottom', grid_origin: 'bottom_left' };
    await fs.writeFile(path.join(product, 'index.json'), JSON.stringify({ schema_version: 2, timestamps: [timestamp], representation: 'binary_chunks', chunk_format: { ...format, media_type: 'application/octet-stream' }, tile_grid: { rows: 1, cols: 1, tile_size: 2 } }));
    await fs.writeFile(path.join(product, timestamp, 'index.json'), JSON.stringify({ schema_version: 2, timestamp, representation: 'binary_chunks', chunk_format: format, tile_grid: { rows: 1, cols: 1, tile_size: 2 }, chunks: [[0, 0]] }));
    await fs.writeFile(path.join(product, timestamp, 'chunks', 'chunk_0_0.f16.gz'), Buffer.from('H4sIAAAAAAAC/2NggAAAad8iZQgAAAA=', 'base64'));
    const service = createRenderService(new ArtifactRepository({ gui: path.join(root, 'gui') }, REPOSITORY_LIMITS, REPOSITORY_CACHE, REPOSITORY_LIST_LIMIT), RENDER_DEFAULTS, 1024);
    await expect(service.chunks('comp-ref-qc', timestamp)).resolves.toMatchObject({ chunks: [[0, 0]], grid: { tileSize: 2 } });
    const opened = await service.chunk('comp-ref-qc', timestamp, 0, 0);
    expect(opened.size).toBe(23); await opened.handle.close();
    await expect(service.chunk('comp-ref-qc', timestamp, 1, 0)).rejects.toMatchObject({ code: 'NOT_FOUND' });
  });

  it('closes a RAP data handle when its metadata is malformed', async () => {
    const close = jest.fn().mockResolvedValue();
    const repository = {
      open: jest.fn().mockResolvedValue({ handle: { close }, size: 1 }),
      readJson: jest.fn().mockRejectedValue(Object.assign(new Error('malformed'), { code: 'IN_PROGRESS' }))
    };
    const service = createAncillaryServices(repository, ANCILLARY_CONFIG);
    await expect(service.rapData('CAPE', '20260317-200000')).rejects.toMatchObject({ code: 'IN_PROGRESS' });
    expect(close).toHaveBeenCalledTimes(1);
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

  it('builds the WPC surface names it lists and reads from wpc.yaml', async () => {
    const repository = {
      list: jest.fn().mockResolvedValue([{ name: 'wpc_sfc_20260317-200000.geojson' }, { name: 'latest.geojson' }]),
      readJson: jest.fn().mockResolvedValue({ type: 'FeatureCollection' })
    };
    const service = createAncillaryServices(repository, ANCILLARY_CONFIG);
    await expect(service.listWpcSurface()).resolves.toEqual(['20260317-200000']);
    await expect(service.wpcSurface('20260317-200000')).resolves.toEqual({ type: 'FeatureCollection' });
    expect(repository.readJson).toHaveBeenCalledWith('wpc', ['surface_analysis', 'wpc_sfc_20260317-200000.geojson']);
  });
});
