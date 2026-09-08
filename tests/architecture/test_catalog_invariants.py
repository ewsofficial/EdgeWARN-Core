"""Phase 3 step 8: uniqueness and coverage invariants over the YAML catalogs.

The Phase 0 snapshots in ``test_catalog_baseline.py`` prove the YAML matches the
source *today*. These tests are the complement: they assert the properties an
editor could break tomorrow without changing any snapshot -- a duplicated
upstream modifier, an output directory claimed twice, a readiness entry that
names a product nobody ingests, a ``filepath`` naming an attribute
``util.file`` does not define.

Lengths are asserted explicitly next to each catalog for the same reason the
Phase 0 tests do it: a silently dropped entry is the likeliest regression, and a
membership test alone would not catch one.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

import util.file as fs
from common.config import loader
from tests.architecture.baseline import requires

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_catalog_declares_schema_version_one():
    """Phase 5 acceptance criterion: all shipped catalogs use schema v1."""
    for name in loader.CONFIG_NAMES:
        assert loader.load_config(name)["schema_version"] == 1


def duplicates(values):
    """Return the values that appear more than once, with their counts."""
    return {value: count for value, count in Counter(values).items() if count > 1}


@pytest.fixture(scope="module")
def mrms_goes():
    return loader.load_config("ingest")


@pytest.fixture(scope="module")
def render():
    return loader.load_config("ewmrs_render")


@pytest.fixture(scope="module")
def integration():
    return loader.load_config("integration")


@pytest.fixture(scope="module")
def nexrad():
    return loader.load_config("nexrad")


# --- NEXRAD sweep selection -----------------------------------------------
#
# The loader's schema walker has no `propertyNames` or cross-field support, so
# these relationships can only be enforced here. Each one describes a way to
# edit the catalog into a state that raises nothing and silently renders a bin
# or a VCP unreachable.

def test_canonical_elevation_bins_are_ascending(nexrad):
    """Snapping is by nearest bin, so order is not required for correctness.

    It is asserted anyway because the bins are published as an ordered readiness
    tuple: `volume_discovery` walks them in catalog order, and an out-of-order
    entry would reorder readiness checks for no stated reason.
    """
    bins = list(nexrad["selection"]["canonical_elevation_bins"])
    assert bins == sorted(bins)


def test_the_elevation_cutoff_does_not_orphan_the_top_bin(nexrad):
    """A cutoff below the highest bin makes that bin unreachable.

    `grouping._finalize_group` drops a group whose representative angle exceeds
    `high_max_angle_deg` *before* snapping, so a cutoff of, say, 3.5 would leave
    the 4.0 bin defined and permanently empty rather than erroring.
    """
    selection = nexrad["selection"]
    assert selection["high_max_angle_deg"] >= max(selection["canonical_elevation_bins"])


def test_the_sweep_floor_does_not_orphan_the_bottom_bin(nexrad):
    """A floor above the lowest bin means that bin is only ever reached downward.

    `parser` discards a sweep below `min_sweep_angle_deg` outright, so a floor
    above 0.5 would leave the 0.5 bin reachable only by a higher sweep rounding
    down onto it.
    """
    selection = nexrad["selection"]
    assert selection["min_sweep_angle_deg"] <= min(selection["canonical_elevation_bins"])


def test_waveform_names_are_distinct(nexrad):
    """The grouper branches on these by equality, in order.

    Were `doppler` to equal `surveillance`, every doppler sweep would open a new
    group instead of joining one, and no group would ever satisfy its
    required-waveform check -- a total ingest stall with nothing logged.
    """
    waveforms = nexrad["selection"]["waveforms"]
    names = [waveforms["surveillance"], waveforms["doppler"], *waveforms["single_elevation"]]
    assert duplicates(names) == {}


# --- MRMS ingest and readiness --------------------------------------------

def test_mrms_products_are_unique(mrms_goes):
    products = mrms_goes["mrms"]["products"]
    assert len(products) == 21
    assert duplicates([(p["region"], p["product"]) for p in products]) == {}


def test_mrms_product_outdirs_are_unique(mrms_goes):
    """Two products sharing an output directory would interleave their files."""
    outdirs = [p["outdir"] for p in mrms_goes["mrms"]["products"]]
    assert duplicates(outdirs) == {}


def test_mrms_readiness_is_a_subset_of_ingest(mrms_goes):
    """A readiness check on an un-ingested product would never be satisfied."""
    mrms = mrms_goes["mrms"]
    assert len(mrms["check_products"]) == 12

    ingested = {(p["region"], p["product"]) for p in mrms["products"]}
    orphans = [
        (p["region"], p["product"])
        for p in mrms["check_products"]
        if (p["region"], p["product"]) not in ingested
    ]
    assert orphans == []


def test_mrms_readiness_entries_are_unique(mrms_goes):
    entries = [(p["region"], p["product"]) for p in mrms_goes["mrms"]["check_products"]]
    assert duplicates(entries) == {}


def test_detection_membership_names_ingested_products(mrms_goes):
    """The detection list selects by product name, so a typo silently matches nothing.

    ``None`` is a legitimate member: it is the ProbSevere entry, which has no
    modifier component in its bucket path.
    """
    mrms = mrms_goes["mrms"]
    detection = mrms["membership_lists"]["detection"]
    available = {p["product"] for p in mrms["products"]}
    assert [name for name in detection if name not in available] == []


# --- GOES ABI channels ----------------------------------------------------

def test_goes_abi_channels_are_unique(mrms_goes):
    channels = mrms_goes["goes"]["abi_channels"]
    assert len(channels) == 16
    assert duplicates([c["id"] for c in channels]) == {}
    assert duplicates([c["name"] for c in channels]) == {}
    assert duplicates([c["outdir"] for c in channels]) == {}


# --- Render layers --------------------------------------------------------

def test_mrms_render_layers_are_unique(render):
    layers = render["mrms_layers"]
    assert len(layers) == 15
    assert duplicates([layer["name"] for layer in layers]) == {}
    assert duplicates([layer["filepath"] for layer in layers]) == {}
    assert duplicates([layer["outdir"] for layer in layers]) == {}


def test_goes_render_layers_are_unique(render):
    layers = render["goes_layers"]["layers"]
    assert len(layers) == 16
    assert duplicates([layer["name"] for layer in layers]) == {}
    assert duplicates([layer["channel_id"] for layer in layers]) == {}
    assert duplicates([layer["outdir"] for layer in layers]) == {}


def test_goes_render_layers_name_ingested_channels(render, mrms_goes):
    """A render layer for an un-ingested channel would find no input files."""
    ingested = {c["id"] for c in mrms_goes["goes"]["abi_channels"]}
    layers = render["goes_layers"]["layers"]
    assert [l["channel_id"] for l in layers if l["channel_id"] not in ingested] == []


# --- Integration datasets -------------------------------------------------

def test_integration_stats_dataset_names_and_keys_are_unique(integration):
    datasets = integration["stats_datasets"]
    assert len(datasets) == 25
    assert duplicates([d["name"] for d in datasets]) == {}
    assert duplicates([d["key"] for d in datasets]) == {}


def test_integration_stats_datasets_may_share_a_source_directory(integration):
    """`filepath` is deliberately NOT unique -- several statistics per source.

    Pinned so that a future uniqueness sweep does not add the wrong invariant
    here: four datasets legitimately read MRMS_VIL_DIR.
    """
    counts = duplicates([d["filepath"] for d in integration["stats_datasets"]])
    assert counts, "expected at least one source directory to feed several datasets"
    assert max(counts.values()) == 4


def test_probsevere_field_map_targets_are_unique(integration):
    """Two catalog keys reading one upstream field would be a transcription slip.

    The map does contain one known source typo (`SRH02km` -> `SRW02KM`, from
    integrator.py:31), but it is still a one-to-one mapping.
    """
    field_map = integration["probsevere_field_map"]
    assert len(field_map) == 40
    assert duplicates(list(field_map.values())) == {}


def test_rap_product_transforms_are_known(integration):
    """`transform` names are resolved by lookup, so an unknown one raises at load."""
    requires("xarray")
    from EdgeWARN.process.integrate.integrate_rap import TRANSFORMS

    named = [p["transform"] for p in integration["rap_products"]["products"] if "transform" in p]
    assert named, "expected at least one product to declare a transform"
    assert [name for name in named if name not in TRANSFORMS] == []


def test_rap_isobaric_levels_are_unique_and_descending(integration):
    levels = list(integration["rap_products"]["isobaric_levels_mb"])
    assert len(levels) == 37
    assert duplicates(levels) == {}
    assert levels == sorted(levels, reverse=True)


# --- Filesystem attribute resolution --------------------------------------

def _attribute_names():
    """Every catalog value that is meant to name a ``util.file`` attribute."""
    mrms_goes = loader.load_config("ingest")
    render = loader.load_config("ewmrs_render")
    integration = loader.load_config("integration")
    runtime = loader.load_config("runtime")

    names: set[str] = set()
    for product in mrms_goes["mrms"]["products"]:
        names.add(product["outdir"])
    for product in mrms_goes["mrms"]["check_products"]:
        names.add(product["outdir"])
    for channel in mrms_goes["goes"]["abi_channels"]:
        names.add(channel["outdir"])
    names.add(mrms_goes["goes"]["glm_outdir"])

    for layer in render["mrms_layers"]:
        names.update((layer["filepath"], layer["outdir"]))
    for layer in render["goes_layers"]["layers"]:
        names.update((layer["filepath"], layer["outdir"]))

    for dataset in integration["stats_datasets"]:
        names.add(dataset["filepath"])

    names.add(runtime["cycle"]["state_file"]["dir"])
    names.add(runtime["supervisor"]["health_file"]["dir"])
    names.add(runtime["supervisor"]["nexrad_heartbeat_file"]["dir"])
    return names


def test_every_named_directory_resolves_on_util_file():
    """The catalogs address directories by attribute name, resolved with getattr.

    A renamed or dropped ``util.file`` global would otherwise surface as an
    AttributeError deep inside a render worker rather than at load time.
    """
    names = _attribute_names()
    assert len(names) >= 70
    assert sorted(name for name in names if not hasattr(fs, name)) == []


def test_named_directories_are_paths_not_arbitrary_globals():
    """Guards against a name that resolves but to something that is not a path."""
    from pathlib import PurePath

    wrong = sorted(
        name for name in _attribute_names() if not isinstance(getattr(fs, name), PurePath)
    )
    assert wrong == []


# --- The two loaders must agree on which catalogs exist -------------------

def _js_config_names() -> list[str]:
    """The CONFIG_NAMES array from the JS loader, in declaration order."""
    import re

    source = (REPO_ROOT / "src/config/loader.js").read_text(encoding="utf-8")
    match = re.search(r"CONFIG_NAMES\s*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, "could not locate CONFIG_NAMES in src/config/loader.js"
    return re.findall(r"'([^']+)'", match.group(1))


def test_python_and_js_loaders_list_the_same_catalogs():
    """Two hand-rolled loaders, one set of files, and nothing kept them in step.

    The JS list had gone on naming `alerts` after that catalog was split into
    `scheduler` and `api_index`, which made `npm run validate-config` fail on a
    missing file *and* silently skip validating the two replacements. Order is
    compared too: these are parallel ports, and a diff is easier to read when
    they stay aligned.
    """
    assert _js_config_names() == list(loader.CONFIG_NAMES)


def test_both_loaders_expand_the_same_path_tokens():
    """A token Node expands and Python does not is a path that resolves two ways.

    The list is short enough to compare textually, and that is the point: the
    expander is the only sanctioned way a catalog names a filesystem location, so
    the two ports have to agree on which names exist before they can agree on
    where they lead.
    """
    source = (REPO_ROOT / "src/config/loader.js").read_text(encoding="utf-8")
    match = re.search(r"PATH_TOKENS\s*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, "could not locate PATH_TOKENS in src/config/loader.js"
    assert re.findall(r"'([^']+)'", match.group(1)) == list(loader.PATH_TOKENS)


def test_both_loaders_reject_traversal_out_of_an_expanded_root():
    """Node's half cannot be executed here (no local Node), so it is pinned by text.

    Each assertion stands for one of the rejections
    ``tests/unit/config/test_loader.py`` exercises against the Python expander.
    They are asserted rather than run because a silently missing check on the Node
    side is exactly the asymmetry a shared expander was introduced to remove.

    The last two were added after the first pass: both loaders resolved a falsy
    root against the working directory, and both accepted a NUL byte -- which on
    Windows renders as a space and so reached a real but different file. Neither is
    reachable from a schema-valid catalog today, and both are cheap to reject, so
    they are rejected rather than documented.

    These are where the ports agree on *inputs*. One asymmetry survives and is
    not a defect this test can close: Python's containment check runs after
    ``Path.resolve()``, which follows symlinks, so it also rejects a link inside
    the root that points out of it. ``path.resolve`` does no such thing, so Node's
    fourth check is a backstop against its own path arithmetic rather than a
    symlink defense. Closing it would need ``fs.realpathSync``, and the Node
    loader reads only files it also validates, so the gap is recorded here rather
    than papered over with an assertion that would imply parity it does not have.
    """
    source = (REPO_ROOT / "src/config/loader.js").read_text(encoding="utf-8")
    body = source[source.index("export function expandPath"):]

    assert "segments.includes('..')" in body, "no `..` rejection"
    assert "remainder.startsWith('/')" in body, "no absolute-remainder rejection"
    assert "template.includes('\\\\')" in body, "no backslash rejection"
    assert "path.relative(root, resolved)" in body, "no containment backstop"
    assert "template.includes('\\0')" in body, "no NUL rejection"
    assert "path.isAbsolute(givenRoot)" in body, "no absolute-root requirement"


def test_the_api_config_delegates_token_expansion_to_the_shared_loader():
    """The Node expander had a second copy in the API's own config factory.

    It enforced the same two rules with its own error text, which is the shape
    that drifts: a rule added to one copy is not added to the other. Asserted as
    the absence of the copy rather than as agreement with it.
    """
    source = (REPO_ROOT / "src/api/config/index.js").read_text(encoding="utf-8")

    assert "expandPath" in source
    assert "const prefix = '<base_dir>/'" not in source
    assert "template.slice(prefix.length)" not in source
    assert "Invalid api.yaml base_dir.derived" not in source


def test_the_filesystem_schema_pattern_names_exactly_the_code_allowlist():
    """The schema repeats the allowlist so both loaders reject a typo at validation.

    A second owner, so it is pinned to the first. The schema catches the common
    case (a misspelled token) before any expander runs, and in both languages at
    once; the expander is what enforces the rules a `pattern` cannot express.
    """
    schema = json.loads(
        (REPO_ROOT / "config/schema/filesystem.schema.json").read_text(encoding="utf-8")
    )
    pattern = schema["properties"]["colormap_search_path"]["items"]["pattern"]
    named = re.findall(r"[a-z_]+_dir", pattern)
    assert named == list(loader.PATH_TOKENS)


def test_every_listed_catalog_has_a_file_and_a_schema():
    """A name in either list with no file on disk is a guaranteed load failure."""
    missing = [
        name
        for name in loader.CONFIG_NAMES
        if not (REPO_ROOT / "config" / f"{name}.yaml").is_file()
        or not (REPO_ROOT / "config" / "schema" / f"{name}.schema.json").is_file()
    ]
    assert missing == []


def test_no_catalog_file_is_absent_from_the_loaders():
    """The reverse coverage check: a new YAML nobody validates is dead weight."""
    on_disk = sorted(path.stem for path in (REPO_ROOT / "config").glob("*.yaml"))
    assert on_disk == sorted(loader.CONFIG_NAMES)


def test_every_shipped_catalog_loads_and_schema_validates():
    """The Python half of ``npm run validate-config``.

    The two tests above only stat the files. Nothing else loaded all 19, so a
    schema reaching for an unimplemented keyword, or a catalog violating its own
    schema, could ship as long as no individual accessor test happened to read
    that file. Loading every name exercises ``_check_supported_keywords`` and the
    walker across the whole tree, which is what makes the hand-rolled validator
    an acceptable substitute for ``jsonschema`` (see the loader module docstring).
    """
    for name in loader.CONFIG_NAMES:
        loader.load_config(name)


# --- API security settings the schema cannot constrain --------------------

@pytest.fixture(scope="module")
def api():
    return loader.load_config("api")


def test_trust_proxy_is_never_blanket_true(api):
    """`true` means "trust every peer's X-Forwarded-For", and no schema can reject it.

    The key is legitimately tri-shaped -- `false`, an integer hop count, or a list
    of proxy addresses -- so `type` must admit booleans, and the walker applies
    `minimum`/`maximum` only to numbers (loader.py, and loader.js:144 the same
    way). `enum` cannot help either, because it would have to enumerate every
    valid address list. The one distinguishing feature of the unsafe value is the
    literal, so it can only be asserted here. The Node parser also rejects `true`
    at runtime, because environment values and YAML booleans share that unsafe
    semantic branch.

    Spoofable forwarding headers defeat both the rate limiter and any
    address-based decision made downstream of it.
    """
    assert api["security"]["trust_proxy"] is not True


def test_credentialed_cors_is_never_paired_with_a_wildcard_origin(api):
    """A cross-field rule, so it is unreachable from the schema walker.

    Either half is defensible alone: a wildcard origin on an unauthenticated
    read-only API is merely broad, and credentials with a fixed origin list is
    ordinary. Together they are the classic full-read hole. `allowed_origins`
    already rejects `*` by pattern, so this guards the pairing rather than
    repeating that check.
    """
    security = api["security"]
    if security["cors"]["credentials"]:
        assert "*" not in security["allowed_origins"]
        assert security["allowed_origins"], "credentialed CORS needs an explicit origin list"


def test_pagination_and_grid_defaults_do_not_exceed_their_maxima(api):
    """Defaults above their own ceilings, which no schema keyword can compare."""
    pagination = api["pagination"]
    assert pagination["default_limit"] <= pagination["max_limit"]

    defaults = api["render_defaults"]
    maxima = defaults["grid_maxima"]
    for axis, ceiling in maxima.items():
        assert defaults["grid"][axis] <= ceiling, axis


# --- The radar-moment allowlist has three owners -------------------------
#
# `api.yaml validation.radar_products` is the owner of record. `ancillary.js` used
# to be a fourth owner and is now a reader, so the test below asserts the absence
# of its copy rather than agreement with it. Two copies remain -- the OpenAPI enum
# and the renderer's producing map -- and neither reads the catalog, so for those
# these tests are what makes the duplication safe: they fail the moment a copy is
# edited alone. They are deliberately written as equality against the catalog
# rather than pairwise, so the catalog stays the thing being tracked.

RADAR_OWNERS = "the radar allowlist has three owners; edit config/api.yaml and re-run"


def test_ancillary_js_reads_the_radar_allowlist_instead_of_restating_it(api):
    """The service that gates every radar request now derives its set from the catalog.

    This replaces an equality check against a module-level
    `const RADAR_PRODUCTS = new Set([...])`. That literal is gone, so the thing worth
    guarding inverted: no copy may grow back, and the read has to stay *inside* the
    factory. `createConfig` resolves the config directory from argv and env after
    every import has already been evaluated, so a module-scope read would bind to the
    default `config/` however `--config-dir` was set -- and would do it silently.
    """
    source = (REPO_ROOT / "src/api/services/ancillary.js").read_text(encoding="utf-8")

    assert "RADAR_PRODUCTS" not in source
    products = list(api["validation"]["radar_products"])
    assert products, "an empty allowlist would make the loop below vacuous"
    for product in products:
        assert product not in source, RADAR_OWNERS

    assert "new Set(config.validation.radar_products)" in source

    # The factory parameter must be the only way config enters this module. Bounding
    # the reads below the factory's opening line is not enough on its own -- anything
    # appended after its closing brace is also "below" it -- so the load-bearing half
    # is that the module never imports the loader and so has nothing to read at
    # import time.
    assert "config/loader.js" not in source
    assert "loadConfig" not in source
    factory = source.index("export function createAncillaryServices(repository, config)")
    assert all(
        index > factory
        for index in (match.start() for match in re.finditer(r"\bconfig\.", source))
    )


def test_openapi_radar_product_enum_matches_the_catalog(api):
    """The published contract, which has no runtime authority at all.

    Despite the `.yaml` extension the document is JSON: it is read as text,
    JSON-parsed for its `paths` keys, and served verbatim. Nothing validates a
    request against this enum -- `productId` reaches `radarField` unchecked -- so
    a stale enum misleads clients rather than rejecting them, which is precisely
    why it needs a test instead of trust.
    """
    import json

    document = json.loads(
        (REPO_ROOT / "src/api/openapi/v3.yaml").read_text(encoding="utf-8")
    )
    enum = document["components"]["parameters"]["radarProductId"]["schema"]["enum"]
    assert enum == list(api["validation"]["radar_products"]), RADAR_OWNERS


def test_the_renderer_produces_exactly_the_moments_the_api_serves(api):
    """`RAW_NEXRAD_BLOCK_VARIABLE_NAMES` maps tape codes to these same names.

    This is the producing end: the API allowlist is only meaningful if the
    renderer actually writes files under those names. Compared as sets because
    the mapping is keyed by NEXRAD block code and carries no ordering.
    """
    from NEXRAD.render import RAW_NEXRAD_BLOCK_VARIABLE_NAMES

    produced = set(RAW_NEXRAD_BLOCK_VARIABLE_NAMES.values())
    assert produced == set(api["validation"]["radar_products"]), RADAR_OWNERS


def test_ccorh_is_servable_but_deliberately_uncolored(api, render):
    """The one intended asymmetry, pinned so it cannot be "fixed" by accident.

    The API accepts CCORH and the renderer writes it, but it has no colormap, so
    the GUI never draws it. Someone reconciling the two lists would either add a
    colormap or drop the product; both are real decisions, and this test forces
    them to be made rather than stumbled into.
    """
    colored = set(render["nexrad_gui"]["variable_colormaps"])
    served = set(api["validation"]["radar_products"])
    assert served - colored == {"CCORH"}
    assert colored - served == set()


def test_the_renderer_reads_the_colormap_mapping_instead_of_restating_it(render):
    """`ewmrs_render.yaml` is the sole owner of moment-to-colormap.

    The renderer used to hold this mapping as a module constant, which meant the
    catalog key was decorative: editing it changed nothing the GUI drew. Asserting
    the constant is *absent* is the part that matters -- an accessor that agrees
    with a surviving constant still leaves two owners.
    """
    from NEXRAD import render as nexrad
    from EWMRS.render.config import nexrad_variable_colormaps

    assert not hasattr(nexrad, "NEXRAD_VARIABLE_COLORMAP_KEYS")
    assert nexrad_variable_colormaps() == render["nexrad_gui"]["variable_colormaps"]


def test_the_colormap_accessor_hands_back_a_mutable_copy(render):
    """`load_config` deep-freezes, and callers must not be able to reach through.

    The renderer only calls `.get`, but a frozen mapping returned directly would
    make any future mutation raise from inside the catalog rather than at the call
    site, and would let one caller's edit leak into every later reader.
    """
    from EWMRS.render.config import nexrad_variable_colormaps

    mapping = nexrad_variable_colormaps()
    mapping["DBZH"] = "mutated"
    assert nexrad_variable_colormaps()["DBZH"] == render["nexrad_gui"]["variable_colormaps"]["DBZH"]
    assert nexrad_variable_colormaps()["DBZH"] != "mutated"


# --- API product catalog --------------------------------------------------

PRODUCT_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"


def _node_product_catalog():
    import json

    return json.loads(
        (REPO_ROOT / "src/api/config/product-catalog.json").read_text(encoding="utf-8")
    )


def test_api_product_catalog_route_keys_are_unique(api):
    """`id` and `legacyId` are both used to address a product over HTTP.

    The length is compared against `product_catalog.entries` rather than a literal.
    api.yaml records the count and the JSON holds the entries; nothing derives one
    from the other, and `createConfig` reports the *recorded* number as
    `diagnostics.effective.renderProductCount`, so a catalog that gained an entry
    would keep serving the stale count with no schema able to notice.
    """
    entries = _node_product_catalog()
    assert len(entries) == api["product_catalog"]["entries"]

    assert duplicates([entry["id"] for entry in entries]) == {}
    assert duplicates([entry["legacyId"] for entry in entries]) == {}
    assert duplicates([entry["storageDirectory"] for entry in entries]) == {}
    assert duplicates([entry["legacyFilePrefix"] for entry in entries]) == {}


def test_api_product_catalog_entries_carry_every_field_the_loader_dereferences():
    """A count alone would pass for 31 entries that are missing every field.

    `productCatalog.js:5-19` validates at import, so a malformed entry takes the API
    down at startup rather than on the request that needs it. It checks the `id` slug
    and the three uniqueness constraints, but only truthiness for the rest, and it
    never looks at `representation` -- which `renders.js:48,51` copies straight into
    an HTTP response.

    Deliberately shape-only, no values: `baselines/node_product_catalog.json` already
    pins this file byte-for-byte, so asserting particular ids or colormaps here would
    only duplicate that snapshot and fail twice for one edit. What is *not* in the
    snapshot is which fields the code dereferences, so that is what this covers.
    """
    entries = _node_product_catalog()
    required = {"id", "legacyId", "storageDirectory", "legacyFilePrefix", "representation"}

    for entry in entries:
        label = entry.get("id")
        assert required <= set(entry), label
        assert re.fullmatch(PRODUCT_SLUG, entry["id"]), label
        for field in required - {"id"}:
            assert isinstance(entry[field], str) and entry[field], (label, field)
        if "colormapId" in entry:
            assert isinstance(entry["colormapId"], str) and entry["colormapId"], label
