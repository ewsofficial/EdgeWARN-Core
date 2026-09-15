"""Cycle catalog construction, requirement evaluation, and the status record.

Three groups of tests live here, and they fail for different reasons.

*Vocabulary agreement* is the promise ``readiness.py`` makes in its own module
docstring: it restates five enums and two patterns from ``docs/ctam/schema/``
rather than importing the documentation tree, and these tests read the schema
files and assert the copies still agree. A failure means the two sources of
truth have diverged, and the schema wins.

*Serialized shape* asserts that everything the module emits validates against
those schemas, using the repository's own walker from
``common.config.loader`` -- the same hand-rolled validator the config stack
runs, not ``jsonschema``. A payload that only validates under a stricter
third-party validator would be a false negative at the real API boundary.

*Verdict semantics* pins which of the five readiness values each degraded
artifact earns. Phase 5 skips a module whose requirements are unmet, so a wrong
verdict here either starves a module that could have run or runs one against a
file that is not on disk. These are the tests that make a behavior change
visible instead of silent.

Artifacts are built in ``tmp_path`` and ``util.file.CELL_DIR`` is redirected,
because ``build_catalog`` reads that global at call time. Nothing here touches
the real data tree.
"""

from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import util.file as fs
from common.config.loader import _walk
from common.ingest.manifest import CycleInputManifest, StagedInput
from EdgeWARN.ctam import readiness as R
from EdgeWARN.ctam.discovery import (
    STATE_DISCOVERED,
    STATE_INVALID,
    STATE_SKIPPED_DISABLED,
    DiscoveredModule,
    DiscoveryResult,
)
from EdgeWARN.ctam.limits import RESERVED_MODULE_IDS
from EdgeWARN.ctam.manifest import (
    ManifestError,
    ModuleManifest,
    ModuleRequirement,
    parse_manifest,
    parse_selector,
)

pytestmark = pytest.mark.ctam


REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "docs" / "ctam" / "schema"

CYCLE_ID = "20260805-120000"
CYCLE_TIME = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)

# Real entries in the host's MRMS catalog. `parse_selector` validates a product
# against that catalog, so an invented name would fail before reaching readiness.
REFLECTIVITY = "MergedReflectivityQCComposite_00.50"
VIL = "VIL_00.50"
MESH = "MESH_00.50"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


FILE_DESCRIPTOR_SCHEMA = load_schema("file-descriptor.schema.json")
EVALUATION_SCHEMA = load_schema("requirements-evaluation.schema.json")
STATUS_SCHEMA = load_schema("status-record.schema.json")


def check_schema(schema: dict, value, label: str) -> None:
    """Validate with the repository's own walker, reporting every error."""
    errors: list = []
    _walk(schema, value, [], errors)
    assert not errors, f"{label}: " + "; ".join(
        f"{'.'.join(str(p) for p in path) or '<root>'} {message}"
        for path, message in errors
    )


@pytest.fixture
def cell_dir(tmp_path, monkeypatch) -> Path:
    """Redirect the cell-history directory ``build_catalog`` reads at call time."""
    directory = tmp_path / "cells"
    directory.mkdir()
    monkeypatch.setattr(fs, "CELL_DIR", str(directory))
    return directory


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def snapshot(path: Path, *, latest: str | None = CYCLE_ID, features=()) -> Path:
    """A stormcell snapshot in the host's real shape: a flat dict, not GeoJSON."""
    payload = {
        "source": "MRMS",
        "product": REFLECTIVITY,
        "version": 1,
        "features": list(features),
    }
    if latest is not None:
        payload["latest_timestamp"] = latest
    return write_json(path, payload)


def staged(
    path: Path,
    *,
    product: str = REFLECTIVITY,
    family: str = "mrms",
    analysis_time: datetime | None = None,
    validated: bool = True,
    role: str = "current",
) -> StagedInput:
    return StagedInput(
        product=product,
        path=str(path),
        analysis_time=analysis_time or CYCLE_TIME,
        source=family,
        family=family,
        validated=validated,
        role=role,
    )


def input_manifest(*records: StagedInput) -> CycleInputManifest:
    return CycleInputManifest(cycle_time=CYCLE_TIME, inputs=records)


def grib(tmp_path: Path, name: str = "reflectivity.grib2", size: int = 32) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return path


