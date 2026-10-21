import { ArtifactError } from '../repositories/artifactRepository.js';
import { isAlertId, isCellId, timestamp } from './validation.js';

const ALERT_SOURCES = new Set(['official', 'edgewarn']);
const alertSegments = (source, type, name) => [
  'Alerts', source === 'edgewarn' ? 'EdgeWARN' : 'official', type,
  ...(name ? [name] : [])
];

export function createAnalysisService(repository) {
  return {
    async listCells() {
      try { return (await repository.readJson('data', ['cells', 'cell_index.json'])).cellIds || []; } catch (error) { if (error.code === 'NOT_FOUND') return []; throw error; }
    },
    async getCell(cellId) {
      if (!isCellId(cellId)) throw new ArtifactError('INVALID_PATH', 'Invalid cell ID');
      return repository.readJson('data', ['cells', `${cellId}.json`]);
    },
    async listStormSnapshots() {
      try {
        const fromIndex = (await repository.readJson('data', ['stormcells', 'stormcell_index.json'])).timestamps || [];
        // Descending by the YYYYMMDD-HHMMSS index key, matching alert-snapshots ordering
        // (the index builds it ascending). slice copies so the index stays the source of truth.
        return fromIndex.slice().sort().reverse();
      } catch (error) {
        if (error.code === 'NOT_FOUND') return []; throw error;
      }
    },
    async getStormSnapshot(value) {
      if (!timestamp(value)) throw new ArtifactError('INVALID_PATH', 'Invalid timestamp');
      return repository.readJson('data', ['stormcells', `stormcells_${value}.json`]);
    },
    async listAlertSnapshots(source) {
      if (!ALERT_SOURCES.has(source)) throw new ArtifactError('INVALID_PATH', 'Invalid alert source');
      try {
        const entries = await repository.list('data', alertSegments(source, 'timestamps'));
        return entries.map((entry) => entry.name.match(/^(\d{8}-\d{6})\.json$/)?.[1]).filter(Boolean).sort().reverse();
      } catch (error) { if (error.code === 'NOT_FOUND') return []; throw error; }
    },
    async getAlertSnapshot(source, value) {
      if (!ALERT_SOURCES.has(source) || !timestamp(value)) throw new ArtifactError('INVALID_PATH', 'Invalid alert snapshot');
      try { const result = await repository.readJson('data', alertSegments(source, 'timestamps', `${value}.json`)); return Array.isArray(result.alerts) ? result.alerts : []; } catch (error) { if (error.code === 'NOT_FOUND') return []; throw error; }
    },
    async getAlert(source, alertId) {
      if (!ALERT_SOURCES.has(source) || !isAlertId(alertId)) throw new ArtifactError('INVALID_PATH', 'Invalid alert');
      const result = await repository.readJson('data', alertSegments(source, 'ids', `${alertId.replaceAll(':', '_')}.json`));
      return result.feature || result;
    },
  };
}
