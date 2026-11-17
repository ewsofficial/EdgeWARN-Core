import { ArtifactError } from '../repositories/artifactRepository.js';
const TIMESTAMP = /^\d{8}-\d{6}$/;
const CELL_ID = /^[1-9][0-9]*$/;
const ALERT_ID = /^[A-Za-z0-9_.:-]{1,200}$/;
const LAYER_ID = /^[A-Za-z0-9_.-]{1,128}$/;

export function timestamp(value) {
  if (typeof value !== 'string' || !TIMESTAMP.test(value)) return null;
  const year = Number(value.slice(0, 4)); const month = Number(value.slice(4, 6)); const day = Number(value.slice(6, 8));
  const hour = Number(value.slice(9, 11)); const minute = Number(value.slice(11, 13)); const second = Number(value.slice(13, 15));
  const date = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day && date.getUTCHours() === hour && date.getUTCMinutes() === minute && date.getUTCSeconds() === second ? date.toISOString() : null;
}

export const isCellId = (value) => typeof value === 'string' && CELL_ID.test(value);
export const isAlertId = (value) => typeof value === 'string' && ALERT_ID.test(value) && !['__proto__', 'constructor', 'prototype'].includes(value);
export const isLayerId = (value) => typeof value === 'string' && LAYER_ID.test(value) && !value.includes('..');
function serializedId(item) { const value = item && typeof item === 'object' && 'id' in item ? item.id : item; return String(value); }
export function page(items, { cursor, limit } = {}, { default_limit: defaultLimit, max_limit: maxLimit }) {
  const safeLimit = Number.isInteger(limit) && limit > 0 ? Math.min(limit, maxLimit) : defaultLimit;
  const cursorIndex = cursor ? items.findIndex((item) => serializedId(item) === String(cursor)) : -1;
  if (cursor && cursorIndex < 0) throw new ArtifactError('INVALID_PATH', 'Cursor no longer exists; restart pagination');
  const start = cursor ? cursorIndex + 1 : 0;
  const data = items.slice(start, start + safeLimit);
  return { data, nextCursor: start + safeLimit < items.length ? serializedId(data.at(-1)) : null };
}