def build(
    *,
    cells=(),
    stormcell_path: Path | None = None,
    manifest: CycleInputManifest | None = None,
    historical: bool = False,
    now: datetime | None = None,
):
    return R.build_catalog(
        cells=list(cells),
        timestamp=CYCLE_ID,
        stormcell_path=stormcell_path,
        input_manifest=manifest,
        historical=historical,
        now=now if now is not None else CYCLE_TIME,
    )


def requirement(
    selector: str,
    *,
    required: bool = True,
    max_age: float | None = None,
    min_history: int | None = None,
) -> ModuleRequirement:
    return ModuleRequirement(
        selector=parse_selector(selector),
        required=required,
        max_age_seconds=max_age,
        min_history_entries=min_history,
    )


def module_manifest(*requires: ModuleRequirement, module_id: str = "cellstats") -> ModuleManifest:
    return ModuleManifest(
        module_id=module_id,
        name="CellStats",
        version="1.2.3",
        api_version="1",
        enabled=True,
        required=True,
        scope="cycle",
        entrypoint=("{python}", "main.py"),
        timeout_seconds=10,
        after=(),
        requires=tuple(requires),
        writes=(),
        directory=Path("modules") / module_id,
        manifest_path=Path("modules") / module_id / "module.toml",
    )


def discovered(manifest: ModuleManifest | None, **kwargs) -> DiscoveredModule:
    module_id = kwargs.pop("module_id", None) or (
        manifest.module_id if manifest is not None else "broken"
    )
    return DiscoveredModule(
        module_id=module_id,
        directory=Path("modules") / module_id,
        manifest=manifest,
        state=kwargs.pop("state", STATE_DISCOVERED),
        reason=kwargs.pop("reason", None),
    )


def only(catalog, kind: str):
    return tuple(entry for entry in catalog.files if entry.kind == kind)


def sole(catalog, kind: str):
    matches = only(catalog, kind)
    assert len(matches) == 1, f"expected one {kind} descriptor, got {len(matches)}"
    return matches[0]


def evaluate(catalog, *requires: ModuleRequirement, now: datetime | None = None) -> dict:
    return R.evaluate_requirements(
        module_manifest(*requires), catalog, now=now if now is not None else CYCLE_TIME
    )


def first(evaluation: dict) -> dict:
    return evaluation["requirements"][0]


# --------------------------------------------------------------------------
# Frozen vocabulary agreement with docs/ctam/schema/
# --------------------------------------------------------------------------
# readiness.py duplicates these rather than importing the documentation tree.
# The module docstring names this file as the reason the copies cannot drift, so
# a failure here is that promise breaking.


def test_kind_vocabulary_matches_file_descriptor_schema():
    assert set(R.KINDS) == set(FILE_DESCRIPTOR_SCHEMA["properties"]["kind"]["enum"])
    assert len(R.KINDS) == len(set(R.KINDS))


def test_role_vocabulary_matches_file_descriptor_schema():
    assert set(R.ROLES) == set(FILE_DESCRIPTOR_SCHEMA["properties"]["role"]["enum"])
    assert len(R.ROLES) == len(set(R.ROLES))


def test_readiness_vocabulary_matches_file_descriptor_schema():
    assert set(R.READINESS_VALUES) == set(
        FILE_DESCRIPTOR_SCHEMA["properties"]["readiness"]["enum"]
    )
    assert len(R.READINESS_VALUES) == len(set(R.READINESS_VALUES))


def test_unmet_conditions_match_evaluation_schema_including_null():
    """``None`` is an enum *member* there, not an absent key."""
    schema_enum = EVALUATION_SCHEMA["properties"]["requirements"]["items"]["properties"][
        "unmet_condition"
    ]["enum"]
    assert set(R.UNMET_CONDITIONS) == set(schema_enum)
    assert None in R.UNMET_CONDITIONS
    assert len(R.UNMET_CONDITIONS) == len(set(R.UNMET_CONDITIONS))


def test_cycle_states_match_status_record_schema():
    assert set(R.CYCLE_STATES) == set(STATUS_SCHEMA["properties"]["state"]["enum"])
    assert len(R.CYCLE_STATES) == len(set(R.CYCLE_STATES))
    # The three Phase 1 can actually reach are named constants, not bare strings.
    for state in (
        R.CYCLE_STATE_CATALOG_BUILDING,
        R.CYCLE_STATE_REQUIREMENTS_EVALUATED,
        R.CYCLE_STATE_NOT_READY,
    ):
        assert state in R.CYCLE_STATES


