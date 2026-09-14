import { ArtifactError } from '../repositories/artifactRepository.js';
import { isLayerId, timestamp } from './validation.js';

const RADAR_SITE = /^[A-Z0-9]{4}$/;
const ELEVATION = /^\d{1,3}(?:\.\d{1,2})?$/;
const noRoot = async (fn, fallback = []) => { try { return await fn(); } catch (error) { if (error.code === 'NOT_FOUND') return fallback; throw error; } };
const escapeLiteral = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function radarFile(site, product, elevation, value) { return `${site}_${product}_${elevation}_${value}.bin.gz`; }

// `config` carries api.yaml's `validation` block and wpc.yaml's `wpc` block. Read
// here and not at module scope: createConfig resolves the config directory from
// argv and env, which happens after every import has already been evaluated.
export function createAncillaryServices(repository, config) {
  const radarProducts = new Set(config.validation.radar_products);
  const validRadar = (site, elevation, value, product) => RADAR_SITE.test(site) && ELEVATION.test(elevation) && timestamp(value) && radarProducts.has(product);
  const wpcSurfaceFile = (value) => `${config.wpc.surface_filename_prefix}${value}${config.wpc.surface_filename_suffix}`;
  const wpcSurfaceName = new RegExp(`^${escapeLiteral(config.wpc.surface_filename_prefix)}(\\d{8}-\\d{6})${escapeLiteral(config.wpc.surface_filename_suffix)}$`);
  const radarAvailability = async (site) => {
    if (!RADAR_SITE.test(site)) throw new ArtifactError('INVALID_PATH', 'Invalid radar site');
    const elevations = await repository.list('gui', ['NEXRAD', site]);
    const output = {};
    for (const elevationEntry of elevations.filter((entry) => entry.isDirectory() && ELEVATION.test(entry.name))) {
      const files = await repository.list('gui', ['NEXRAD', site, elevationEntry.name]);
      const scans = new Map();
      for (const file of files.filter((entry) => entry.isFile())) {
        const match = file.name.match(new RegExp(`^${site}_([A-Z0-9]+)_${elevationEntry.name.replace('.', '\\.')}_(\\d{8}-\\d{6})\\.bin\\.gz$`));
        if (match && radarProducts.has(match[1]) && timestamp(match[2])) {
          const products = scans.get(match[2]) || []; products.push(match[1]); scans.set(match[2], products);
        }
      }
      if (scans.size) output[elevationEntry.name] = [...scans.entries()].sort(([a], [b]) => b.localeCompare(a)).map(([validTime, products]) => ({ timestamp: validTime, validTime: timestamp(validTime), products: products.sort() }));
    }
    return output;
  };
  return {
    async listRadarSites() {
      const entries = await noRoot(() => repository.list('gui', ['NEXRAD'])); const sites = [];
      for (const entry of entries.filter((entry) => entry.isDirectory() && RADAR_SITE.test(entry.name))) {
        if (Object.keys(await radarAvailability(entry.name)).length) sites.push(entry.name);
      }
      return sites.sort();
    },
    radarAvailability,
    async radarField(site, value, elevation, product) {
      const normalized = String(site).toUpperCase();
      if (!validRadar(normalized, elevation, value, product)) throw new ArtifactError('INVALID_PATH', 'Invalid radar field');
      return repository.open('gui', ['NEXRAD', normalized, elevation, radarFile(normalized, product, elevation, value)]);
    },
    async listRapLayers() {
      const entries = await noRoot(() => repository.list('gui', ['RAP'])); const layers = [];
      for (const entry of entries.filter((entry) => entry.isDirectory() && isLayerId(entry.name))) {
        try { await repository.readJson('gui', ['RAP', entry.name, 'index.json']); layers.push(entry.name); } catch (error) { if (error.code !== 'NOT_FOUND') throw error; }
      }
      return layers.sort();
    },
    async rapSnapshots(layer) {
      if (!isLayerId(layer)) throw new ArtifactError('INVALID_PATH', 'Invalid RAP layer');
      const index = await noRoot(() => repository.readJson('gui', ['RAP', layer, 'index.json']));
      return Array.isArray(index) ? index : (Array.isArray(index.timestamps) ? index.timestamps : []);
    },
    async rapMetadata(layer, value) { if (!isLayerId(layer) || !timestamp(value)) throw new ArtifactError('INVALID_PATH', 'Invalid RAP resource'); return repository.readJson('gui', ['RAP', layer, value, 'metadata.json']); },
    async rapData(layer, value) {
      if (!isLayerId(layer) || !timestamp(value)) throw new ArtifactError('INVALID_PATH', 'Invalid RAP resource');
      const opened = await repository.open('gui', ['RAP', layer, value, 'data.u16']);
      let metadata = null;
      try {
        metadata = await repository.readJson('gui', ['RAP', layer, value, 'metadata.json']);
      } catch (error) {
        if (error.code !== 'NOT_FOUND') {
          await opened.handle.close();
          throw error;
        }
      }
      const number = (candidate) => Number.isFinite(candidate) && Math.abs(candidate) <= 1e12 ? String(candidate) : null;
      const units = typeof metadata?.units === 'string' && /^[\x20-\x7e]{1,80}$/.test(metadata.units) ? metadata.units : null;
      const grid = metadata?.grid || {};
      opened.headers = Object.fromEntries(Object.entries({
        'X-Data-Type': 'uint16', 'X-Byte-Order': 'little_endian', 'X-Missing-Value': '65535',
        'X-Grid-Ni': number(grid.ni ?? metadata?.shape?.[1]), 'X-Grid-Nj': number(grid.nj ?? metadata?.shape?.[0]),
        'X-Scale-Min': number(metadata?.scale?.min), 'X-Scale-Max': number(metadata?.scale?.max), 'X-Units': units
      }).filter(([, header]) => header !== null));
      return opened;
    },
    async listWpcSurface() {
      const entries = await noRoot(() => repository.list('wpc', ['surface_analysis']));
      return entries.map((entry) => entry.name.match(wpcSurfaceName)?.[1]).filter((value) => value && timestamp(value)).sort().reverse();
    },
    async wpcSurface(value) { if (!timestamp(value)) throw new ArtifactError('INVALID_PATH', 'Invalid timestamp'); return repository.readJson('wpc', ['surface_analysis', wpcSurfaceFile(value)]); },
  };
}
