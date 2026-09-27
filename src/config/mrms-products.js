import fs from 'fs';

export function parseProductId(value) {
  if (value === 'MRMS_ProbSevere') return { productId: 'ProbSevere', pathName: value };
  const match = typeof value === 'string' && /^MRMS_([A-Za-z0-9][A-Za-z0-9_-]*)_([0-9]{2}\.[0-9]{2})$/.exec(value);
  if (!match || match[0] !== value || match[1].startsWith('MRMS_') || value.length > 133) {
    throw new Error(`Invalid MRMS product ${JSON.stringify(value)}: expected one MRMS_ prefix and _DD.DD elevation (or MRMS_ProbSevere); normalized identity must fit 128 characters`);
  }
  return { productId: value.slice(5), pathName: `MRMS_${match[1]}` };
}

export function normalizeProducts(values, protectedIds = []) {
  if (!Array.isArray(values)) throw new Error('MRMS products must be an array');
  values.forEach(parseProductId);
  if (new Set(values).size !== values.length) throw new Error('Duplicate MRMS product');
  const paths = new Map();
  return [...new Set([...protectedIds, ...values])].map((value) => {
    const identity = parseProductId(value);
    const key = identity.pathName.toLowerCase();
    if (paths.has(key)) throw new Error(`MRMS path collision: ${paths.get(key)} and ${value}`);
    paths.set(key, value);
    return identity;
  });
}

export const MIGRATION_HINT = 'Run edgewarn migrate-mrms --config-path <config> --base-dir <runtime> for an offline migration report.';

export function validateMrmsDocument(name, document) {
  if (name === 'ingest' && document.schema_version === 2) {
    const asset = JSON.parse(fs.readFileSync(new URL('../common/ingest/mrms/core-contract.json', import.meta.url), 'utf8'));
    normalizeProducts(document.mrms.products, asset.products.map((p) => p.configured_id));
  }
  const key = { integration: 'stats_datasets', ewmrs_render: 'mrms_layers' }[name];
  if (key) document[key].forEach((item, index) => {
    if (('product' in item) === ('filepath' in item)) throw new Error(`${key}[${index}] requires exactly one of product or legacy filepath`);
    if ('product' in item) parseProductId(`MRMS_${item.product}`);
  });
}