def test_module_states_match_status_record_schema():
    schema_enum = STATUS_SCHEMA["properties"]["modules"]["additionalProperties"][
        "properties"
    ]["state"]["enum"]
    assert set(R.MODULE_STATES) == set(schema_enum)
    assert len(R.MODULE_STATES) == len(set(R.MODULE_STATES))
    # Discovery owns three of them; readiness must reuse those constants rather
    # than spelling the strings a second time.
    for state in (STATE_DISCOVERED, STATE_INVALID, STATE_SKIPPED_DISABLED):
        assert state in R.MODULE_STATES


def test_file_id_pattern_matches_schema_verbatim():
    assert R.FILE_ID_PATTERN == FILE_DESCRIPTOR_SCHEMA["properties"]["file_id"]["pattern"]


def test_cycle_id_pattern_matches_schema_verbatim():
    assert R.CYCLE_ID_PATTERN == STATUS_SCHEMA["properties"]["cycle_id"]["pattern"]


def test_status_schema_version_matches_schema_const():
    assert R.STATUS_SCHEMA_VERSION == STATUS_SCHEMA["properties"]["schema_version"]["const"]


def test_api_version_matches_schema_const():
    assert R.API_VERSION == STATUS_SCHEMA["properties"]["api_version"]["const"]


@pytest.mark.parametrize(
    "media_type",
    sorted(set(R._FAMILY_MEDIA_TYPES.values()) | set(R._SUFFIX_MEDIA_TYPES.values())
           | {R._DEFAULT_MEDIA_TYPE, R._JSON_MEDIA_TYPE}),
)
def test_every_emittable_media_type_matches_the_schema_pattern(media_type):
    """A media type the schema would reject cannot be reached by any input."""
    pattern = FILE_DESCRIPTOR_SCHEMA["properties"]["media_type"]["pattern"]
    assert re.search(pattern, media_type) is not None


# --------------------------------------------------------------------------
# Cycle identity
# --------------------------------------------------------------------------


def test_cycle_id_is_the_pipeline_timestamp_verbatim():
    """No reformatting step exists that could disagree with the pipeline."""
    catalog = build()
    assert catalog.cycle_id == CYCLE_ID
    assert R.parse_cycle_id(CYCLE_ID) == CYCLE_TIME
    check_schema(STATUS_SCHEMA["properties"]["cycle_id"], catalog.cycle_id, "cycle_id")


def test_parse_cycle_id_returns_an_aware_utc_datetime():
    parsed = R.parse_cycle_id(CYCLE_ID)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "bad",
    [
        "2026080-120000",       # eight digits required
        "20260805_120000",      # separator is a hyphen
        "20260805-1200",        # six digits required
        " 20260805-120000",     # no leading whitespace
        "20260805-120000\n",    # \Z rejects a trailing newline, unlike $
        "",
        12345,                  # not a string
        None,
    ],
)
def test_parse_cycle_id_rejects_a_non_conforming_timestamp(bad):
    with pytest.raises(ValueError) as excinfo:
        R.parse_cycle_id(bad)
    # The message has to name the schema, because this is what an operator sees.
    assert "status-record.schema.json" in str(excinfo.value)


@pytest.mark.parametrize("impossible", ["20260230-120000", "20260805-250000"])
def test_pattern_valid_but_impossible_timestamps_still_raise_value_error(impossible):
    r"""Characterizes a known gap: the message comes from ``strptime``, not us.

    ``^[0-9]{8}-[0-9]{6}\Z`` admits a February 30th and an hour 25, so the
    calendar check falls through to ``datetime.strptime`` and the caller gets
    "day is out of range for month" with no pointer to the cycle-id contract.
    The exception *type* is still ``ValueError``, which is the part callers
    depend on, so this test pins the contract that holds and marks the message
    quality that does not.
    """
    assert re.search(R.CYCLE_ID_PATTERN, impossible) is not None
    with pytest.raises(ValueError) as excinfo:
        R.parse_cycle_id(impossible)
    assert "status-record.schema.json" not in str(excinfo.value)


def test_catalog_analysis_time_is_the_parsed_cycle_with_an_explicit_offset():
    catalog = build()
    assert catalog.analysis_time == "2026-08-05T12:00:00+00:00"
    check_schema(
        STATUS_SCHEMA["properties"]["analysis_time"], catalog.analysis_time, "analysis_time"
    )


def test_catalog_cell_count_is_the_input_cell_list_length(cell_dir):
    catalog = build(cells=[{"id": "A"}, {"id": "B"}, {"id": "C"}])
    assert catalog.cell_count == 3


