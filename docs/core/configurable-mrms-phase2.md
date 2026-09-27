# Configurable MRMS ingestion: phase 2

Phase 2 supplies pure definitions for the coordinated v2 release. The shipped
ingest catalog, live acquisition, filesystem bindings, readiness gates, and
consumer behavior still use v1. Activation belongs to later phases in
[the implementation plan](../../plans/configurable-mrms-ingestion-plan.md).

## Interfaces and ownership

- `common.config.mrms_products.parse_product_id` validates configured IDs.
  It preserves the full upstream elevation in `product_id` and removes only
  the last elevation token from `path_name`. Normalized IDs fit CTAM's
  128-character limit. `normalize_products` merges an explicitly supplied
  protected contract and rejects operator duplicates and case-folded path
  collisions, including collisions with reserved products.
- `common.ingest.mrms.core_contract` owns the three required detection and
  discovery products: composite reflectivity, PrecipFlag, and ProbSevere.
  Frozen definitions include source, phase, discovery, and requiredness.
  Legacy aliases remain supported through 3.x, with removal no earlier than
  4.0 after a deprecation notice.
- `common.ingest.mrms.source` owns bucket, HTTPS endpoints, key/filename
  grammars, and exceptional directory mappings. All current standard HTTPS
  directories derive from parsed base names; no exceptions are needed today.
  ProbSevere has a separate JSON source with a null source modifier and a
  non-null manifest identity. Source methods require aware timestamps and
  normalize them to UTC. The previous-hour ProbSevere listing marker is
  preserved across midnight.
- `common.ingest.mrms.registry.build_registry(mrms_config, base_dir)` takes
  a prevalidated v2 MRMS section (including `products`) and an absolute,
  resolved base path. It does no config loading, network access, path probing,
  symlink resolution, or directory creation. Phase 3 owns full schema and
  semantic validation; phase 4 owns filesystem containment and binding.

Example for development fixtures, independent of the v1 loader:

```python
from pathlib import Path
from common.ingest.mrms.registry import build_registry

registry = build_registry(
    {"products": ("MRMS_MergedAzShear_3-6kmAGL_00.50",)},
    Path("/var/lib/edgewarn"),
)
path = registry.paths_by_name()["MRMS_MergedAzShear_3-6kmAGL"]
assert path == Path("/var/lib/edgewarn/data/MRMS_MergedAzShear_3-6kmAGL")
assert registry.is_enabled("ProbSevere")
```

Lookups accept exact manifest/upstream IDs, not configured IDs or aliases.
`require` and `path_for` fail for disabled products. `paths_by_name` and
`legacy_paths` expose immutable mappings of enabled products only.
Compatibility `get_mrms_modifiers` and `get_check_modifiers` methods derive
triples from registry membership; they do not maintain a second catalog.
They are not yet wired into the live `config.py` accessors.

Registry specs are frozen and pickle-compatible for spawned workers.
The SHA-256 fingerprint covers Core/source contract versions, effective
definitions, paths, and a copied canonical JSON representation of MRMS settings.
Reordering additions or listing a reserved product once does not change it.
Changing membership, operational settings, or the base path does.
Configuration changes require a rebuilt registry and process restart.
Source grammar changes must increment `SOURCE_CONTRACT_VERSION`.

## Release asset and verification

The Node validator will consume the same release-owned asset at
`src/common/ingest/mrms/core-contract.json`; it is outside the operator
configuration tree. Python wheels include it as package data, and Node package
contents include that source path. Regenerate explicitly from the Python owner:

```bash
PYTHONPATH=src python -m common.ingest.mrms.core_contract \
  > src/common/ingest/mrms/core-contract.json
```

Tests compare the asset byte-for-byte with the generator and use
`tests/fixtures/config/mrms_products_v2.json` for reusable normalization
acceptance/rejection cases. Phase 3 can reuse these cases in Node.

Run in the `EdgeWARN` environment:

```bash
python -m pytest tests/core/ingest/mrms/test_registry.py
python -m pytest tests/core/ingest/mrms tests/architecture tests/packaging
```

Coverage includes empty additions, all 21 default identities and aliases, new
CONUS products, negative temperatures, nonzero elevations, exact AzShear paths,
unsafe names, length boundaries, duplicate/reserved collisions, UTC source
grammars, immutable snapshots, disabled lookup, fingerprints, serialization,
and import/build independence from runtime configuration and filesystem I/O.

Verification on 2026-09-27: **370 passed, 1 skipped** across MRMS,
architecture, and packaging suites in the EdgeWARN environment. Existing async
downloader tests required execution outside the restricted sandbox. An offline
wheel build contained the exact generated contract bytes; an npm package dry
run included the same asset. Architecture checks explicitly recognize the
planned MRMS source/contract ownership while retaining their negative controls.
