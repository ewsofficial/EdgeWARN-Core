import { ArtifactError } from '../repositories/artifactRepository.js';
import { productById, productCatalog } from '../config/productCatalog.js';
import { timestamp } from './validation.js';

const grid = (value, maxima) => value && Number.isInteger(value.rows) && Number.isInteger(value.cols) && Number.isInteger(value.tile_size)
  && value.rows > 0 && value.rows <= maxima.rows && value.cols > 0 && value.cols <= maxima.cols && value.tile_size > 0 && value.tile_size <= maxima.tile_size
  ? { rows: value.rows, cols: value.cols, tileSize: value.tile_size } : null;
const chunkFormat = (value) => value && value.version === 2 && value.encoding === 'float16' && value.file_suffix === '.f16.gz'
  && value.compression === 'gzip' && [1, 3].includes(value.channels) && ['scalar', 'rgb'].includes(value.value_kind)
  && value.bytes_per_component === 2 && value.no_data === 'nan' && value.pixel_row_order === 'top_to_bottom' && value.grid_origin === 'bottom_left'
  ? value : null;

function chunkIndex(value, maxima) {
  if (!value || value.schema_version !== 2 || value.representation !== 'binary_chunks') throw new ArtifactError('INVALID_ARTIFACT', 'Unsupported render chunk index');
  const format = chunkFormat(value.chunk_format); const chunkGrid = grid(value.tile_grid, maxima);
  if (!format || !chunkGrid || !Array.isArray(value.chunks) || value.chunks.length > chunkGrid.rows * chunkGrid.cols) throw new ArtifactError('INVALID_ARTIFACT', 'Malformed render chunk index');
  const seen = new Set();
  const chunks = value.chunks.map((chunk) => {
    if (!Array.isArray(chunk) || chunk.length !== 2 || !chunk.every(Number.isInteger) || chunk[0] < 0 || chunk[0] >= chunkGrid.cols || chunk[1] < 0 || chunk[1] >= chunkGrid.rows) throw new ArtifactError('INVALID_ARTIFACT', 'Malformed render chunk coordinates');
    const key = `${chunk[0]},${chunk[1]}`;
    if (seen.has(key)) throw new ArtifactError('INVALID_ARTIFACT', 'Duplicate render chunk coordinates');
    seen.add(key); return chunk;
  });
  return { format, grid: chunkGrid, chunks };
}

export function createRenderService(repository, renderDefaults, chunkLengthSlackBytes) {
  const defaultGrid = Object.freeze({ rows: renderDefaults.grid.rows, cols: renderDefaults.grid.cols, tileSize: renderDefaults.grid.tile_size });
  const maxima = renderDefaults.grid_maxima;
  const product = (id) => {
    const found = productById.get(id);
    if (!found) throw new ArtifactError('NOT_FOUND', 'Render product not found');
    return found;
  };
  const productIndex = async (item) => {
    try { return await repository.readJson('gui', [item.storageDirectory, 'index.json']); } catch (error) { if (error.code === 'NOT_FOUND') return []; throw error; }
  };
  return {
    async listProducts() {
      const result = [];
      for (const item of productCatalog) {
        try { await repository.list('gui', [item.storageDirectory], { limit: 1 }); result.push(item); } catch (error) { if (error.code !== 'NOT_FOUND') throw error; }
      }
      return result;
    },
    async getProduct(id) {
      const item = product(id); const index = await productIndex(item);
      if (Array.isArray(index) || index.schema_version !== 2 || index.representation !== 'binary_chunks') return { ...item, grid: grid(Array.isArray(index) ? null : index.tile_grid, maxima) || defaultGrid };
      const format = chunkFormat(index.chunk_format);
      if (!format) throw new ArtifactError('INVALID_ARTIFACT', 'Unsupported render chunk format');
      return { ...item, representation: index.representation, chunkFormat: index.chunk_format, grid: grid(index.tile_grid, maxima) || defaultGrid };
    },
    async listSnapshots(id) { const index = await productIndex(product(id)); return Array.isArray(index) ? index : (Array.isArray(index.timestamps) ? index.timestamps : []); },
    async chunks(id, value) {
      if (!timestamp(value)) throw new ArtifactError('INVALID_PATH', 'Invalid timestamp');
      const item = product(id); const data = chunkIndex(await repository.readJson('gui', [item.storageDirectory, value, 'index.json']), maxima);
      const productData = await productIndex(item);
      if (!productData || productData.schema_version !== 2 || productData.representation !== 'binary_chunks') throw new ArtifactError('INVALID_ARTIFACT', 'Unsupported render product index');
      const productFormat = chunkFormat(productData.chunk_format);
      if (!productFormat || productData.chunk_format.version !== data.format.version || productData.chunk_format.file_suffix !== data.format.file_suffix) throw new ArtifactError('INVALID_ARTIFACT', 'Conflicting render chunk metadata');
      return data;
    },
    async chunk(id, value, x, y) {
      const data = await this.chunks(id, value);
      if (!Number.isInteger(x) || !Number.isInteger(y) || !data.chunks.some(([chunkX, chunkY]) => chunkX === x && chunkY === y)) throw new ArtifactError('NOT_FOUND', 'Render chunk not found');
      const opened = await repository.open('gui', [product(id).storageDirectory, value, 'chunks', `chunk_${x}_${y}.f16.gz`], { kind: 'binary' });
      const expectedLength = data.grid.tileSize * data.grid.tileSize * data.format.channels * data.format.bytes_per_component;
      if (!opened.size || opened.size > expectedLength + chunkLengthSlackBytes) { await opened.handle.close(); throw new ArtifactError('INVALID_ARTIFACT', 'Render chunk has an invalid length'); }
      return { ...opened, chunk: data };
    }
  };
}