# --------------------------------------------------------------------------
# Input descriptor readiness
# --------------------------------------------------------------------------


def test_input_descriptor_ready_when_present_validated_and_aligned(cell_dir, tmp_path):
    path = grib(tmp_path)
    manifest = input_manifest(staged(path))
    catalog = build(manifest=manifest)
    entry = sole(catalog, R.KIND_INPUT)
    assert entry.readiness == R.READY
    assert entry.available is True
    assert entry.validated is True
    assert entry.size_bytes == path.stat().st_size
    check_schema(FILE_DESCRIPTOR_SCHEMA, entry.as_dict(), "input descriptor")


def test_input_descriptor_unavailable_when_file_missing(cell_dir, tmp_path):
    missing_path = tmp_path / "never-written.grib2"
    manifest = input_manifest(staged(missing_path))
    catalog = build(manifest=manifest)
    entry = sole(catalog, R.KIND_INPUT)
    assert entry.readiness == R.UNAVAILABLE
    assert entry.available is False
    assert entry.validated is False
    assert "never-written.grib2" in entry.reason


def test_input_descriptor_unavailable_when_oversized(cell_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_STREAMED_FILE_BYTES", 8)
    path = grib(tmp_path, size=32)
    manifest = input_manifest(staged(path))
    catalog = build(manifest=manifest)
    entry = sole(catalog, R.KIND_INPUT)
    assert entry.readiness == R.UNAVAILABLE
    assert entry.available is True
    assert entry.validated is False
    assert entry.size_bytes == 32


def test_input_descriptor_invalid_when_not_validated(cell_dir, tmp_path):
    path = grib(tmp_path)
    manifest = input_manifest(staged(path, validated=False))
    catalog = build(manifest=manifest)
    entry = sole(catalog, R.KIND_INPUT)
    assert entry.readiness == R.INVALID
    assert entry.validated is False


def test_input_descriptor_stale_when_outside_alignment_window(cell_dir, tmp_path):
    path = grib(tmp_path)
    manifest = input_manifest(
        staged(path, analysis_time=CYCLE_TIME - timedelta(seconds=300))
    )
    catalog = build(manifest=manifest)
    entry = sole(catalog, R.KIND_INPUT)
    assert entry.readiness == R.STALE
    assert "alignment window" in entry.reason


def test_input_descriptor_ignores_non_current_role_for_alignment(cell_dir, tmp_path):
    """``validate_alignment`` only checks ``current``, so a stale ``previous`` stays ready."""
    path = grib(tmp_path)
    manifest = input_manifest(
        staged(
            path,
            role="previous",
            analysis_time=CYCLE_TIME - timedelta(hours=3),
        )
    )
    catalog = build(manifest=manifest)
    entry = sole(catalog, R.KIND_INPUT)
    assert entry.readiness == R.READY


# --------------------------------------------------------------------------
# Stormcells descriptor readiness
# --------------------------------------------------------------------------


def test_stormcells_pending_when_missing_and_within_grace(cell_dir):
    catalog = build(stormcell_path=None, now=CYCLE_TIME)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.PENDING
    assert entry.available is False


def test_stormcells_unavailable_when_missing_past_grace(cell_dir):
    now = CYCLE_TIME + timedelta(seconds=R.PENDING_GRACE_SECONDS + 1)
    catalog = build(stormcell_path=None, now=now)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.UNAVAILABLE


def test_stormcells_pending_when_missing_and_historical_regardless_of_age(cell_dir):
    now = CYCLE_TIME + timedelta(days=3)
    catalog = build(stormcell_path=None, now=now, historical=True)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.PENDING


def test_stormcells_ready_when_snapshot_matches_cycle(cell_dir, tmp_path):
    path = snapshot(tmp_path / "stormcells.json", latest=CYCLE_ID)
    catalog = build(stormcell_path=path)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.READY
    check_schema(FILE_DESCRIPTOR_SCHEMA, entry.as_dict(), "stormcells descriptor")


def test_stormcells_stale_when_snapshot_is_a_different_cycle(cell_dir, tmp_path):
    path = snapshot(tmp_path / "stormcells.json", latest="20260805-115800")
    catalog = build(stormcell_path=path)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.STALE
    assert "20260805-115800" in entry.reason


def test_stormcells_ready_when_snapshot_uses_iso_timestamp(cell_dir, tmp_path):
    """The pipeline writes ISO ``latest_timestamp``; the catalog tracks the
    compact cycle id. A same-cycle snapshot must be ready, not stale."""
    path = snapshot(tmp_path / "stormcells.json", latest="2026-08-05T12:00:00")
    catalog = build(stormcell_path=path)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.READY


def test_same_cycle_normalizes_iso_and_compact_forms():
    assert R._same_cycle("2026-08-05T12:00:00", CYCLE_TIME) is True
    assert R._same_cycle("20260805-120000", CYCLE_TIME) is True
    assert R._same_cycle("2026-08-05T12:02:00", CYCLE_TIME) is False
    assert R._same_cycle("not-a-timestamp", CYCLE_TIME) is False


def test_external_module_terminal_states_are_defined():
    """``run.py`` writes these after the external-module phase; a missing
    constant fails every external module before launch."""
    assert R.CYCLE_STATE_COMPLETED in R.CYCLE_STATES
    assert R.CYCLE_STATE_FAILED in R.CYCLE_STATES


def test_stormcells_invalid_when_not_json(cell_dir, tmp_path):
    path = tmp_path / "stormcells.json"
    path.write_text("not json {{{", encoding="utf-8")
    catalog = build(stormcell_path=path)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.INVALID


def test_stormcells_invalid_when_missing_features_key(cell_dir, tmp_path):
    path = write_json(tmp_path / "stormcells.json", {"source": "MRMS"})
    catalog = build(stormcell_path=path)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.INVALID
    assert "features" in entry.reason


def test_stormcells_unavailable_when_oversized(cell_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_STREAMED_FILE_BYTES", 4)
    path = snapshot(tmp_path / "stormcells.json", latest=CYCLE_ID)
    catalog = build(stormcell_path=path)
    entry = sole(catalog, R.KIND_STORMCELLS)
    assert entry.readiness == R.UNAVAILABLE
    assert entry.available is True


# --------------------------------------------------------------------------
# Cell-history descriptor readiness
# --------------------------------------------------------------------------


def test_cell_history_pending_when_missing_and_within_grace(cell_dir):
    catalog = build(cells=[{"id": "A"}], now=CYCLE_TIME)
    entry = sole(catalog, R.KIND_CELL_HISTORY)
    assert entry.readiness == R.PENDING
    assert "first scan" in entry.reason


def test_cell_history_unavailable_when_missing_past_grace(cell_dir):
    now = CYCLE_TIME + timedelta(seconds=R.PENDING_GRACE_SECONDS + 1)
    catalog = build(cells=[{"id": "A"}], now=now)
    entry = sole(catalog, R.KIND_CELL_HISTORY)
    assert entry.readiness == R.UNAVAILABLE


def test_cell_history_ready_when_present_as_list(cell_dir):
    write_json(cell_dir / "A.json", [{"timestamp": CYCLE_ID}])
    catalog = build(cells=[{"id": "A"}])
    entry = sole(catalog, R.KIND_CELL_HISTORY)
    assert entry.readiness == R.READY
    check_schema(FILE_DESCRIPTOR_SCHEMA, entry.as_dict(), "cell history descriptor")


def test_cell_history_invalid_when_not_a_list(cell_dir):
    write_json(cell_dir / "A.json", {"not": "a list"})
    catalog = build(cells=[{"id": "A"}])
    entry = sole(catalog, R.KIND_CELL_HISTORY)
    assert entry.readiness == R.INVALID


def test_cell_history_invalid_when_not_json(cell_dir):
    (cell_dir / "A.json").write_text("not json", encoding="utf-8")
    catalog = build(cells=[{"id": "A"}])
    entry = sole(catalog, R.KIND_CELL_HISTORY)
    assert entry.readiness == R.INVALID


def test_cell_history_unavailable_when_oversized(cell_dir, monkeypatch):
    monkeypatch.setattr(R, "MAX_STREAMED_FILE_BYTES", 4)
    write_json(cell_dir / "A.json", [{"timestamp": CYCLE_ID}])
    catalog = build(cells=[{"id": "A"}])
    entry = sole(catalog, R.KIND_CELL_HISTORY)
    assert entry.readiness == R.UNAVAILABLE
    assert entry.available is True


def test_cell_history_ignores_cells_without_an_id(cell_dir):
    catalog = build(cells=[{"id": "A"}, {"no_id": True}])
    assert catalog.cell_count == 2
    assert len(only(catalog, R.KIND_CELL_HISTORY)) == 1


# --------------------------------------------------------------------------
# CTAMCycleCatalog.resolve() fan-out
# --------------------------------------------------------------------------


def test_resolve_cells_history_returns_every_cell_regardless_of_id(cell_dir):
    write_json(cell_dir / "A.json", [])
    write_json(cell_dir / "B.json", [])
    catalog = build(cells=[{"id": "A"}, {"id": "B"}])
    matches = catalog.resolve("cells.history")
    assert {m.file_id for m in matches} == {
        R._cell_history_file_id("A"),
        R._cell_history_file_id("B"),
    }


def test_resolve_stormcells_current_matches_the_synthetic_descriptor(cell_dir, tmp_path):
    path = snapshot(tmp_path / "stormcells.json", latest=CYCLE_ID)
    catalog = build(stormcell_path=path)
    matches = catalog.resolve("stormcells.current")
    assert len(matches) == 1
    assert matches[0].kind == R.KIND_STORMCELLS
    assert matches[0].role == R.ROLE_CURRENT


def test_resolve_input_filters_by_family_product_role(cell_dir, tmp_path):
    path = grib(tmp_path)
    manifest = input_manifest(staged(path))
    catalog = build(manifest=manifest)
    selector = f"input:mrms:{REFLECTIVITY}:current"
    assert len(catalog.resolve(selector)) == 1
    assert catalog.resolve(f"input:mrms:{VIL}:current") == ()
    assert catalog.resolve(f"input:mrms:{REFLECTIVITY}:previous") == ()


def test_resolve_accepts_a_parsed_selector_or_its_raw_string_identically(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path)))
    raw = f"input:mrms:{REFLECTIVITY}:current"
    assert catalog.resolve(raw) == catalog.resolve(parse_selector(raw))


