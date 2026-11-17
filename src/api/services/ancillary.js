import { ArtifactError } from '../repositories/artifactRepository.js';
import { timestamp } from './validation.js';

const RADAR_SITE = /^[A-Z0-9]{4}$/;
const ELEVATION = /^\d{1,3}(?:\.\d{1,2})?$/;
const noRoot = async (fn, fallback = []) => { try { return await fn(); } catch (error) { if (error.code === 'NOT_FOUND') return fallback; throw error; } };

function radarFile(site, product, elevation, value) { return `${site}_${product}_${elevation}_${value}.bin.gz`; }

export function createAncillaryServices(repository, config) {
  const radarProducts = new Set(config.validation.radar_products);
  const validRadar = (site, elevation, value, product) => RADAR_SITE.test(site) && ELEVATION.test(elevation) && timestamp(value) && radarProducts.has(product);
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
  };
}
