import catalog from './product-catalog.json' with { type: 'json' };

// v3 render id == render layer name == legacyFilePrefix (e.g. MRMS_MergedReflectivityQC).
// Charset is frozen for 3.1.0 dynamic ingest/render products: 3.1.0 adds catalog
// entries only, no route or validation change. Matches the layerId charset.
const PRODUCT_ID = /^[A-Za-z0-9_.-]+$/;

function assertCatalog(entries) {
  const ids = new Set();
  const directories = new Set();
  for (const entry of entries) {
    if (!entry || !PRODUCT_ID.test(entry.id) || !entry.storageDirectory || !entry.legacyFilePrefix) {
      throw new Error('Invalid render product catalog entry');
    }
    for (const [set, value, field] of [[ids, entry.id, 'id'], [directories, entry.storageDirectory, 'storageDirectory']]) {
      if (set.has(value)) throw new Error(`Duplicate render catalog ${field}: ${value}`);
      set.add(value);
    }
  }
  return Object.freeze(entries.map((entry) => Object.freeze({ ...entry })));
}

export const productCatalog = assertCatalog(catalog);
export const productById = new Map(productCatalog.map((product) => [product.id, product]));
export const productByLegacyId = new Map(productCatalog.map((product) => [product.storageDirectory, product]));

export function getProductByLegacyId(legacyId) {
  return productByLegacyId.get(legacyId) || null;
}