# --------------------------------------------------------------------------
# No recursive filesystem scan: only pinned/explicit paths are ever consulted
# --------------------------------------------------------------------------


def test_build_catalog_ignores_files_in_cell_dir_not_named_by_a_cell(cell_dir):
    write_json(cell_dir / "A.json", [])
    write_json(cell_dir / "unrelated-stray-file.json", [])
    catalog = build(cells=[{"id": "A"}])
    file_ids = {e.file_id for e in only(catalog, R.KIND_CELL_HISTORY)}
    assert file_ids == {R._cell_history_file_id("A")}


def test_build_catalog_ignores_unreferenced_files_beside_a_staged_input(cell_dir, tmp_path):
    path = grib(tmp_path, name="reflectivity.grib2")
    grib(tmp_path, name="unreferenced-sibling.grib2")
    catalog = build(manifest=input_manifest(staged(path)))
    assert len(only(catalog, R.KIND_INPUT)) == 1


def test_build_catalog_with_no_manifest_emits_no_input_descriptors(cell_dir):
    catalog = build(manifest=None)
    assert only(catalog, R.KIND_INPUT) == ()


# --------------------------------------------------------------------------
# evaluate_requirements() semantics
# --------------------------------------------------------------------------


def test_requirement_satisfied_when_a_ready_descriptor_matches(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path)))
    result = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    assert result["satisfied"] is True
    entry = first(result)
    assert entry["satisfied"] is True
    assert entry["unmet_condition"] is None
    check_schema(EVALUATION_SCHEMA, result, "evaluation")


