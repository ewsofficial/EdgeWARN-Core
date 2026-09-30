import { lstatSync, readFileSync, realpathSync } from 'fs';
import path from 'path';

// Runtime startup uses this gate; offline catalog validation remains available
// while an operator resumes or rolls back a migration.
export function requireCompletedMigration(configDir) {
  const root = realpathSync(configDir);
  const journal = path.join(root, '.mrms-migration', 'journal.json');
  const message = `MRMS migration at ${journal} is incomplete or invalid; keep services stopped and use migrate-mrms --resume or --rollback before startup`;
  try {
    try { lstatSync(journal); } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      try {
        if (lstatSync(path.dirname(journal)).isSymbolicLink()) throw new Error(message);
      } catch (parentError) {
        if (parentError.code !== 'ENOENT') throw parentError;
      }
      return;
    }
    const relative = path.relative(root, realpathSync(journal));
    if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error(message);
    const payload = JSON.parse(readFileSync(journal, 'utf8'));
    if (!payload || Array.isArray(payload) || payload.schema_version !== 1 || !['complete', 'rolled-back'].includes(payload.status)) throw new Error(message);
  } catch (error) {
    throw new Error(message, { cause: error });
  }
}
