import { describe, expect, it } from '@jest/globals';
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import { productCatalog, productById } from '../../src/api/config/productCatalog.js';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

describe('unified API contract specification', () => {
  it('defines every v3 capability in the migration plan', async () => {
    const openApi = JSON.parse(await fs.readFile(path.join(projectRoot, 'src/api/openapi/v3.yaml'), 'utf8'));
    expect(openApi.openapi).toBe('3.1.0');
    expect(Object.keys(openApi.paths)).toEqual(expect.arrayContaining([
      '/api/v3', '/api/v3/openapi.json', '/health/live', '/health/ready',
      '/api/v3/cells', '/api/v3/cells/{cellId}',
      '/api/v3/storm-snapshots', '/api/v3/storm-snapshots/{timestamp}',
      '/api/v3/alert-snapshots', '/api/v3/alerts/{alertId}',
      '/api/v3/observations/metar/{timestamp}',
      '/api/v3/render-products/{productId}/snapshots/{timestamp}/chunks/{x}/{y}',
      '/api/v3/radar-sites/{siteId}/scans/{timestamp}/elevations/{elevation}/products/{productId}',
      '/api/v3/models/rap/layers/{layerId}/snapshots/{timestamp}/data',
      '/api/v3/analyses/wpc/surface/{timestamp}'
    ]));
    for (const [route, item] of Object.entries(openApi.paths)) {
      for (const operation of Object.values(item)) {
        const declared = (operation.parameters || []).map((parameter) => parameter.$ref ? openApi.components.parameters[parameter.$ref.split('/').at(-1)] : parameter);
        for (const match of route.matchAll(/\{([^}]+)\}/g)) {
          expect(declared.some((parameter) => parameter?.name === match[1] && parameter.in === 'path' && parameter.required)).toBe(true);
        }
      }
    }
  });

  it('has a collision-free canonical product catalog', () => {
    expect(productCatalog).toHaveLength(32);
    expect(productById.size).toBe(productCatalog.length);
    expect(productById.get('MRMS_MergedReflectivityQC').storageDirectory).toBe('MRMS_MergedReflectivityQC');
    expect(productById.get('GOES_ABI_C13_BrightnessTemp').legacyFilePrefix).toBe('GOES_ABI_C13_BrightnessTemp');
    expect(productById.get('goes-rgb-true-color')).toBeUndefined();
    expect(productCatalog.every((product) => product.representation === 'binary_chunks')).toBe(true);
  });

  it('does not advertise removed PNG render resources in v3', async () => {
    const openApi = JSON.parse(await fs.readFile(path.join(projectRoot, 'src/api/openapi/v3.yaml'), 'utf8'));
    const routes = Object.keys(openApi.paths);
    expect(routes.some((route) => route.endsWith('/image'))).toBe(false);
    expect(routes.some((route) => route.includes('/tiles'))).toBe(false);
  });
});