def test_requirement_unmet_missing_when_nothing_was_staged(cell_dir):
    catalog = build(manifest=None)
    result = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    assert result["satisfied"] is False
    entry = first(result)
    assert entry["unmet_condition"] == R.UNMET_MISSING
    assert entry["file_ids"] == []


def test_requirement_role_mismatch_when_staged_under_a_different_role(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(
        manifest=input_manifest(staged(path, role="previous"))
    )
    result = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    entry = first(result)
    assert entry["unmet_condition"] == R.UNMET_ROLE_MISMATCH
    assert "previous" in entry["reason"]


def test_requirement_not_validated_when_descriptor_is_invalid(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path, validated=False)))
    result = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    entry = first(result)
    assert entry["unmet_condition"] == R.UNMET_NOT_VALIDATED


def test_requirement_analysis_time_mismatch_when_descriptor_is_stale(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(
        manifest=input_manifest(
            staged(path, analysis_time=CYCLE_TIME - timedelta(seconds=300))
        )
    )
    result = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    entry = first(result)
    assert entry["unmet_condition"] == R.UNMET_ANALYSIS_TIME_MISMATCH


def test_requirement_max_age_satisfied_within_threshold(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path)))
    result = evaluate(
        catalog, requirement(f"input:mrms:{REFLECTIVITY}:current", max_age=600.0)
    )
    entry = first(result)
    assert entry["satisfied"] is True
    assert entry["threshold"] == 600.0


