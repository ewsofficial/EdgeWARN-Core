"""Semantic catalog rules, shared by disk loading and in-memory editing."""
import json
from pathlib import Path

from common.config.mrms_products import normalize_products, parse_product_id

MIGRATION_HINT = 'Run edgewarn migrate-mrms --config-path <config> --base-dir <runtime> for an offline migration report.'


def validate_mrms_document(name, document):
    if name == 'ingest' and document.get('schema_version') == 2:
        # Release-owned asset, never read from the operator configuration tree.
        asset = Path(__file__).parents[1] / 'ingest/mrms/core-contract.json'
        protected = [p['configured_id'] for p in json.loads(asset.read_text())['products']]
        normalize_products(document['mrms']['products'], protected_ids=protected)
    key = {'integration': 'stats_datasets', 'ewmrs_render': 'mrms_layers'}.get(name)
    if key:
        for index, item in enumerate(document[key]):
            if ('product' in item) == ('filepath' in item):
                raise ValueError(f'{key}[{index}] requires exactly one of product or legacy filepath')
            if 'product' in item:
                parse_product_id('MRMS_' + item['product'])