def test_requirement_max_age_unmet_beyond_threshold(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path, analysis_time=CYCLE_TIME - timedelta(seconds=90))))
    result = evaluate(
        catalog, requirement(f"input:mrms:{REFLECTIVITY}:current", max_age=30.0)
    )
    entry = first(result)
    assert entry["unmet_condition"] == R.UNMET_MAX_AGE
    assert entry["observed"] == pytest.approx(90.0)
    assert entry["threshold"] == 30.0


def test_requirement_min_history_satisfied_when_one_cell_qualifies(cell_dir):
    write_json(cell_dir / "A.json", [{}])
    write_json(cell_dir / "B.json", [{}, {}, {}])
    catalog = build(cells=[{"id": "A"}, {"id": "B"}])
    result = evaluate(catalog, requirement("cells.history", min_history=2))
    entry = first(result)
    assert entry["satisfied"] is True
    assert entry["observed"] == 3.0


def test_requirement_min_history_unmet_when_no_cell_qualifies(cell_dir):
    write_json(cell_dir / "A.json", [{}])
    catalog = build(cells=[{"id": "A"}])
    result = evaluate(catalog, requirement("cells.history", min_history=2))
    entry = first(result)
    assert entry["unmet_condition"] == R.UNMET_MIN_HISTORY
    assert entry["observed"] == 1.0
    assert entry["threshold"] == 2.0


def test_requirement_optional_unmet_does_not_fail_top_level_satisfied(cell_dir):
    catalog = build(manifest=None)
    result = evaluate(
        catalog, requirement(f"input:mrms:{REFLECTIVITY}:current", required=False)
    )
    assert result["satisfied"] is True
    assert first(result)["satisfied"] is False


def test_evaluate_requirements_reports_every_requirement_in_manifest_order(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path)))
    result = evaluate(
        catalog,
        requirement("cells.history"),
        requirement(f"input:mrms:{REFLECTIVITY}:current"),
        requirement(f"input:mrms:{VIL}:current"),
    )
    assert [r["selector"] for r in result["requirements"]] == [
        "cells.history",
        f"input:mrms:{REFLECTIVITY}:current",
        f"input:mrms:{VIL}:current",
    ]
    assert result["satisfied"] is False  # VIL was never staged and is required
    check_schema(EVALUATION_SCHEMA, result, "evaluation")


# --------------------------------------------------------------------------
# cycle_status() / write_cycle_status()
# --------------------------------------------------------------------------


def test_cycle_status_rejects_a_state_outside_the_frozen_enum(cell_dir):
    catalog = build()
    with pytest.raises(ValueError):
        R.cycle_status(
            catalog=catalog,
            discovery=(),
            state="not_a_real_state",
            started_at=CYCLE_TIME,
        )


def test_module_entry_state_discovered_when_no_evaluation_was_run(cell_dir):
    manifest = module_manifest()
    module = discovered(manifest)
    status = R.cycle_status(
        catalog=build(),
        discovery=(module,),
        state=R.CYCLE_STATE_CATALOG_BUILDING,
        started_at=CYCLE_TIME,
    )
    assert status["modules"]["cellstats"]["state"] == STATE_DISCOVERED
    assert status["modules"]["cellstats"]["requirements_satisfied"] is None


def test_module_entry_state_ready_when_requirements_are_satisfied(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path)))
    manifest = module_manifest(requirement(f"input:mrms:{REFLECTIVITY}:current"))
    module = discovered(manifest)
    evaluation = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    status = R.cycle_status(
        catalog=catalog,
        discovery=(module,),
        evaluations={"cellstats": evaluation},
        state=R.CYCLE_STATE_REQUIREMENTS_EVALUATED,
        started_at=CYCLE_TIME,
    )
    assert status["modules"]["cellstats"]["state"] == R.MODULE_STATE_READY
    assert status["modules"]["cellstats"]["requirements_satisfied"] is True
    assert "unmet_requirements" not in status["modules"]["cellstats"]


def test_module_entry_state_skipped_missing_requirements_when_unsatisfied(cell_dir):
    catalog = build(manifest=None)
    manifest = module_manifest(requirement(f"input:mrms:{REFLECTIVITY}:current"))
    module = discovered(manifest)
    evaluation = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    status = R.cycle_status(
        catalog=catalog,
        discovery=(module,),
        evaluations={"cellstats": evaluation},
        state=R.CYCLE_STATE_REQUIREMENTS_EVALUATED,
        started_at=CYCLE_TIME,
    )
    entry = status["modules"]["cellstats"]
    assert entry["state"] == R.MODULE_STATE_SKIPPED_MISSING_REQUIREMENTS
    assert entry["requirements_satisfied"] is False
    assert entry["unmet_requirements"] == [f"input:mrms:{REFLECTIVITY}:current"]


def test_module_entry_state_invalid_passes_through_from_discovery(cell_dir):
    module = discovered(None, module_id="broken", state=STATE_INVALID, reason="bad toml")
    status = R.cycle_status(
        catalog=build(),
        discovery=(module,),
        state=R.CYCLE_STATE_NOT_READY,
        started_at=CYCLE_TIME,
    )
    entry = status["modules"]["broken"]
    assert entry["state"] == STATE_INVALID
    assert entry["version"] is None
    assert entry["api_version"] is None
    assert entry["error"] == "bad toml"


def test_module_entry_state_skipped_disabled_passes_through_from_discovery(cell_dir):
    manifest = module_manifest(module_id="cellstats")
    module = discovered(manifest, state=STATE_SKIPPED_DISABLED)
    status = R.cycle_status(
        catalog=build(),
        discovery=(module,),
        state=R.CYCLE_STATE_NOT_READY,
        started_at=CYCLE_TIME,
    )
    assert status["modules"]["cellstats"]["state"] == STATE_SKIPPED_DISABLED


def test_module_entry_marks_reserved_module_ids(cell_dir):
    reserved_id = next(iter(RESERVED_MODULE_IDS))
    module = discovered(None, module_id=reserved_id, state=STATE_INVALID, reason="reserved")
    status = R.cycle_status(
        catalog=build(),
        discovery=(module,),
        state=R.CYCLE_STATE_NOT_READY,
        started_at=CYCLE_TIME,
    )
    assert status["modules"][reserved_id]["reserved"] is True


def test_cycle_status_accepts_a_discovery_result_or_a_bare_iterable(cell_dir):
    module = discovered(module_manifest())
    catalog = build()
    from_tuple = R.cycle_status(
        catalog=catalog, discovery=(module,), state=R.CYCLE_STATE_CATALOG_BUILDING,
        started_at=CYCLE_TIME,
    )
    from_result = R.cycle_status(
        catalog=catalog,
        discovery=DiscoveryResult(root=Path("modules"), root_present=True, modules=(module,)),
        state=R.CYCLE_STATE_CATALOG_BUILDING,
        started_at=CYCLE_TIME,
    )
    assert from_tuple == from_result


def test_cycle_status_payload_validates_against_status_schema(cell_dir, tmp_path):
    path = grib(tmp_path)
    catalog = build(manifest=input_manifest(staged(path)))
    manifest = module_manifest(requirement(f"input:mrms:{REFLECTIVITY}:current"))
    module = discovered(manifest)
    evaluation = evaluate(catalog, requirement(f"input:mrms:{REFLECTIVITY}:current"))
    status = R.cycle_status(
        catalog=catalog,
        discovery=(module,),
        evaluations={"cellstats": evaluation},
        state=R.CYCLE_STATE_REQUIREMENTS_EVALUATED,
        started_at=CYCLE_TIME,
        finished_at=CYCLE_TIME + timedelta(seconds=2),
        ctam_ready=True,
    )
    check_schema(STATUS_SCHEMA, status, "status record")


def test_write_cycle_status_round_trips_through_atomic_write(cell_dir, tmp_path):
    catalog = build()
    status = R.cycle_status(
        catalog=catalog,
        discovery=(),
        state=R.CYCLE_STATE_CATALOG_BUILDING,
        started_at=CYCLE_TIME,
    )
    destination = R.write_cycle_status(status, base_dir=tmp_path)
    assert destination == R.ctam_status_path(CYCLE_ID, base_dir=tmp_path)
    assert json.loads(destination.read_text(encoding="utf-8")) == status


# --------------------------------------------------------------------------
# Frozen dataclasses
# --------------------------------------------------------------------------


def test_catalog_file_is_immutable():
    catalog = build()
    entry = catalog.files[0]
    with pytest.raises(FrozenInstanceError):
        entry.readiness = R.PENDING


def test_ctam_cycle_catalog_is_immutable():
    catalog = build()
    with pytest.raises(FrozenInstanceError):
        catalog.cell_count = 99
