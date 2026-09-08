"""Phase 0 tests pinning each known configuration disagreement.

These tests assert the behavior that exists **today**, including where it is
almost certainly a bug. Phase 0 is explicitly value-preserving: the point is to
make each disagreement visible and impossible to change by accident, so that a
later phase resolves it as a reviewed decision rather than a silent side effect
of moving a literal into YAML.

Every test below names the decision the migration still owes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from tests.architecture.source_inspect import has_param_default, param_default

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- Kalman: the YAML is now the single copy of every value ---------------

KALMAN_CONFIG_PATH = "EdgeWARN/process/detect/kalman/config.py"


def _dataclass_field_defaults(relative_path: str, class_name: str) -> dict:
    """Map field name -> literal default, for fields that declare one."""
    import ast

    from tests.architecture.source_inspect import SRC, _literal

    tree = ast.parse((SRC / relative_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                statement.target.id: _literal(statement.value)
                for statement in node.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.value is not None
            }
    raise AssertionError(f"{class_name} not found in {relative_path}")


def _kalman_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "kalman.yaml").read_text(encoding="utf-8"))


def test_tracking_max_prediction_time_resolves_to_the_yaml_value_only():
    """RESOLVED in Phase 4: the value is 6.0, and there is only one of it.

    The disagreement was between a 6.0 dataclass field default and a
    `.get('max_prediction_time_minutes', 10.0)` fallback that would have taken
    over if the key were ever dropped. Both copies are gone: the dataclass
    declares no default and the loader is given no fallback, so a missing key
    is a startup error rather than a silent switch to 10 minutes.
    """
    from EdgeWARN.process.detect.kalman.config import TrackingConfig

    assert _dataclass_field_defaults(KALMAN_CONFIG_PATH, "TrackingConfig") == {}

    yaml_value = _kalman_yaml()["tracking"]["max_prediction_time_minutes"]
    assert yaml_value == 6.0
    assert TrackingConfig.from_yaml().max_prediction_time_minutes == yaml_value

    source = (REPO_ROOT / "src" / KALMAN_CONFIG_PATH).read_text(encoding="utf-8")
    assert "10.0" not in source


def test_no_kalman_config_field_declares_a_default():
    """Every base default lives in the YAML, so no dataclass may restate one."""
    for class_name in (
        "KalmanConfig",
        "TrackingConfig",
        "AssignmentConfig",
        "FilterInternalsConfig",
        "ConfidenceConfig",
        "AssignmentCostsConfig",
    ):
        assert _dataclass_field_defaults(KALMAN_CONFIG_PATH, class_name) == {}, class_name


def test_kalman_config_reads_the_yaml_without_inline_fallbacks():
    """The 19 `.get(key, fallback)` calls are gone; keys are required outright."""
    import ast

    from tests.architecture.source_inspect import SRC

    tree = ast.parse((SRC / KALMAN_CONFIG_PATH).read_text(encoding="utf-8"))
    fallback_gets = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) >= 2
    ]
    assert fallback_gets == []


def test_every_kalman_yaml_section_is_consumed():
    """No section is inert: each one is subscripted by name in the loader."""
    import ast

    from tests.architecture.source_inspect import SRC

    tree = ast.parse((SRC / KALMAN_CONFIG_PATH).read_text(encoding="utf-8"))
    subscripted = {
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }

    sections = set(_kalman_yaml()) - {"schema_version"}
    assert sections == {
        "kalman_filter",
        "tracking",
        "assignment",
        "filter_internals",
        "confidence",
        "assignment_costs",
    }
    assert sections <= subscripted


def test_process_noise_declares_only_the_scalar_the_filter_reads():
    """RESOLVED (Phase 5): `process_noise.position` and `.velocity` are gone.

    Both were inert, and the catalog asserted the opposite -- it said all three
    "enter np.diag directly, so each is a VARIANCE". They did enter an np.diag,
    into `_Q` in `_initialize_noise_matrices`, but `_Q` was read by nothing.
    `predict()` builds Q per step from dt via `_build_process_noise_matrix`, which
    consumes `acceleration` alone. An operator retuning the filter would have
    moved the two keys with no effect and believed the comment.

    Deleted rather than wired: `_build_process_noise_matrix` implements the
    piecewise-constant white-noise-jerk model, in which the position and velocity
    blocks of Q are *derived* from this one scalar times a power of dt. Giving
    them independent variances changes the filter's model, so it would retune
    storm tracking rather than merely relocate a literal.

    Asserted behaviourally: `predict()` must respond to `acceleration` and the
    dead field must not come back.
    """
    import dataclasses

    import numpy as np

    from EdgeWARN.process.detect.kalman.config import KalmanConfig
    from EdgeWARN.process.detect.kalman.filter import KalmanFilter

    recorded = _kalman_yaml()["kalman_filter"]["process_noise"]
    assert set(recorded) == {"acceleration"}

    fields = {field.name for field in dataclasses.fields(KalmanConfig)}
    assert "process_noise_position" not in fields
    assert "process_noise_velocity" not in fields

    # The precomputed constant Q is gone, not merely unread.
    filter_source = (
        REPO_ROOT / "src/EdgeWARN/process/detect/kalman/filter.py"
    ).read_text(encoding="utf-8")
    assert "self._Q" not in filter_source

    # And the surviving scalar reaches the matrix predict() actually uses.
    from EdgeWARN.process.detect.kalman.config import default_kalman_config

    base = default_kalman_config()
    loose = dataclasses.replace(base, process_noise_acceleration=10.0)
    tight = dataclasses.replace(base, process_noise_acceleration=0.001)

    def _q_trace(config):
        return float(np.trace(KalmanFilter(config=config)._build_process_noise_matrix(60.0)))

    assert _q_trace(loose) > _q_trace(tight)


# --- Lineage overlap: two defaults, each owned in lineage.yaml --------------

def test_each_lineage_overlap_concept_has_exactly_one_owner():
    """RESOLVED: no literal competes with the YAML for either ratio.

    `overlap_threshold` has two configured meanings. Each has one owner and the
    optional declaration sites default to `None`, so callers can still override
    without a literal shadowing the catalog:

    - the tracked merge/split gate -> `lineage.yaml tracked_overlap_ratio`
    - a directly built detector     -> `lineage.yaml tracked_overlap_ratio`

    A bare spatial query must receive an explicit threshold, eliminating the
    former third configuration path.
    """
    declaration_sites = {
        ("EdgeWARN/process/detect/track.py", "StormCellTracker.__init__"),
        ("EdgeWARN/process/detect/lineage/detector.py", "LineageDetector.__init__"),
        ("EdgeWARN/process/detect/lineage/detector.py", "detect_lineage_events"),
    }
    for relative_path, qualified_name in declaration_sites:
        assert param_default(relative_path, qualified_name, "overlap_threshold") is None, (
            f"{relative_path}::{qualified_name} still declares a literal default"
        )

    detector_source = (
        REPO_ROOT / "src/EdgeWARN/process/detect/lineage/detector.py"
    ).read_text(encoding="utf-8")
    assert "DEFAULT_OVERLAP_THRESHOLD" not in detector_source

    assert _lineage_yaml()["lineage"]["tracked_overlap_ratio"] == 0.10
    assert "lineage_overlap_ratio" not in _detection_yaml()["tracker"]

    from EdgeWARN.process.detect.lineage.config import tracked_overlap_ratio
    from EdgeWARN.process.detect.lineage.detector import LineageDetector
    from EdgeWARN.process.detect.track import StormCellTracker

    assert tracked_overlap_ratio() == 0.10
    assert StormCellTracker(None, None, None).overlap_threshold == 0.10
    assert LineageDetector().overlap_threshold == 0.10

    assert not has_param_default(
        "EdgeWARN/process/detect/lineage/spatial.py",
        "find_overlapping_cells",
        "overlap_threshold",
    )

    # The tracker forwards its configured value into the detector.
    track_source = (REPO_ROOT / "src/EdgeWARN/process/detect/track.py").read_text(encoding="utf-8")
    assert "overlap_threshold=self.overlap_threshold" in track_source


def test_lineage_buffer_thresholds_have_no_literal_defaults():
    """`LineageBuffer()` is constructed with no kwargs in production.

    `track.py` calls `LineageBuffer.load(stormcell_dir)`, so whatever the
    constructor defaults to is what runs. Literals there would outrank
    lineage.yaml for the entire tracked path.
    """
    buffer_keys = ("min_confirmations", "max_pending", "prune_after_scans", "scan_interval_seconds")
    for key in buffer_keys:
        assert param_default(
            "EdgeWARN/process/detect/lineage/buffer.py", "LineageBuffer.__init__", key
        ) is None, f"LineageBuffer.__init__ still declares a literal {key}"

    yaml_buffer = _lineage_yaml()["lineage"]["buffer"]
    assert yaml_buffer == {
        "min_confirmations": 2,
        "max_pending": 100,
        "prune_after_scans": 5,
        "scan_interval_seconds": 120.0,
    }


def _lineage_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "lineage.yaml").read_text(encoding="utf-8"))


def _detection_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "detection.yaml").read_text(encoding="utf-8"))


# --- API index: one flag the two pipelines answer differently -------------

def test_remove_old_cells_stays_split_between_the_two_pipelines():
    """RESOLVED: three declaration sites, one resolution point, two recorded answers.

    The realtime and historical pipelines genuinely disagree, so this cannot become
    a single key. Every declaration site now defaults to `None` and forwards, and
    only `APIIndexManager` resolves -- which is what keeps `False` distinguishable
    from "caller said nothing". A literal `True` restored at any forwarding site
    would make a replay start deleting cells.
    """
    from EdgeWARN.api_integration.config import (
        remove_old_cells_historical,
        remove_old_cells_realtime,
    )

    for relative_path, qualified_name in (
        ("EdgeWARN/api_integration/index_manager.py", "APIIndexManager.__init__"),
        ("EdgeWARN/pipeline.py", "run_edgewarn_integration_phase"),
        ("EdgeWARN/process/integrate/pipeline.py", "main"),
    ):
        assert param_default(relative_path, qualified_name, "remove_old_cells") is None, (
            f"{relative_path}::{qualified_name} still declares a literal default"
        )

    recorded = _api_index_yaml()["api_index"]["remove_old_cells"]
    assert remove_old_cells_realtime() is recorded["realtime"] is True
    assert remove_old_cells_historical() is recorded["historical"] is False

    pipeline_source = (REPO_ROOT / "src/EdgeWARN/pipeline.py").read_text(encoding="utf-8")
    assert "remove_old_cells=remove_old_cells_historical()" in pipeline_source
    assert "remove_old_cells=False" not in pipeline_source


def test_inactive_cell_age_is_a_separate_owner_from_alert_cleanup_age():
    """RESOLVED: two 120s, two owners, and only one of them is in a catalog.

    One expires cells from the API index; the other prunes alert files. They are
    equal today, which is exactly why a single key would look correct right up to
    the first time one subsystem needs a different budget. CTAM alert extraction is
    deferred, so `cleanup_expired` keeps its literal and remains its sole owner --
    a later phase moves it, and this test is what stops it being folded into the
    api_index key on the way.
    """
    from EdgeWARN.api_integration.config import inactive_cell_max_age_minutes

    recorded = _api_index_yaml()["api_index"]["inactive_cell_max_age_minutes"]
    assert inactive_cell_max_age_minutes() == recorded == 120

    assert (
        param_default("EdgeWARN/alerts/manager.py", "AlertManager.cleanup_expired", "max_age_minutes")
        == 120
    ), "the alert cleanup budget moved; give it its own key rather than reusing api_index's"

    source = (REPO_ROOT / "src/EdgeWARN/api_integration/index_manager.py").read_text(encoding="utf-8")
    assert "120 * 60" not in source
    assert "inactive_cell_max_age_minutes() * 60" in source


def test_index_bootstrap_stays_split_between_the_two_pipelines():
    """RESOLVED: the audit filed this under `runtime`, but api_index owns it.

    It gates `APIIndexManager.initialize_indexes()`, so it belongs to the index
    subsystem rather than to the historical runtime that happens to switch it off.
    Same shape as `remove_old_cells`: `None` at the declaration site so an explicit
    `False` stays distinguishable from "caller said nothing", and a literal restored
    in `process_historical.py` would make every replay rebuild the realtime index.
    """
    from EdgeWARN.api_integration.config import (
        initialize_at_startup_historical,
        initialize_at_startup_realtime,
    )

    assert param_default("EdgeWARN/pipeline.py", "initialize_runtime", "initialize_indexes") is None

    recorded = _api_index_yaml()["api_index"]["initialize_at_startup"]
    assert initialize_at_startup_realtime() is recorded["realtime"] is True
    assert initialize_at_startup_historical() is recorded["historical"] is False

    historical_source = (REPO_ROOT / "src/process_historical.py").read_text(encoding="utf-8")
    assert "initialize_indexes=initialize_at_startup_historical()" in historical_source
    assert "initialize_indexes=False" not in historical_source


def test_stormcell_resync_interval_has_no_config_key():
    """No counter can reach an interval here, so no key offers to tune one.

    The only path reaching `update_stormcell_index` is detection, and
    `src/util/runtime/cycle.py` starts `edgewarn_cycle_worker` in a fresh
    `multiprocessing.Process` per cycle. A per-instance counter is therefore zeroed
    before every update, so hoisting the manager inside the worker cannot help --
    the interval needs the counter to outlive the process, not just the call.

    `api_index.resync_every_updates` used to record the number a reused manager
    should adopt, but an operator could edit it and observe nothing, which the
    extraction plan forbids. The rationale now lives in `update_stormcell_index`.
    This test stops the key returning without the index commit first moving
    somewhere that survives a cycle.
    """
    assert "resync_every_updates" not in _api_index_yaml()["api_index"], (
        "the key is back; a counter still cannot survive a cycle, so it would be inert"
    )

    config_source = (REPO_ROOT / "src/EdgeWARN/api_integration/config.py").read_text(encoding="utf-8")
    assert "resync_every_updates" not in config_source, (
        "an accessor came back; the counter still cannot survive a cycle"
    )

    source = (REPO_ROOT / "src/EdgeWARN/api_integration/index_manager.py").read_text(encoding="utf-8")
    assert "stormcell_resync_interval" not in source
    assert "stormcell_updates_since_resync" not in source
    # No literal stood in for the accessor either.
    assert "128" not in source and "500" not in source


# --- Scheduler listing width: two keys, only one of them reached -----------

def _scheduler_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "scheduler.yaml").read_text(encoding="utf-8"))


def _api_index_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "api_index.yaml").read_text(encoding="utf-8"))


def test_scheduler_lookback_and_perf_gate_have_no_literals():
    from EdgeWARN.schedule.config import (
        modifier_lookup_max_entries,
        s3_lookback_hours,
        slow_check_log_threshold_ms,
    )

    recorded = _scheduler_yaml()["scheduler"]
    assert modifier_lookup_max_entries() == recorded["modifier_lookup_max_entries"] == 20
    assert s3_lookback_hours() == recorded["s3_lookback_hours"] == 2
    assert slow_check_log_threshold_ms() == recorded["slow_check_log_threshold_ms"] == 2000

    source = (REPO_ROOT / "src/EdgeWARN/schedule/scheduler.py").read_text(encoding="utf-8")
    assert "timedelta(hours=2)" not in source
    assert "dt > 2000" not in source
    assert "max_entries = modifier_lookup_max_entries()" in source
    assert "max_entries=max_entries," in source
    assert "bucket, 20," not in source


def test_scheduler_check_pools_stay_uncapped_with_no_catalog_key():
    """REVERTED (Phase 5): the two check pools are excluded from extraction.

    `plans/source-configuration-extraction-plan.md:622` excludes both bare
    `ThreadPoolExecutor()` calls -- they have no cap today, so adding one is a
    retune, not an extraction. A `check_max_workers` key was added anyway and then
    reverted; this test is what stops it coming back a third time.

    If a ceiling is ever genuinely wanted, the two pools need separate keys:
    `check_https_fallback` has its own production caller at `src/run.py`, so it is
    not merely the path the primary falls into and the two can run in one tick.
    """
    assert "check_max_workers" not in _scheduler_yaml()["scheduler"]

    schema = json.loads(
        (REPO_ROOT / "config/schema/scheduler.schema.json").read_text(encoding="utf-8")
    )
    scheduler_schema = schema["properties"]["scheduler"]
    assert "check_max_workers" not in scheduler_schema["properties"]
    assert "check_max_workers" not in scheduler_schema["required"]

    config_source = (REPO_ROOT / "src/EdgeWARN/schedule/config.py").read_text(encoding="utf-8")
    assert "check_max_workers" not in config_source

    # Both pools stay bare. Counted rather than merely searched, so a cap
    # reintroduced at one of the two sites still fails.
    source = (REPO_ROOT / "src/EdgeWARN/schedule/scheduler.py").read_text(encoding="utf-8")
    assert source.count("concurrent.futures.ThreadPoolExecutor() as executor:") == 2
    assert "max_workers" not in source


# --- Historical replay: the loop literals now live in the catalog ---------

def _historical_yaml() -> dict:
    import yaml

    return yaml.safe_load(
        (REPO_ROOT / "config" / "historical.yaml").read_text(encoding="utf-8")
    )


def test_historical_replay_loop_holds_no_step_or_throttle_literals():
    """RESOLVED: the replay cadence is owned by historical.yaml.

    `step_minutes` is applied on all three branches of the scan loop, so a literal
    surviving on any one of them would desynchronize the cursor from the other two
    and silently skip or re-scan minutes.
    """
    from EdgeWARN.historical_config import (
        historical_step_minutes,
        historical_throttle_seconds,
    )

    source = (REPO_ROOT / "src/process_historical.py").read_text(encoding="utf-8")
    assert "timedelta(minutes=1)" not in source
    assert "time.sleep(1)" not in source
    assert source.count("current_time += step") == 3

    recorded = _historical_yaml()["historical"]
    assert historical_step_minutes() == recorded["step_minutes"] == 1
    assert historical_throttle_seconds() == recorded["throttle_seconds"] == 1


def test_historical_cleanup_caps_files_but_inherits_the_age_budget():
    """RESOLVED: two catalogs still govern one cleanup call, on purpose.

    `_cleanup_historical_data_dirs` passes `max_files` only, so `clean_old_files`
    applies `filesystem.yaml`'s age budget for the other half. A directory is
    therefore trimmed to `historical.cleanup_max_files` AND anything older than an
    hour is dropped, which means raising the file count alone cannot retain more
    than an hour of replay data. That is not a bug to consolidate away -- the two
    keys have different owners -- but it is surprising enough to pin.
    """
    from EdgeWARN.historical_config import historical_cleanup_max_files
    from util.file_config import cleanup_max_age_minutes

    assert historical_cleanup_max_files() == 5
    assert cleanup_max_age_minutes() == 60

    pipeline_source = (REPO_ROOT / "src/EdgeWARN/pipeline.py").read_text(encoding="utf-8")
    assert "max_files=historical_cleanup_max_files()" in pipeline_source
    assert "max_age_minutes" not in pipeline_source


def test_both_longitude_catalogs_state_the_same_wide_bound():
    """RESOLVED: the two files disagree on convention, so neither schema picks one.

    `runtime.yaml`'s lon_limits is rewritten to 0-360 by io.py:142; nothing
    normalizes `historical.yaml`'s lon, so it stays signed. `handler.py:118-123`
    converts per dataset in either direction, so both conventions reach the same
    subset -- which is why the fix was to widen the schema rather than to force one
    convention on the other file.

    Pinned behaviourally: a 0-360 pair must validate in both files, and 400 must
    still fail, so the bound cannot be "fixed" by narrowing it back to 180 or by
    dropping it entirely.
    """
    import yaml

    from common.config import loader

    def _validates(name: str, document: dict) -> bool:
        schema_path = REPO_ROOT / "config" / "schema" / f"{name}.schema.json"
        try:
            loader._validate(name, document, schema_path)
        except loader.ConfigError:
            return False
        return True

    def _document(name: str) -> dict:
        return yaml.safe_load(
            (REPO_ROOT / "config" / f"{name}.yaml").read_text(encoding="utf-8")
        )

    cases = (
        ("historical", lambda doc, lon: doc["historical"].__setitem__("lon", lon)),
        ("runtime", lambda doc, lon: doc["run"].__setitem__("lon_limits", lon)),
    )
    for name, set_lon in cases:
        assert _validates(name, _document(name)), name

        signed = _document(name)
        set_lon(signed, [-130, -60])
        assert _validates(name, signed), name

        wrapped = _document(name)
        set_lon(wrapped, [230, 300])
        assert _validates(name, wrapped), name

        beyond = _document(name)
        set_lon(beyond, [230, 400])
        assert not _validates(name, beyond), name


def test_no_cleaner_in_util_file_restates_a_retention_number():
    """The three cleaners are not interchangeable, so each defers separately.

    `clean_old_files` applies age and count, `clean_files_by_age` applies age only,
    and `clean_idx_files` applies neither. A literal restored in any signature
    would let one of them drift from the catalog while the others tracked it.
    """
    import yaml

    from util.file_config import cleanup_max_age_minutes, cleanup_max_files

    for qualified_name in ("clean_old_files", "clean_files_by_age", "async_clean_files_by_age"):
        assert param_default("util/file.py", qualified_name, "max_age_minutes") is None

    recorded = yaml.safe_load(
        (REPO_ROOT / "config" / "filesystem.yaml").read_text(encoding="utf-8")
    )["cleanup_defaults"]
    assert cleanup_max_age_minutes() == recorded["max_age_minutes"] == 60
    assert cleanup_max_files() == recorded["max_files"] == 10

    source = (REPO_ROOT / "src/util/file.py").read_text(encoding="utf-8")
    assert "max_age_minutes=60" not in source
    assert "max_files=10" not in source


def test_rap_pre_download_sweep_keeps_its_uncapped_pass():
    """DECISION PRESERVED: `max_files=None` means "no count cap", not "unset".

    `download_rap` sweeps by age before downloading and only applies the count cap
    afterwards, so the pre-download pass must be able to say "age only". That is
    why `clean_old_files` resolves `max_files` from a sentinel rather than `None`:
    treating `None` as unset here would trim the RAP directory a download early.
    """
    import util.file as fs

    assert param_default("util/file.py", "clean_old_files", "max_files") is not None

    source = (REPO_ROOT / "src/common/ingest/synoptic/main.py").read_text(encoding="utf-8")
    assert source.count("max_files=None") == 2
    assert source.count("max_files=rap_max_files()") == 2

    file_source = (REPO_ROOT / "src/util/file.py").read_text(encoding="utf-8")
    assert "if max_files is _FROM_CATALOG:" in file_source
    assert "if max_files is None:" not in file_source.split("def clean_old_files")[1].split("now =")[0]

    assert fs._FROM_CATALOG is not None


# --- RAP filename: two keys describing one naming scheme ------------------

def _synoptic_rap_yaml() -> dict:
    import yaml

    return yaml.safe_load(
        (REPO_ROOT / "config" / "synoptic_rap.yaml").read_text(encoding="utf-8")
    )


def test_rap_filename_regex_round_trips_the_local_file_pattern():
    """RESOLVED: no literal competes with the catalog for the RAP naming scheme.

    `local_file_pattern` writes the cache file and `filename_regex` reads it back,
    so the two must describe the same name. Nothing in the schema can enforce
    that -- one is a `str.format` template and the other a regex -- so editing
    either alone would make `clean_rap_cache` stop recognizing its own downloads
    and warn-and-skip every file instead of pruning it.
    """
    from common.ingest.synoptic import config as rap_config
    from common.ingest.synoptic.main import parse_rap_analysis_time

    recorded = _synoptic_rap_yaml()["rap"]
    written = Path(
        recorded["local_file_pattern"].format(date="20260815", hour=7)
    )
    assert re.match(recorded["filename_regex"], written.name)
    assert parse_rap_analysis_time(written).strftime("%Y%m%d%H") == "2026081507"

    # The accessors are the only owners: the pre-extraction module constants are gone.
    main_source = (
        REPO_ROOT / "src/common/ingest/synoptic/main.py"
    ).read_text(encoding="utf-8")
    assert "RAP_FILENAME_RE = " not in main_source
    assert "RAP_MAX_FILES" not in main_source
    assert rap_config.rap_max_files() == recorded["max_files"] == 3


def test_generic_synoptic_download_demands_an_age_budget():
    """RESOLVED: `download_synoptic` declares no age default, so 180 is the only number.

    It used to default to 60 while this catalog said 180. That was unreachable --
    `download_rap`, the sole caller, always passed `get_rap_max_age_minutes()` --
    but a second synoptic dataset added without the argument would have silently
    run a budget its operator never chose. Defaulting to the RAP key instead would
    be the same fault with the other number: the helper is dataset-generic, and
    this budget also bounds manifest freshness and cache retention.

    Asserted behaviourally, not by AST: re-adding any default (60, 180, or a
    `None` that resolves the RAP key) makes the omitting call succeed.
    """
    assert _synoptic_rap_yaml()["rap"]["max_age_minutes"] == 180
    assert not has_param_default(
        "common/ingest/synoptic/downloader.py", "download_synoptic", "max_age_minutes"
    )

    from common.ingest.synoptic import downloader as synoptic_downloader

    # Raises at call time, before a coroutine exists, so nothing needs awaiting.
    with pytest.raises(TypeError, match="max_age_minutes"):
        synoptic_downloader.download_synoptic(
            datetime(2026, 8, 15, 7, tzinfo=timezone.utc),
            "bucket",
            "file-{hour:02d}",
            "dir-{date}",
            Path("."),
        )

    # The RAP wrapper is where the catalog value enters, and it is the only site.
    downloader_source = (
        REPO_ROOT / "src/common/ingest/synoptic/downloader.py"
    ).read_text(encoding="utf-8")
    assert downloader_source.count("max_age_minutes=get_rap_max_age_minutes()") == 1


# --- WPC: six keys describing one naming scheme ---------------------------

def _wpc_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "wpc.yaml").read_text(encoding="utf-8"))


def test_wpc_cleanup_glob_matches_the_timestamped_name_but_not_latest():
    """RESOLVED: the retention sweep and the writer must agree, and no schema can check it.

    `output_filename_pattern` names the files the sweep is meant to delete and
    `latest_filename` names the one it must never touch. Broadening the glob would
    delete `latest.geojson` -- the file the API serves -- and narrowing it would
    leak every timestamped copy instead. One is a `str.format` template, one a
    glob, one a literal name, so only a round-trip test couples them.

    This also carries forward the refutation of the plan's claimed
    `surface_analysis_*.geojson` mismatch: `surface_analysis` is only a directory
    name, and the sweep and the writer have always agreed on the `wpc_sfc_` prefix.
    """
    import fnmatch

    from common.ingest.wpc.config import cleanup_glob, latest_filename
    from common.ingest.wpc.downloader import get_latest_output_filepath, get_output_filepath

    recorded = _wpc_yaml()["wpc"]
    glob = cleanup_glob()
    assert glob == recorded["cleanup_glob"] == "wpc_sfc_*.geojson"

    timestamped = recorded["output_filename_pattern"].format(date="20260815", hour=18)
    assert fnmatch.fnmatch(timestamped, glob)
    assert not fnmatch.fnmatch(latest_filename(), glob)

    written = get_output_filepath(
        datetime(2026, 8, 15, 18, 30, tzinfo=timezone.utc)
    ).name
    assert fnmatch.fnmatch(written, glob)
    assert get_latest_output_filepath().name == recorded["latest_filename"]


def test_wpc_surface_filename_halves_bracket_the_pattern_the_writer_uses():
    """RESOLVED: the Node reader's two keys and Python's one template name one file.

    `ancillary.js` cannot use `output_filename_pattern` -- it is a `str.format`
    template and `{hour:02d}` means nothing in JS -- so the reader owns a prefix and
    a suffix instead, and those are only safe while they still bracket what the
    writer emits. Drift would not raise: the API would list an empty directory and
    404 files sitting right there. Nothing but this test couples the three strings.

    The reader is deliberately *looser* than the writer, which is pinned here too.
    Python always emits `HH0000`; the API accepts any `\\d{6}`, so hour-aligned names
    are a subset rather than the whole language. Tightening the API to match would
    strand any non-hour-aligned file already on disk.
    """
    recorded = _wpc_yaml()["wpc"]
    prefix = recorded["surface_filename_prefix"]
    suffix = recorded["surface_filename_suffix"]

    written = recorded["output_filename_pattern"].format(date="20260815", hour=18)
    assert written.startswith(prefix)
    assert written.endswith(suffix)

    reader = re.compile(rf"^{re.escape(prefix)}(\d{{8}}-\d{{6}}){re.escape(suffix)}$")
    assert reader.fullmatch(written).group(1) == "20260815-180000"
    assert reader.fullmatch("wpc_sfc_20260815-183000.geojson") is not None
    assert reader.fullmatch(recorded["latest_filename"]) is None

    # The point of the two keys is that the reader stopped restating the name. The
    # regex above is re-derived in Python, so it proves the *keys* agree with the
    # writer but not that JS builds the same thing from them; these pin the two
    # derivations that Node cannot be run here to check.
    source = (REPO_ROOT / "src/api/services/ancillary.js").read_text(encoding="utf-8")
    assert "wpc_sfc_" not in source
    assert ".geojson" not in source
    assert "escapeLiteral(config.wpc.surface_filename_prefix)" in source
    assert "escapeLiteral(config.wpc.surface_filename_suffix)" in source
    assert r"(\\d{8}-\\d{6})" in source
    assert "${config.wpc.surface_filename_prefix}${value}${config.wpc.surface_filename_suffix}" in source


def test_wpc_valid_hours_are_derived_from_the_publish_interval():
    """RESOLVED (Phase 5): the hand-enumerated `valid_hours` key is gone.

    It used to be a second key holding `update_interval_hours` written out by
    hand, with nothing deriving one from the other. The downloader reads only the
    list, so changing the interval alone left a stale list behind and every run
    would request hours WPC never publishes and fall into its fallback path.

    `valid_hours()` now computes `range(0, 24, interval)`, which leaves the
    interval as the sole owner. What the catalog can no longer express, the schema
    now constrains instead: the interval is an `enum` of the divisors of 24, since
    the downloader wraps off the last publish hour to the previous day and an
    interval that does not tile the day would leave a short gap across midnight.
    """
    from common.ingest.wpc.config import update_interval_hours, valid_hours

    recorded = _wpc_yaml()["wpc"]
    assert "valid_hours" not in recorded, "the derived list must not return as a key"

    interval = recorded["update_interval_hours"]
    assert update_interval_hours() == interval == 3
    assert isinstance(interval, int), "range() cannot step by a float"
    assert 24 % interval == 0, "the publish hours must tile the day"

    # Pinned against the literal the catalog used to hold, so the derivation
    # cannot quietly start producing a different schedule than WPC publishes on.
    assert valid_hours() == (0, 3, 6, 9, 12, 15, 18, 21)
    assert valid_hours() == tuple(range(0, 24, interval))

    downloader_source = (
        REPO_ROOT / "src/common/ingest/wpc/downloader.py"
    ).read_text(encoding="utf-8")
    assert "default=hours[-1]" in downloader_source, "the wrap hour must track the list"
    assert "default=21" not in downloader_source


def test_wpc_request_url_round_trips_the_three_keys_that_build_it():
    """`coded_sfc_base_url`, `date_format` and `remote_filename_pattern` form one URL.

    None of them is meaningful alone, and a wrong one produces a 404 that the
    fallback path then masks as a stale-but-successful analysis, so the assertion
    is on the assembled string rather than on the parts.
    """
    from common.ingest.wpc import downloader as wpc_downloader

    recorded = _wpc_yaml()["wpc"]
    reference = datetime(2026, 8, 15, 18, 30, tzinfo=timezone.utc)
    built = wpc_downloader.build_url(reference, 18)

    assert built == "{}/{}/{}".format(
        recorded["coded_sfc_base_url"],
        reference.strftime(recorded["date_format"]),
        recorded["remote_filename_pattern"].format(hour=18),
    )
    assert built == "https://ftp.wpc.ncep.noaa.gov/coded_sfc/20260815/codsus18_hr"


def test_wpc_request_timeout_and_backfill_reach_the_catalog():
    from common.ingest.wpc.config import (
        http_timeout_seconds,
        previous_analysis_lookback_hours,
    )

    recorded = _wpc_yaml()["wpc"]
    assert http_timeout_seconds() == recorded["http_timeout_seconds"] == 30
    assert previous_analysis_lookback_hours() == recorded["previous_analysis_lookback_hours"] == 3

    downloader_source = (
        REPO_ROOT / "src/common/ingest/wpc/downloader.py"
    ).read_text(encoding="utf-8")
    assert downloader_source.count("timeout=http_timeout_seconds()") == 2
    assert "timeout=30" not in downloader_source

    main_source = (REPO_ROOT / "src/common/ingest/wpc/main.py").read_text(encoding="utf-8")
    assert "timedelta(hours=previous_analysis_lookback_hours())" in main_source


def test_wpc_unknown_feature_code_falls_back_to_the_catalog_color():
    """The fallback was written twice, once per feature kind, before extraction."""
    from common.ingest.wpc.converter import create_front_feature

    recorded = _wpc_yaml()["wpc"]
    feature = create_front_feature([(35.0, -97.0), (36.0, -96.0)], "NOTATYPE")

    assert feature["properties"]["color"] == recorded["fallback_geojson_color"] == "#000000"
    assert feature["properties"]["name"] == "NOTATYPE"

    known = create_front_feature([(35.0, -97.0), (36.0, -96.0)], "COLD")
    assert known["properties"]["color"] == recorded["feature_types"]["COLD"]["color"]

    source = (REPO_ROOT / "src/common/ingest/wpc/converter.py").read_text(encoding="utf-8")
    assert '"#000000"' not in source
    assert source.count("_style_for(") == 3, "one helper, two call sites"


def test_wpc_owns_its_retention_window_and_verifies_tls():
    """WPC's 360 minutes is its own; inheriting filesystem.yaml's 60 would keep nothing.

    A 3-hourly product needs a window wider than an hour, so this is a genuinely
    separate owner rather than a duplicate of the generic cleanup default.

    `verify_tls` is pinned `true` by the schema. The downloader reads it anyway and
    raises on false, so loosening the schema fails loudly rather than quietly
    downgrading the transport.
    """
    from common.ingest.wpc.config import cleanup_max_age_minutes, verify_tls
    from util.file_config import cleanup_max_age_minutes as generic_cleanup_age

    recorded = _wpc_yaml()["wpc"]
    assert cleanup_max_age_minutes() == recorded["cleanup_max_age_minutes"] == 360
    assert generic_cleanup_age() == 60

    assert verify_tls() is True
    schema = json.loads(
        (REPO_ROOT / "config/schema/wpc.schema.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["wpc"]["properties"]["verify_tls"]["const"] is True

    source = (REPO_ROOT / "src/common/ingest/wpc/downloader.py").read_text(encoding="utf-8")
    assert "ssl.CERT_NONE" not in source
    assert "if not verify_tls():" in source
    assert source.count("ssl.CERT_REQUIRED") == 1, "the two contexts were deliberately merged"

    main_source = (REPO_ROOT / "src/common/ingest/wpc/main.py").read_text(encoding="utf-8")
    assert "timedelta(hours=3)" not in main_source
    assert "wpc_sfc_*.geojson" not in main_source


# --- METAR: the one subsystem that really does skip TLS verification -------

def _metar_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "metar.yaml").read_text(encoding="utf-8"))


def _metar_source() -> str:
    return (REPO_ROOT / "src/common/ingest/metar.py").read_text(encoding="utf-8")


def test_metar_tls_verifies_from_one_place():
    """RESOLVED: `verify_tls` is true, and one key reaches all five call sites.

    Five call sites disabled verification independently -- three aiohttp
    ``ssl=False`` and two ``ssl.CERT_NONE`` -- so flipping the policy meant
    finding all five. They now share two helpers, and the count assertions below
    are what stops a sixth site from quietly reintroducing its own downgrade.

    The switch stayed false until a run confirmed both hosts present chains that
    validate against the default trust store. It is asserted here rather than by
    reaching the network, so this test does not depend on connectivity or on a
    certificate that will eventually be reissued.
    """
    from common.ingest.metar_config import aiohttp_ssl, ssl_context, verify_tls

    recorded = _metar_yaml()["metar"]
    assert verify_tls() is recorded["verify_tls"] is True

    # Unlike wpc.verify_tls, the schema leaves this free -- it is a real switch,
    # so an operator behind an intercepting proxy can still turn it off.
    schema = json.loads(
        (REPO_ROOT / "config/schema/metar.schema.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["metar"]["properties"]["verify_tls"] == {"type": "boolean"}

    # The value actually reaches the transport, in both spellings.
    assert aiohttp_ssl() is not False
    context = ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True

    source = _metar_source()
    assert "CERT_NONE" not in source, "the five inline downgrades must stay collapsed"
    assert "ssl=False" not in source
    assert source.count("ssl=aiohttp_ssl()") == 3
    assert source.count("context=ssl_context()") == 2


def test_turning_metar_verify_tls_off_still_reaches_every_transport():
    """The escape hatch must work, and must be loud when used.

    Runs at ``False`` -- the opposite of the shipped value -- so the key cannot
    quietly become decorative. If a future edit hardcoded a verifying context,
    this fails while the test above still passes.

    The warning goes to the subsystem's IOManager, not ``warnings.warn``, so it
    lands in the same log stream as the rest of the ingest run. It is
    deliberately not deduplicated: an ingest run builds only a couple of
    contexts, and a single line at startup is easy to miss in a log.
    """
    from common.config import loader
    from common.ingest import metar_config

    def _not_verifying():
        return False

    original = metar_config.verify_tls
    metar_config.verify_tls = _not_verifying
    try:
        with mock.patch.object(metar_config, "_io") as recorder:
            assert metar_config.aiohttp_ssl() is False
            context = metar_config.ssl_context()
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False

        assert recorder.write_warning.call_count == 2, "every helper must warn"
        for call in recorder.write_warning.call_args_list:
            assert "metar.verify_tls is false" in call.args[0]
    finally:
        metar_config.verify_tls = original
        loader.reset_cache()


def test_metar_accept_encoding_still_excludes_brotli():
    """DECISION PRESERVED: `br` is omitted because brotli responses failed to decode.

    This header is narrower than the client default on purpose, which is easy to
    read as an oversight and "fix" by restoring the default. Both request paths
    send it, and neither handles a brotli body, so adding `br` back would break
    the station download rather than speed it up.
    """
    from common.ingest.metar_config import accept_encoding

    recorded = _metar_yaml()["metar"]
    assert accept_encoding() == recorded["accept_encoding"] == "gzip, deflate"

    encodings = {token.strip() for token in accept_encoding().split(",")}
    assert "br" not in encodings, "nothing in metar.py decompresses brotli"
    assert "gzip" in encodings, "the sync path branches on a gzip Content-Encoding"

    source = _metar_source()
    assert "gzip, deflate" not in source
    assert source.count("accept_encoding()") == 2


def test_metar_cycle_url_needs_the_hour_placeholder():
    """A pattern without `{hour}` formats to itself, so every cycle repeats one hour.

    `str.format` does not raise on an unused keyword, so this fails silently and
    produces three identical fetches. The schema requires the placeholder.
    """
    from common.ingest.metar import _cycle_url
    from common.ingest.metar_config import observation_url_pattern

    recorded = _metar_yaml()["metar"]
    assert observation_url_pattern() == recorded["observation_url_pattern"]
    assert "{hour}" in observation_url_pattern()

    assert _cycle_url("07") == (
        "https://tgftp.nws.noaa.gov/data/observations/metar/cycles/07Z.TXT"
    )
    assert _cycle_url("07") != _cycle_url("18")

    schema = json.loads(
        (REPO_ROOT / "config/schema/metar.schema.json").read_text(encoding="utf-8")
    )
    pattern = schema["properties"]["metar"]["properties"]["observation_url_pattern"]["pattern"]
    assert re.search(pattern, observation_url_pattern())

    source = _metar_source()
    assert "tgftp.nws.noaa.gov" not in source
    assert source.count("_cycle_url(hour_str)") == 3, "one definition, two callers"


def test_metar_keeps_two_timeouts_rather_than_one():
    """RESOLVED: 60 and 30 describe different requests and must not be unified.

    The station database is one large JSON document fetched once; the observation
    cycles are small hourly text files fetched three at a time. Both were bare
    literals repeated across five call sites.
    """
    from common.ingest.metar_config import (
        observation_timeout_seconds,
        station_timeout_seconds,
    )

    recorded = _metar_yaml()["metar"]
    assert station_timeout_seconds() == recorded["station_timeout_seconds"] == 60
    assert observation_timeout_seconds() == recorded["observation_timeout_seconds"] == 30
    assert station_timeout_seconds() != observation_timeout_seconds()

    source = _metar_source()
    assert source.count("timeout=station_timeout_seconds()") == 2
    # One for the sync fetch, one for the async `_read` the two async branches share.
    assert source.count("timeout=observation_timeout_seconds()") == 2


def test_metar_retention_is_its_own_key_despite_matching_the_generic_default():
    """DECISION PRESERVED: two 60s, two owners, so retuning one cannot move the other.

    `util.file.clean_old_files` already defaults the age to `filesystem.yaml`'s 60,
    so METAR could have passed nothing. It passes its own key instead: the equal
    value is a coincidence of tuning, not a shared policy. The file count cap is
    the opposite call -- METAR does inherit that one, which is why only the age is
    forwarded.
    """
    from common.ingest.metar_config import cleanup_max_age_minutes
    from util.file_config import cleanup_max_age_minutes as generic_cleanup_age
    from util.file_config import cleanup_max_files as generic_cleanup_files

    recorded = _metar_yaml()["metar"]
    assert cleanup_max_age_minutes() == recorded["cleanup_max_age_minutes"] == 60
    assert generic_cleanup_age() == 60
    assert generic_cleanup_files() == 10

    assert "cleanup_max_files" not in recorded

    source = _metar_source()
    assert source.count("max_age_minutes=cleanup_max_age_minutes()") == 2
    assert "max_files" not in source, "METAR takes the generic count cap"


def test_metar_lookback_drives_both_entry_points():
    """The sync and async ingests must cover the same span, and did so by two 3s."""
    from common.ingest.metar_config import lookback_hours

    recorded = _metar_yaml()["metar"]
    assert lookback_hours() == recorded["lookback_hours"] == 3

    source = _metar_source()
    assert source.count("range(lookback_hours())") == 2
    assert "range(3)" not in source


def test_metar_station_parsing_rounds_once_from_the_catalog():
    """The sync and async station downloads parsed the same payload twice.

    Both rounded to 4 decimals with their own literal, so the two caches could
    have diverged. They now share one parser.
    """
    from common.ingest.metar import _parse_station_entries
    from common.ingest.metar_config import coordinate_decimals

    recorded = _metar_yaml()["metar"]
    assert coordinate_decimals() == recorded["coordinate_decimals"] == 4

    parsed = _parse_station_entries(
        [
            {"icaoId": "KJFK", "lat": 40.63992345, "lon": -73.77869012},
            {"stationId": "KORD", "lat": 41.9, "lon": -87.9},
            {"icaoId": "KNOPE", "lat": None, "lon": 1.0},
            {"lat": 1.0, "lon": 1.0},
        ]
    )
    assert parsed == {"KJFK": [40.6399, -73.7787], "KORD": [41.9, -87.9]}

    source = _metar_source()
    assert source.count("_parse_station_entries(") == 3, "one definition, two callers"


def test_metar_pressure_rounding_is_config_but_the_encoding_is_not():
    """`/100` is the AXXXX altimeter encoding; only the rounding is a setting."""
    from common.ingest.metar import parse_metar
    from common.ingest.metar_config import pressure_decimals

    recorded = _metar_yaml()["metar"]
    assert pressure_decimals() == recorded["pressure_decimals"] == 2

    parsed = parse_metar("KJFK 121756Z 31009KT A3039", "2023/01/12 17:56")
    assert parsed["pressure"] == 30.39

    source = _metar_source()
    assert "alt_value / 100, pressure_decimals()" in source


def test_metar_conus_bounds_are_the_audits_corrected_numbers():
    """The audit recorded lon -125..-67; the code has always used -66.0.

    Also pins that the filter reads the catalog once per call rather than through
    the old `CONUS_BOUNDS` module constant.
    """
    from common.ingest import metar
    from common.ingest.metar_config import conus_bounds

    recorded = _metar_yaml()["metar"]["conus_bounds"]
    assert dict(conus_bounds()) == recorded
    assert recorded == {"lat_min": 24.0, "lat_max": 50.0, "lon_min": -125.0, "lon_max": -66.0}

    assert not hasattr(metar, "CONUS_BOUNDS")
    assert not hasattr(metar, "STATION_DB_URL")

    # A station between the audit's -67 and the real -66 is kept.
    with mock.patch.object(metar, "get_station_coordinates", return_value=[40.0, -66.5]):
        kept = metar.process_content("2023/01/12 17:56\nKXYZ 121756Z 31009KT A3039\n")
    assert [entry["station"] for entry in kept] == ["KXYZ"]


def test_metar_station_cache_path_follows_the_runtime_data_dir():
    """The filename was written twice and joined to `fs.DATA_DIR` at each site."""
    from common.ingest import metar
    from common.ingest.metar_config import station_cache_file, station_db_url

    recorded = _metar_yaml()["metar"]
    assert station_cache_file() == recorded["station_cache_file"] == "stations_cache.json"
    assert station_db_url() == recorded["station_db_url"]
    assert "aviationweather.gov" in station_db_url()

    import util.file as fs

    assert metar._station_cache_path() == fs.DATA_DIR / station_cache_file()

    source = _metar_source()
    assert "stations_cache.json" not in source
    assert source.count("_station_cache_path()") == 3, "one definition, two callers"


# --- Detection thresholds duplicated across eight declaration sites --------

DETECTION_DEFAULT_FILES = {
    "EdgeWARN/pipeline.py": 0,
    "EdgeWARN/process/detect/detect.py": 0,
    "EdgeWARN/process/detect/main.py": 0,
    "EdgeWARN/process/detect/tools/gatemapper.py": 0,
    "util/io.py": 0,  # Phase 1: the argparse flag now defaults to None and is filled from detection.yaml
}


def _declaration_sites(param: str, expected: float) -> dict[str, int]:
    """Count places that declare ``param=expected`` as a default.

    Counted via ``ast`` rather than substring search so that a prose mention
    such as gatemapper's "With min_seed_percentage=0.001, ..." comment does not
    inflate the total.
    """
    import ast

    from tests.architecture.source_inspect import SRC, _literal

    flag = f"--{param.replace('_', '-')}"
    found: dict[str, int] = {}
    for relative in DETECTION_DEFAULT_FILES:
        tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                positional = args.posonlyargs + args.args
                padding = len(positional) - len(args.defaults)
                pairs = [
                    (arg, args.defaults[i - padding])
                    for i, arg in enumerate(positional)
                    if i >= padding
                ]
                pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d]
                count += sum(1 for a, d in pairs if a.arg == param and _literal(d) == expected)
            elif isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Attribute) and function.attr == "add_argument":
                    flags = {_literal(a) for a in node.args}
                    keywords = {k.arg: k.value for k in node.keywords if k.arg}
                    if flag in flags and _literal(keywords.get("default", ast.Constant(None))) == expected:
                        count += 1
        found[relative] = count
    return found


@pytest.mark.parametrize(
    ("param", "expected"),
    [("refl_threshold", 37.5), ("min_seed_percentage", 0.001), ("drop_offset", 10.0)],
)
def test_detection_thresholds_are_declared_only_in_yaml(param, expected):
    """RESOLVED in Phase 4: `detection.yaml` is the only declaration site.

    Phase 1 removed the argparse default in `util/io.py`; Phase 4 removed the
    remaining seven keyword-argument declarations by threading one typed
    `DetectionConfig` from the entrypoint. Every intermediate function now takes
    the resolved object, so there is nowhere left for a stale copy of the literal
    to shadow the YAML.
    """
    sites = _declaration_sites(param, expected)
    assert sites == DETECTION_DEFAULT_FILES
    assert sum(sites.values()) == 0
    assert _detection_yaml()["detection"][param] == expected


def test_gatemapper_hard_floor_makes_a_raised_threshold_ineffective():
    """RESOLVED in Phase 4: every term of the curve is read from YAML.

    The cap itself is unchanged and still a cap -- `baseline_refl_floor` is
    applied as `min(floor, refl_threshold)`, so raising `--refl-threshold` above
    the floor still cannot raise the baseline mask. What changed is that the
    floor is now editable, and the five terms of the per-cell threshold curve
    are named keys rather than literals buried in two expressions. They remain
    *coupled*: moving one shifts the whole curve.
    """
    source = (REPO_ROOT / "src/EdgeWARN/process/detect/tools/gatemapper.py").read_text(encoding="utf-8")
    assert "min(self.gm.baseline_refl_floor, self.refl_threshold)" in source

    # No term of the curve survives as a literal in the module.
    for literal in ("37.5", "40.0", "45.0", "52.0"):
        assert literal not in source

    gatemapper = _detection_yaml()["gatemapper"]
    assert gatemapper["baseline_refl_floor"] == 37.5
    assert gatemapper["dynamic_min_threshold"] == {
        "switch_max_refl": 45.0,
        "low": 37.5,
        "high": 40.0,
    }
    assert gatemapper["max_refl_clamp"] == 52.0


# --- Divergent user agents ------------------------------------------------

def test_one_user_agent_template_interpolates_the_package_version():
    """RESOLVED (Phase 5): three strings across five sites became one template.

    The template shape is hardcoded in `util/release.py format_user_agent()`,
    with the contact half resolved from `runtime.yaml identity`. `{version}` is
    filled from package.json, so the advertised version tracks the release
    instead of being copied into source.
    """
    from util.release import format_user_agent

    package_version = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))["version"]

    resolved = format_user_agent()
    assert resolved == f"(EdgeWARN/{package_version}, ewsbackend@gmail.com)"

    # No subsystem keeps a copy: not in YAML...
    for name in ("nws", "nexrad", "metar"):
        recorded = (REPO_ROOT / f"config/{name}.yaml").read_text(encoding="utf-8")
        assert "user_agent:" not in recorded

    # ...and not as a literal in the five sites that send it.
    for relative in (
        "src/common/ingest/nws/zone_sync.py",
        "src/common/ingest/nws/main.py",
        "src/common/ingest/nexrad/config.py",
        "src/common/ingest/nexrad/weather_api.py",
        "src/common/ingest/metar.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "EdgeWARN/1.0" not in source
        assert package_version not in source


def test_every_outbound_user_agent_resolves_through_release():
    """The header is built at request time, so no site can pin a stale string."""
    from pathlib import Path

    from common.ingest.nexrad.weather_api import RadarStationCatalog
    from common.ingest.nws.zone_sync import NWSZoneSync
    from util.release import format_user_agent

    expected = format_user_agent()
    assert NWSZoneSync(Path(".")).headers["User-Agent"] == expected
    assert RadarStationCatalog()._headers()["User-Agent"] == expected

    # The NEXRAD module constant is gone, not merely unused.
    from common.ingest.nexrad import config as nexrad_config

    assert not hasattr(nexrad_config, "WEATHER_API_USER_AGENT")


def test_no_metar_request_path_goes_out_anonymous():
    """RESOLVED (Phase 5): two of METAR's five request paths sent no User-Agent.

    `metar.yaml` claimed all of them identified themselves. In fact the sync
    observation fetch passed a bare URL string to `urlopen` -- so urllib sent its
    own `Python-urllib/3.x` -- and the async fallback session built a
    `ClientSession` with a connector but no headers, sending aiohttp's default.
    Aviation Weather asks callers to identify themselves with a contact address,
    so both were a courtesy violation against a public service, and neither was
    visible from the catalog.

    The two repaired paths are asserted behaviourally rather than by reading the
    source: a header dict that is built and then not passed to the client would
    satisfy any AST check while still going out anonymous.
    """
    import asyncio
    import urllib.request

    from common.ingest import metar
    from util.release import format_user_agent

    expected = format_user_agent()
    dt = datetime(2026, 8, 15, 7, tzinfo=timezone.utc)

    # Path 3 of 5: sync observation fetch, urllib.
    captured: list = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b""

    def _fake_urlopen(request, *args, **kwargs):
        captured.append(request)
        return _FakeResponse()

    with mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
        metar.fetch_metar_cycle(dt)

    assert len(captured) == 1
    assert captured[0].get_header("User-agent") == expected
    # Deliberately absent: urllib would hand the raw gzip to .decode('utf-8').
    assert captured[0].get_header("Accept-encoding") is None

    # Path 4 of 5: the async fallback session, taken only when no caller supplies
    # one. Recorded at construction; the fetch itself is allowed to fail.
    sessions: list = []

    class _RecordingSession:
        def __init__(self, *args, **kwargs):
            sessions.append(kwargs.get("headers"))
            raise RuntimeError("no network in tests")

    with mock.patch("aiohttp.ClientSession", _RecordingSession):
        assert asyncio.run(metar.fetch_metar_cycle_async(dt)) is None

    assert sessions == [{"User-Agent": expected}]

    # Completeness guard for the other three: the two station-database fetches
    # and the shared async observation session. Five resolutions, one per path --
    # dropping any header drops the count.
    metar_source = (REPO_ROOT / "src/common/ingest/metar.py").read_text(encoding="utf-8")
    assert metar_source.count("format_user_agent()") == 5


# --- zone_sync pause_seconds: flag default fights constructor default ------

def test_zone_sync_pause_seconds_agrees_with_the_constructor_and_throttles():
    """RESOLVED (Phase 5): YAML now records 0.05, matching the constructor.

    Phase 1 wired the overlay but recorded the effective 0.0, which disabled
    throttling against api.weather.gov. The pause was also in the `as_completed`
    collection loop, downstream of every submitted future, so even a non-zero
    value could not slow the API down. It now runs in the worker.

    The constructor no longer carries its own 0.05 for the file to agree with --
    it resolves the recorded value, so the two cannot disagree. What is checked
    here is that the recorded value is non-zero and that the sleep still gates
    the request rather than the bookkeeping.
    """
    from tests.architecture.source_inspect import argparse_defaults

    assert param_default(
        "common/ingest/nws/zone_sync.py", "NWSZoneSync.__init__", "pause_seconds"
    ) is None
    assert argparse_defaults("common/ingest/nws/zone_sync.py")["--pause-seconds"]["default"] is None

    source = (REPO_ROOT / "src/common/ingest/nws/zone_sync.py").read_text(encoding="utf-8")
    assert "pause_seconds=args.pause_seconds" in source

    # The sleep gates the request, not the result bookkeeping.
    worker = source.index("def _fetch_missing_zone")
    collect = source.index("for future in as_completed(futures):")
    assert worker < source.index("time.sleep(self.pause_seconds") < collect

    import yaml

    from common.ingest.nws.zone_sync import NWSZoneSync

    recorded = yaml.safe_load((REPO_ROOT / "config/nws.yaml").read_text(encoding="utf-8"))
    assert recorded["zone_sync"]["pause_seconds"] > 0, "throttling is disabled"
    assert NWSZoneSync(Path(".")).pause_seconds == recorded["zone_sync"]["pause_seconds"]


# --- NEXRAD sites sentinel ------------------------------------------------

def test_nexrad_all_sites_sentinel_is_null_not_an_empty_list():
    """RESOLVED (Phase 3): the sentinel is `null`, matching the code's `None`.

    `pipeline/__init__.py:76` keeps `None` as-is and `station_filter.py:16` then
    applies no site filter, so `null` means "every allowed station". The file
    used to record `[]`, which would have filtered every site away.
    """
    source = (REPO_ROOT / "src/common/ingest/nexrad/pipeline/__init__.py").read_text(encoding="utf-8")
    assert "None if sites is None else" in source

    import yaml

    recorded = yaml.safe_load((REPO_ROOT / "config/nexrad.yaml").read_text(encoding="utf-8"))
    assert recorded["cli"]["sites"] is None


def test_nexrad_cli_declares_no_volume_count_it_cannot_use():
    """RESOLVED (Phase 5): `cli.max_volumes_per_site` and its flag are gone.

    Both were inert. `_resolve_cli_args` overlaid the flag onto the key and
    `main()` never read the result: the `--volume-id` branch calls
    `ingest_allowed_vcp_volume_async`, which takes one volume id and has no such
    parameter, and the default branch calls
    `NexradScanCoordinator.ingest_latest_station_scans_async`, which has none
    either.

    Deleted rather than threaded through, because the only functions honouring it
    (`ingest_latest_allowed_vcp_scans` and its async twin) have no callers outside
    the public re-export and tests. Wiring it would have made a dead path
    configurable -- an operator would edit the key, observe nothing, and be right.
    """
    import yaml

    recorded = yaml.safe_load((REPO_ROOT / "config/nexrad.yaml").read_text(encoding="utf-8"))
    assert "max_volumes_per_site" not in recorded["cli"]

    from common.ingest.nexrad.main import _build_parser

    flags = {action.option_strings[0] for action in _build_parser()._actions if action.option_strings}
    assert "--max-volumes-per-site" not in flags
    # The sibling that is genuinely read stays, and still defaults to the catalog.
    assert "--max-candidate-volumes-per-site" in flags

    # The library defaults survive: with no key claiming them they are not copies.
    for qualname in (
        "ingest_latest_allowed_vcp_scans",
        "ingest_latest_allowed_vcp_scans_async",
    ):
        assert param_default(
            "common/ingest/nexrad/main.py", qualname, "max_volumes_per_site"
        ) == 1


# --- RAP colormap authority drift ----------------------------------------

def test_mappings_json_carries_one_layer_with_no_python_producer():
    """DECISION OWED: delete the orphan, or add a producer.

    `mappings.json` is otherwise exactly `_with_colormap_key` applied to the
    43-layer RAP catalog, so the drift is a single stale entry.
    """
    from EWMRS.rap.config import get_rap_uint16_layers

    mappings = json.loads((REPO_ROOT / "src/EWMRS/mappings.json").read_text(encoding="utf-8"))
    produced = {layer["name"] for layer in get_rap_uint16_layers()}

    assert sorted(set(mappings) - produced) == ["RAP_BestLiftedIndex_180_0mbAGL"]
    assert produced - set(mappings) == set()
    assert len(mappings) == 44


def test_mappings_json_agrees_with_the_python_colormap_matcher():
    """Everything except the orphan is derivable, so it is a duplicate authority."""
    from EWMRS.rap.config import get_rap_uint16_layers

    mappings = json.loads((REPO_ROOT / "src/EWMRS/mappings.json").read_text(encoding="utf-8"))
    for layer in get_rap_uint16_layers():
        assert mappings[layer["name"]] == layer.get("colormap_key")


def test_every_configured_colormap_key_exists_in_colormaps_json():
    from EWMRS.rap.config import get_rap_uint16_layers
    from EWMRS.render.config import get_file_list

    document = json.loads((REPO_ROOT / "src/EWMRS/colormaps.json").read_text(encoding="utf-8"))
    available = {entry["name"] for entry in document[0]["colormaps"]}

    used = {layer["colormap_key"] for layer in get_file_list()}
    used |= {l["colormap_key"] for l in get_rap_uint16_layers() if "colormap_key" in l}

    assert used - available == set()


def test_reflectance_colormap_ternary_is_resolved():
    """RESOLVED: the ternary was dropped when the layers moved to the catalog.

    `get_goes_file_list()` used to compute `colormap_key` with a ternary whose
    `else` arm (`GOES_ABI_C07_Reflectance` and similar) was unreachable, because
    `reflectance_specs` held only C01-C06. Each layer now names its colormap
    outright in ewmrs_render.yaml, so there is no dead branch to transcribe.
    """
    from EWMRS.render.config import get_goes_file_list

    source = (REPO_ROOT / "src/EWMRS/render/config.py").read_text(encoding="utf-8")
    assert "reflectance_specs" not in source
    assert "GOES_ABI_{channel_id}_Reflectance" not in source

    reflectance = [l for l in get_goes_file_list() if l["name"].endswith("_Reflectance")]
    assert len(reflectance) == 6
    assert [l["channel_id"] for l in reflectance] == ["C01", "C02", "C03", "C04", "C05", "C06"]
    assert {l["colormap_key"] for l in reflectance} == {"GOES_RGB_Raw"}


# --- Import-time binding hazards -----------------------------------------

def test_render_config_no_longer_snapshots_the_file_list_at_import_time():
    """RESOLVED: the `file_list = get_file_list()` module-scope snapshot is gone.

    It froze two things at import, both before an entry point could speak: the
    config root, because `get_file_list` loads `ewmrs_render.yaml` and `run.py`
    imports this package before `get_args()` exports `EDGEWARN_CONFIG_DIR`; and
    every `filepath`/`outdir` path, because `_resolve_dir` reads them off
    `util.file` and `--base_dir` had not been applied yet.

    Deleted rather than deferred. Its comment offered it "for backward
    compatibility", but nothing in `src/` read it -- `EWMRS/pipeline.py` imports
    the three accessors and calls them per use.
    """
    from EWMRS.render import config as render_config

    assert not hasattr(render_config, "file_list")
    source = (REPO_ROOT / "src/EWMRS/render/config.py").read_text(encoding="utf-8")
    assert "\nfile_list = " not in source


def test_render_chunk_format_resolves_after_config_root_selection(tmp_path):
    """`run.py` imports EWMRS before `get_args()`, so chunk settings stay lazy."""
    from EWMRS.render import config as render_config

    config_dir = _config_dir_with_overrides(
        tmp_path, "ewmrs_render", indent="  ", tile_size="175"
    )
    with _with_config_root(config_dir):
        assert render_config.tile_size() == 175


def test_util_file_binds_paths_at_import_but_already_knows_base_dir():
    """RESOLVED as far as it can be: the bind stays, the value is now correct.

    `_define_paths()` still runs at module scope, and deliberately so -- 36
    modules read the 113 globals as `fs.<NAME>` attributes, so making them lazy
    is a 250-site change with no behavioral gain. What was actually broken is
    that the value bound was always the platform default, leaving a window in
    which anything reading a path between this import and the entry point's
    `initialize_filesystem` call got the wrong directory.

    `IOManager.get_base_dir_arg()` closes it by answering `--base_dir` from
    `sys.argv` before the real parser exists. `initialize_filesystem` is still
    phase two: a spawned child has no argv, and programmatic callers pass a
    directory directly.
    """
    source = (REPO_ROOT / "src/util/file.py").read_text(encoding="utf-8")

    assert "Path(IOManager.get_base_dir_arg())" in source
    # The shared resolver now supplies both the platform default and environment
    # compatibility overrides before the module-scope bind.
    tail = source.split("def initialize_filesystem")[1]
    assert "_platform_base_dir" not in source
    assert tail.count("_define_paths(") == 2


def test_base_dir_argv_resolution_beats_the_platform_default(monkeypatch, tmp_path):
    """The two-phase resolution has to work at import, not just in principle.

    Re-imported in a subprocess because `util.file` binds at module scope and
    this process already did it; `importlib.reload` would not re-run the
    `sys.argv` read under a different argv in a way that proves the entry-point
    ordering.
    """
    import subprocess
    import sys

    script = (
        "import sys; sys.path.insert(0, r'{src}')\n"
        "import util.file as fs\n"
        "print(fs.BASE_DIR)\n"
    ).format(src=REPO_ROOT / "src")

    chosen = tmp_path / "elsewhere"
    for flag in ("--base_dir", "--base-dir"):
        result = subprocess.run(
            [sys.executable, "-c", script, flag, str(chosen)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(chosen), f"{flag} did not reach the import-time bind"

    bare = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert bare.returncode == 0, bare.stderr
    assert bare.stdout.strip() != str(chosen)


def test_colormap_json_resolution_no_longer_depends_on_the_working_directory():
    """RESOLVED: the candidate list moved to filesystem.yaml, tokenized.

    `Path.cwd() / "colormaps.json"` led the list, so which file the renderer drew
    with depended on the directory the operator launched from -- and silently, since
    a colormaps.json anywhere on the launch path simply won. It is not carried over:
    `src/EWMRS/colormaps.json` is the shipped file and the `<src_dir>` candidate
    that replaces it resolves against the installed tree instead.

    The two surviving candidates are tokens rather than absolute paths because
    neither location is knowable when the catalog is written: `<gui_dir>` follows
    `--base_dir`, and `<src_dir>` is wherever the tree was installed.
    """
    from common.config.loader import load_config

    source = (REPO_ROOT / "src/util/file.py").read_text(encoding="utf-8")
    assert 'Path.cwd() / "colormaps.json"' not in source
    assert '"EWMRS" / "colormaps.json"' not in source
    assert "colormap_search_path(" in source, "the candidate list belongs to the catalog now"

    assert list(load_config("filesystem")["colormap_search_path"]) == [
        "<src_dir>/EWMRS/colormaps.json",
        "<gui_dir>/colormaps.json",
    ]

    # `EWMRS/pipeline.py` overwrote the resolved path with its own `__file__`-relative
    # literal at module scope, which would have silently outranked the catalog for
    # every EWMRS run. It resolved to the same file, so deleting it is not a
    # behavior change -- it is what leaves the catalog as the only owner.
    pipeline = (REPO_ROOT / "src/EWMRS/pipeline.py").read_text(encoding="utf-8")
    assert "fs.GUI_COLORMAP_JSON =" not in pipeline
    assert "EWMRS_COLORMAP_JSON" not in pipeline


def test_the_colormap_candidates_still_find_the_shipped_file():
    """The tokens have to land on the same file the `__file__` candidate used to.

    Asserted against the shipped path directly rather than against `fs`, because
    the point is that the expansion agrees with the repository layout -- a
    `<src_dir>` that resolved one directory too high would still produce a plausible
    path and only fail at open time.
    """
    import util.file as fs

    assert fs.GUI_COLORMAP_JSON == (REPO_ROOT / "src/EWMRS/colormaps.json").resolve()
    assert fs.GUI_COLORMAP_JSON.is_file()


def test_run_module_scope_is_outside_a_main_guard():
    """RESOLVED (Phase 1): parsing lives inside ``main()``, not module scope.

    The original hazard was module-scope argument parsing re-executing under
    Windows ``spawn`` in every child process. ``run.py`` is now a thin alias
    over ``run_edgewarn.py`` with no parsing or side effects at all; the
    live contract is enforced by ``tests/unit/test_entrypoint_import_safety``.
    """
    source = (REPO_ROOT / "src/run.py").read_text(encoding="utf-8")
    guard_index = source.find('if __name__ == "__main__"')
    assert guard_index != -1
    assert "get_args()" not in source


# --- Silent fallbacks ----------------------------------------------------

def test_unknown_rap_transform_name_is_rejected():
    """RESOLVED (Phase 3): an unknown transform name now aborts the run.

    The silent `TRANSFORMS.get(name, lambda x: x)` fallback meant a typo
    published raw Kelvin under a Celsius key. Names are resolved once, before
    extraction, so the failure is early and names the offending product.
    """
    from EdgeWARN.process.integrate.integrate_rap import TRANSFORMS, _transform_for

    source = (REPO_ROOT / "src/EdgeWARN/process/integrate/integrate_rap.py").read_text(encoding="utf-8")
    assert 'TRANSFORMS.get(' not in source

    assert _transform_for({"key": "x"})(5.0) == 5.0
    assert _transform_for({"key": "x", "transform": "kelvin_to_celsius"})(273.15) == 0.0

    with pytest.raises(ValueError, match="unknown RAP transform 'kelvin_to_farenheit'"):
        _transform_for({"key": "temp_2m", "transform": "kelvin_to_farenheit"})

    assert set(TRANSFORMS) == {"kelvin_to_celsius"}


def test_unparseable_derived_formula_aborts_before_extraction():
    """RESOLVED (Phase 4): a bad formula is a config error, not per-cell data.

    Formulas now come from `integration.yaml`, and an unparseable one used to be
    caught inside the per-field loop and written to every cell as None -- which
    an operator could not tell apart from a cell that had no input value. Each
    formula is parsed and grammar-checked once, before extraction.

    Missing *inputs* still null the field per cell: that is a data condition, and
    the check substitutes a stand-in number for every name so it stays out of it.
    """
    from EdgeWARN.process.integrate.integrate_rap import _calculate_derived, _compile_derived

    key, expression = _compile_derived({"key": "depression", "formula": "temp_2m - dewpoint_2m"})
    assert key == "depression"

    with pytest.raises(ValueError, match="does not parse"):
        _compile_derived({"key": "typo", "formula": "temp_2m -"})

    with pytest.raises(ValueError, match="is not evaluable"):
        _compile_derived({"key": "unsafe", "formula": "__import__('os').getcwd()"})

    cells = [{"properties": {"temp_2m": 20.0}}]
    _calculate_derived(cells, expression, key, 2)
    assert cells[0]["properties"]["depression"] is None


def test_integration_output_rounding_reads_output_decimals():
    """RESOLVED (Phase 4): `integration.yaml output.decimals` is now live.

    The same 2 was hardcoded at three separate rounding sites, so an operator
    changing precision had to find all three. They now share one YAML value.
    The AzShear `round(x, 3)` / `np.round(x, 4)` sites are deliberately excluded:
    those are fixed feature-vector precisions, not an output-formatting choice.
    """
    from EdgeWARN.process.integrate.config import output_decimals

    assert output_decimals() == 2

    for relative_path in (
        "src/EdgeWARN/process/integrate/integrate_rap.py",
        "src/EdgeWARN/process/integrate/core/stats.py",
        "src/EdgeWARN/process/integrate/core/integrator.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "decimals" in source, relative_path
        assert ", 2)" not in source, relative_path


# --- NEXRAD concurrency: one value, two declarations ----------------------

def test_max_chunk_downloads_has_a_single_owner_in_nexrad_yaml():
    """RESOLVED (Phase 5): 64 was declared by the service *and* the CLI.

    The plan attributed the default to `s3_chunks.py`, which never held it; it
    lived in `NexradIngestService.__init__` and was restated by `main.py`, so a
    catalog key had to displace both. Both now pass None and the service
    resolves from `nexrad.yaml`.

    The defaults must stay None rather than calling the accessor inline:
    `run.py` imports this package before `get_args()` exports
    EDGEWARN_CONFIG_DIR, and a signature default binds at import time.
    """
    from common.ingest.nexrad import config as nexrad_config

    assert nexrad_config.max_chunk_downloads() == 64

    for module, qualname in (
        ("common/ingest/nexrad/service.py", "NexradIngestService.__init__"),
        ("common/ingest/nexrad/main.py", "NexradIngestService.__init__"),
    ):
        assert param_default(module, qualname, "max_chunk_downloads") is None, module

    for relative_path in (
        "src/common/ingest/nexrad/service.py",
        "src/common/ingest/nexrad/main.py",
        "src/common/ingest/nexrad/s3_chunks.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "max_chunk_downloads=64" not in source, relative_path


@pytest.mark.parametrize(
    "module_name",
    ["common.ingest.nexrad.main", "common.ingest.nexrad.pipeline"],
)
def test_nexrad_entry_points_export_the_config_root_for_parse_workers(
    module_name, tmp_path, monkeypatch
):
    """RESOLVED (Phase 5): `--config-dir` never reached the parse workers.

    Both NEXRAD entry points honoured the flag by passing `config_dir=` to
    `load_config`, which configures the *parent* only. Parse workers are
    ProcessPoolExecutor children that receive no config in their submit payload
    (see `worker_pool._worker_parse`), so their sole channel is the inherited
    environment. Before this fix the parent read the override while its workers
    walked up to the repo default -- a split-brain config, not an error.

    Asserted behaviourally rather than by grepping for the call, so that moving
    or renaming it cannot pass while the export silently stops happening.

    The Namespace comes from the module's own parser rather than being assembled
    by hand: every flag the entry point defines is then present with its real
    default, so adding one cannot break this test with an AttributeError that
    says nothing about the export.
    """
    import importlib
    import shutil

    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    config_dir = tmp_path / "config"

    monkeypatch.delenv("EDGEWARN_CONFIG_DIR", raising=False)
    module = importlib.import_module(module_name)
    args = module._build_parser().parse_args(["--config-dir", str(config_dir)])

    module._resolve_cli_args(args)

    from common.config import loader

    assert os.environ.get("EDGEWARN_CONFIG_DIR") == str(config_dir)
    # The no-argument path is the one a spawned worker takes.
    assert loader.config_root() == config_dir
    loader.reset_cache()


# --- EWMRS pipeline: three owners of 120, and one parameter nobody reads ---

def _ewmrs_pipeline_yaml() -> dict:
    import yaml

    return yaml.safe_load(
        (REPO_ROOT / "config" / "ewmrs_pipeline.yaml").read_text(encoding="utf-8")
    )


def _ewmrs_pipeline_source() -> str:
    return (REPO_ROOT / "src/EWMRS/pipeline.py").read_text(encoding="utf-8")


def _param_loads(relative_path: str, qualname: str, param: str) -> int:
    """Count reads of ``param`` inside a function body.

    The signature declares the parameter as an ``ast.arg``, not a ``Name``, so a
    count of zero means the function accepts the parameter and never uses it.
    """
    import ast

    from tests.architecture.source_inspect import SRC, _find_function

    function = _find_function(
        ast.parse((SRC / relative_path).read_text(encoding="utf-8")), qualname
    )
    return sum(
        1
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id == param and isinstance(node.ctx, ast.Load)
    )


EWMRS_MAX_ENTRIES_SITES = (
    "run_render_pipeline",
    "run_mrms_render_pipeline",
    "run_goes_render_pipeline",
    "ewmrs_goes_worker",
)


def test_ewmrs_max_entries_has_no_catalog_copy_because_nothing_reads_it():
    """RESOLVED: `ewmrs_pipeline.yaml` deliberately omits `render.max_entries`.

    Five EWMRS functions declared `max_entries: int = 10`, which looked like a
    tunable with five duplicate defaults. It is not a tunable at all:
    `run_render_pipeline` is where the chain terminates and it never reads the
    value, because layer selection comes from the render catalogs rather than a
    count. The parameter survives only because it rides in the GOES render task
    tuple that `cycle.py` queues and `background.py` unpacks positionally.

    So the single owner is `runtime.yaml goes_coordination.render_task_max_entries`
    and a copy in `ewmrs_pipeline.yaml` would be a second owner of a dead value.
    The five declarations are `None` so no literal shadows that owner either.
    """
    from util.runtime.config import section

    recorded = _ewmrs_pipeline_yaml()
    assert "max_entries" not in recorded["render"]

    for qualname in EWMRS_MAX_ENTRIES_SITES:
        assert param_default("EWMRS/pipeline.py", qualname, "max_entries") is None, qualname

    assert _param_loads("EWMRS/pipeline.py", "run_render_pipeline", "max_entries") == 0
    assert section("goes_coordination")["render_task_max_entries"] == 10


def test_ewmrs_declaration_defaults_all_defer_to_the_catalog():
    """RESOLVED: every EWMRS default that shadowed a catalog key is now `None`.

    Listed as a sweep so adding a sixth caller with its own literal fails here
    rather than at the next audit.
    """
    for qualname, param in (
        ("run_render_pipeline", "phase_name"),
        ("run_render_pipeline", "cleanup_after"),
        ("cleanup_old_gui_files", "max_age_minutes"),
        ("_maybe_cleanup_goes_gui_files", "max_age_minutes"),
    ):
        assert param_default("EWMRS/pipeline.py", qualname, param) is None, (qualname, param)

    # Decomposition Phase 3 moved the NEXRAD GUI loop into its own service
    # package; its declaration defaults must keep deferring to the catalog.
    for relative_path, qualname, param in (
        ("NEXRAD/gui_pipeline.py", "cleanup_old_nexrad_gui_files", "max_age_minutes"),
        ("NEXRAD/gui_pipeline.py", "render_pending_nexrad_gui_files", "max_source_age_minutes"),
        ("NEXRAD/gui_pipeline.py", "run_nexrad_render_loop", "poll_interval_seconds"),
    ):
        assert param_default(relative_path, qualname, param) is None, (qualname, param)


def test_ewmrs_three_owners_of_120_stay_three_keys():
    """RESOLVED: 120 appeared at five sites meaning three different things.

    GUI output retention, GOES output retention, and NEXRAD *input* freshness all
    read 120, and two of them were restated at call sites. Collapsing them into
    one key would have coupled a retention sweep to a freshness window, so they
    remain three keys that happen to agree today. The literal is gone from the
    module, which is what stops them drifting back into one number.
    """
    from EWMRS.pipeline_config import (
        goes_cleanup_max_age_minutes,
        gui_cleanup_max_age_minutes,
        nexrad_source_max_age_minutes,
    )

    recorded = _ewmrs_pipeline_yaml()
    assert gui_cleanup_max_age_minutes() == recorded["render"]["gui_cleanup_max_age_minutes"] == 120
    assert goes_cleanup_max_age_minutes() == recorded["render"]["goes_cleanup_max_age_minutes"] == 120
    assert nexrad_source_max_age_minutes() == recorded["nexrad_gui"]["retention_minutes"] == 120

    source = _ewmrs_pipeline_source()
    assert "max_age_minutes=120" not in source
    assert "= 120" not in source


def test_ewmrs_goes_cleanup_interval_is_read_per_call_not_frozen_at_import():
    """RESOLVED: the rate limit was an import-time `os.environ` read.

    `_GOES_CLEANUP_MIN_INTERVAL_SECONDS` was computed at module scope, so a
    render worker that imported the module before the environment was arranged
    kept the wrong interval for its whole life. It is now read per sweep.

    The zero floor is kept in the accessor: a negative interval would make the
    elapsed-time comparison always true and disable cleanup entirely.
    """
    from EWMRS.pipeline_config import (
        GOES_CLEANUP_MIN_INTERVAL_ENV,
        goes_cleanup_min_interval_seconds,
    )

    recorded = _ewmrs_pipeline_yaml()
    assert (
        goes_cleanup_min_interval_seconds()
        == recorded["render"]["goes_cleanup_min_interval_seconds"]
        == 300
    )

    source = _ewmrs_pipeline_source()
    assert "_GOES_CLEANUP_MIN_INTERVAL_SECONDS" not in source
    assert "goes_cleanup_min_interval_seconds()" in source

    with mock.patch.dict("os.environ", {GOES_CLEANUP_MIN_INTERVAL_ENV: "45"}):
        assert goes_cleanup_min_interval_seconds() == 45.0
    with mock.patch.dict("os.environ", {GOES_CLEANUP_MIN_INTERVAL_ENV: "-5"}):
        assert goes_cleanup_min_interval_seconds() == 0.0
    assert goes_cleanup_min_interval_seconds() == 300.0


def test_ewmrs_worker_budget_is_coupled_to_the_goes_phase_name():
    """DECISION PRESERVED: the memory budget is selected by a phase-name prefix.

    `worker_budget_mb` dispatches on `phase_name.upper().startswith("GOES")`, so
    the phase label is not a free-form string -- renaming the GOES phase would
    silently halve its budget. `render.phase_name` is pinned here alongside the
    budgets to make that coupling fail loudly rather than quietly.
    """
    from EWMRS.pipeline_config import render_phase_name, worker_budget_mb

    recorded = _ewmrs_pipeline_yaml()
    assert render_phase_name() == recorded["render"]["phase_name"] == "EWMRS"
    assert not render_phase_name().upper().startswith("GOES")

    assert worker_budget_mb("GOES") == recorded["workers"]["budget_mb"]["goes"] == 1200.0
    assert worker_budget_mb("MRMS") == recorded["workers"]["budget_mb"]["default"] == 768.0
    assert worker_budget_mb(render_phase_name()) == 768.0

    source = _ewmrs_pipeline_source()
    assert "1200.0" not in source
    assert "768.0" not in source


def test_ewmrs_worker_memory_env_vars_still_outrank_the_catalog():
    """RESOLVED: both budgets kept their environment overrides.

    One variable covers the GOES and default budgets, which means setting it
    flattens the distinction the catalog draws. That is the pre-extraction
    behavior and is preserved rather than split, because splitting it would
    invent a variable no deployment sets.
    """
    from EWMRS.pipeline_config import (
        WORKER_BUDGET_MB_ENV,
        WORKER_RESERVE_MB_ENV,
        worker_budget_mb,
        worker_psutil_fallback_max,
        worker_reserve_mb,
    )

    recorded = _ewmrs_pipeline_yaml()
    assert worker_reserve_mb() == recorded["workers"]["reserve_mb"] == 1024.0
    assert worker_psutil_fallback_max() == recorded["workers"]["psutil_fallback_max"] == 2

    with mock.patch.dict("os.environ", {WORKER_BUDGET_MB_ENV: "256.5"}):
        assert worker_budget_mb("GOES") == 256.5
        assert worker_budget_mb("MRMS") == 256.5
    with mock.patch.dict("os.environ", {WORKER_RESERVE_MB_ENV: "64"}):
        assert worker_reserve_mb() == 64.0

    # 1024.0 is not asserted absent: the bytes-to-MiB conversion legitimately
    # uses it twice, and that arithmetic is not a tunable.
    source = _ewmrs_pipeline_source()
    assert "worker_reserve_mb()" in source
    assert "worker_psutil_fallback_max()" in source
    assert "EWMRS_WORKER" not in source


def test_ewmrs_numeric_thread_caps_are_one_value_across_a_fixed_list():
    """RESOLVED: four BLAS-family variables share one cap, set to 1.

    The catalog cannot express per-variable caps because the code sets them in a
    single loop, so the list and the value are separate keys rather than a map.
    The cap is 1 so the process pool owns the parallelism; letting each worker
    thread out would oversubscribe every core.
    """
    import os as _os

    from EWMRS.pipeline import _configure_numerical_thread_caps
    from EWMRS.pipeline_config import numeric_thread_cap_value, numeric_thread_cap_variables

    recorded = _ewmrs_pipeline_yaml()["workers"]["numeric_thread_caps"]
    assert numeric_thread_cap_value() == recorded["value"] == 1
    assert list(numeric_thread_cap_variables()) == recorded["variables"]
    assert numeric_thread_cap_variables() == (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    )

    with mock.patch.dict("os.environ", {}, clear=True):
        _configure_numerical_thread_caps()
        for variable in numeric_thread_cap_variables():
            assert _os.environ[variable] == str(numeric_thread_cap_value())

    source = _ewmrs_pipeline_source()
    assert "OMP_NUM_THREADS" not in source


def test_ewmrs_tile_threads_env_bypasses_the_cpu_cap_but_the_catalog_does_not():
    """DECISION PRESERVED: the override and the catalog value are not equivalent.

    `EWMRS_TILE_THREADS` returns `min(tile_count, cap)`, skipping the CPU count;
    the catalog value goes through `min(tile_count, cap, cpu_count)`. Routing
    both through `overlay.resolve` would have been tidier and would have changed
    behavior on any machine with fewer than 8 cores, so the two branches stay.
    """
    import os as _os

    from EWMRS.pipeline_config import TILE_THREADS_ENV, max_tile_threads
    from EWMRS.render.render import _resolve_tile_workers

    recorded = _ewmrs_pipeline_yaml()
    assert max_tile_threads() == recorded["render_threads"]["max_tile_threads"] == 8

    cpu_count = max(1, _os.cpu_count() or 1)
    with mock.patch.dict("os.environ", {}, clear=True):
        assert _resolve_tile_workers(1000) == min(8, cpu_count)
    with mock.patch.dict("os.environ", {TILE_THREADS_ENV: "1000"}):
        assert _resolve_tile_workers(1000) == 1000

    render_source = (REPO_ROOT / "src/EWMRS/render/render.py").read_text(encoding="utf-8")
    assert "min(tile_count, max_tile_threads(), cpu_cap)" in render_source


def test_ewmrs_lru_cache_sizes_come_from_the_catalog_at_import(tmp_path):
    """DECISION PRESERVED: two keys are read once at import, not per call.

    `maxsize` is a decorator argument, so Python evaluates it when the module
    loads and a cache cannot be resized per call. Every other key in this file is
    read per use; these two need a restart, which is why the catalog says so.

    Re-examined alongside the other import-time bindings and deliberately left
    alone. It is the one import-time read that `--config-dir` cannot reach, since
    both modules are imported before `get_args()` exports `EDGEWARN_CONFIG_DIR`.
    Two facts make that acceptable rather than merely tolerated:

    - It cannot mislead a later reader. `load_config` memoizes on
      `(resolved_root, name)`, asserted below, so an import-time load under the
      repo default does not become the answer a subsequent `--config-dir` load
      receives. The stale value is confined to these two `maxsize` arguments.
    - The only fix is to build the cache lazily behind a first-call check, which
      puts an extra indirection and a mutable module global into the per-tile and
      per-colormap paths. A cache holding 512 entries instead of a configured 256
      is a memory-footprint difference, not a wrong answer.
    """
    from common.config import loader as config_loader
    from EWMRS.pipeline import _load_timestamp_chunk_index_cached
    from EWMRS.pipeline_config import colormap_cache_entries, tile_index_cache_entries
    from EWMRS.render.render import _get_cached_cmap

    recorded = _ewmrs_pipeline_yaml()
    assert tile_index_cache_entries() == recorded["caches"]["tile_index_entries"] == 512
    assert colormap_cache_entries() == recorded["caches"]["colormap_entries"] == 128

    assert _load_timestamp_chunk_index_cached.cache_info().maxsize == tile_index_cache_entries()
    assert _get_cached_cmap.cache_info().maxsize == colormap_cache_entries()

    # The keying claim is exercised by loading the same catalog from a second
    # root, not by inspecting the existing keys: a loader keyed by name alone
    # would still record absolute roots, so `is_absolute()` could not fail for
    # the reason that matters here.
    alternate = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", alternate)
    catalog = alternate / "ewmrs_pipeline.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            "tile_index_entries: 512", "tile_index_entries: 64"
        ),
        encoding="utf-8",
    )

    try:
        from_alternate = config_loader.load_config("ewmrs_pipeline", config_dir=alternate)
        assert from_alternate["caches"]["tile_index_entries"] == 64
        assert config_loader.load_config("ewmrs_pipeline")["caches"]["tile_index_entries"] == 512
        assert (str(alternate.resolve()), "ewmrs_pipeline") in config_loader._config_cache
    finally:
        # The entry above is keyed by a `tmp_path` that pytest is about to delete.
        # Nothing else looks that key up, so this is hygiene rather than a fix, but
        # leaving a cache pointing at a removed directory is the kind of state that
        # makes a later failure hard to attribute.
        config_loader.reset_cache()


def test_ewmrs_nexrad_render_loop_floor_and_width_come_from_the_catalog():
    """RESOLVED: the poll floor was a bare `max(1.0, ...)` beside its own default.

    The floor exists so a caller passing 0 cannot spin the loop, and it is a
    separate key from the interval because it is a safety bound rather than a
    cadence. `max_workers` is a ceiling, not the pool size -- the effective width
    is capped by the number of pending artifacts.
    """
    from EWMRS.pipeline_config import (
        nexrad_poll_interval_min_seconds,
        nexrad_poll_interval_seconds,
        nexrad_render_max_workers,
    )

    recorded = _ewmrs_pipeline_yaml()["nexrad_gui"]
    assert nexrad_poll_interval_seconds() == recorded["poll_interval_seconds"] == 30.0
    assert nexrad_poll_interval_min_seconds() == recorded["poll_interval_min_seconds"] == 1.0
    assert nexrad_render_max_workers() == recorded["max_workers"] == 8

    # Decomposition Phase 3: the loop body now lives in the NEXRAD service
    # package, which reads the same catalog keys.
    from NEXRAD import config as nexrad_config

    assert (
        nexrad_config.nexrad_poll_interval_seconds()
        == recorded["poll_interval_seconds"]
        == 30.0
    )
    nexrad_source = (
        (REPO_ROOT / "src/NEXRAD/gui_pipeline.py").read_text(encoding="utf-8")
    )
    assert "max(nexrad_poll_interval_min_seconds(), float(poll_interval_seconds))" in nexrad_source
    assert "min(nexrad_render_max_workers(), len(pending_metadata))" in nexrad_source


def test_ewmrs_generic_render_phase_still_cleans_up_by_default():
    """RESOLVED: `cleanup_after` is a catalog key, and GOES still opts out.

    The GOES phase passes `cleanup_after=False` explicitly because it runs its
    own rate-limited sweep afterwards. That literal is a caller decision, not a
    default, so it stays in code -- but the default it overrides is now the
    catalog's.
    """
    from EWMRS.pipeline_config import render_cleanup_after

    recorded = _ewmrs_pipeline_yaml()
    assert render_cleanup_after() is recorded["render"]["cleanup_after"] is True

    source = _ewmrs_pipeline_source()
    assert "cleanup_after=False," in source
    assert "cleanup_after: bool | None = None," in source




# --- MRMS/GOES: --config-dir could not reach the ingest catalog ------------

def test_mrms_catalog_values_are_resolved_per_call_not_at_import():
    """RESOLVED (Phase 5): `--config-dir` reached none of these values.

    `config.py` bound five values at module scope -- two S3 buckets, the ABI
    product, the channel-id tuple, and `GoesIngestSpec.max_files` as a dataclass
    field default. `src/run.py:9` imports this module 23 lines before `get_args()`
    calls `export_config_root`, so every one of them froze the repo-default
    catalog. An operator passing `--config-dir` was silently ingesting from the
    default bucket, not theirs.

    Asserting the module attributes are *absent* is the load-bearing half: an
    accessor that agrees with a surviving constant still leaves two owners.
    """
    from common.ingest.mrms import config as mrms_config

    for name in ("bucket", "ABI_RADC_PRODUCT", "DEFAULT_ABI_RADC_CHANNEL_IDS"):
        assert not hasattr(mrms_config, name)

    # `goes_bucket` kept its name but is now callable rather than a bound string.
    assert callable(mrms_config.goes_bucket)

    source = (REPO_ROOT / "src/common/ingest/mrms/config.py").read_text(encoding="utf-8")
    # The field default was the subtlest of the five: it reads as per-instance but
    # Python evaluates it once, at class-definition time.
    assert 'max_files: int | None = _FROM_CATALOG' in source
    assert '_catalog()["goes"]["max_files_per_spec"]' not in source.split("def goes_max_files_per_spec")[0]


def test_config_dir_reaches_mrms_values_after_the_module_is_already_imported(tmp_path):
    """The regression test for the ordering that made the bug invisible.

    Importing first and exporting second is not artificial -- it is exactly what
    `src/run.py` does. A test that exported before importing would pass against
    the old module constants too, and so would prove nothing.
    """
    import shutil

    from common.config import loader
    from common.ingest.mrms import config as mrms_config

    config_dir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    catalog = config_dir / "ingest.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace(mrms_config.mrms_bucket(), "override-mrms")
            .replace(mrms_config.goes_bucket(), "override-goes"),
        encoding="utf-8",
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.export_config_root(config_dir)
        assert mrms_config.mrms_bucket() == "override-mrms"
        assert mrms_config.goes_bucket() == "override-goes"
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous

    assert mrms_config.mrms_bucket() != "override-mrms"


def test_ncep_https_fallback_reads_every_value_from_the_catalog():
    """RESOLVED (Phase 5): the whole `mrms.ncep_https` block was inert.

    Seven values were transcribed into the catalog and read by nothing -- the
    client held its own copies. Editing `base_url` or `directory_map` to follow an
    NCEP reorganization did nothing at all, which is the failure mode this
    migration exists to remove.

    Derivation is pinned three ways because the map is only half the rule: a mapped
    product, an unmapped one that derives by splitting on the token, and ProbSevere
    (a sibling of /data/2D, not a directory inside it, hence its own key).
    """
    from datetime import datetime, timezone

    import common.ingest.mrms.https_client as https_client
    from common.ingest.mrms import config as mrms_config

    # A surviving constant would leave two owners even if they agreed today.
    assert not hasattr(https_client, "NCEP_BASE_URL")

    source = (REPO_ROOT / "src/common/ingest/mrms/https_client.py").read_text(encoding="utf-8")
    for literal in (
        '"https://mrms.ncep.noaa.gov',
        "timeout=10",
        "diff < 120",
        "read(8192)",
        'split("_00.")',
        # The map entry, not the docstring's illustrative "EchoTop_18" example.
        '"EchoTop_18_00.50":',
    ):
        assert literal not in source, literal

    base = mrms_config.ncep_base_url()
    finder = https_client.HttpsFileFinder(datetime(2026, 1, 24, 14, 0, tzinfo=timezone.utc))
    # Mapped, so the level suffix cannot simply be dropped.
    assert finder.construct_url("CONUS", "MESH_00.50") == f"{base}/MESH"
    # Absent from the map, so the split-token fallback derives it.
    assert finder.construct_url("CONUS", "EchoTop_50_00.50") == f"{base}/EchoTop_50"
    assert finder.construct_url("CONUS", None) == mrms_config.ncep_probsevere_url()
    assert mrms_config.ncep_probsevere_url() != f"{base}/ProbSevere"


async def _record_download_read_sizes(https_client, dt, outdir):
    """Run one HTTPS download against a fake session, returning the read sizes."""
    sizes = []

    class _Content:
        async def read(self, size):
            sizes.append(size)
            # One chunk then EOF, so the size is recorded before the stream ends.
            return b"" if sizes.count(size) > 1 else b"x"

    class _Response:
        status = 200
        content = _Content()
        content_length = 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def get(self, _url):
            return _Response()

    name = f"MRMS_Test_{dt.strftime('%Y%m%d-%H%M')}00.grib2.gz"
    with mock.patch.object(https_client.aiohttp, "ClientSession", _Session):
        await https_client.HttpsFileDownloader(dt).download_matching(
            [f"https://example.invalid/{name}"], outdir
        )
    return sizes[:1]


def test_config_dir_reaches_the_ncep_https_values_after_import(tmp_path):
    """Same import-then-export ordering as `src/run.py`, for the fallback block.

    The timeout and the chunk size are observed at the call sites, not through the
    accessors: an accessor that returns the catalog value while its caller keeps a
    literal passes an accessor-only assertion.
    """
    import asyncio
    import shutil
    from datetime import datetime, timezone

    from common.config import loader
    import common.ingest.mrms.https_client as https_client
    from common.ingest.mrms import config as mrms_config

    config_dir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    catalog = config_dir / "ingest.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace("base_url: https://mrms.ncep.noaa.gov/data/2D", "base_url: https://example.invalid/2D")
            .replace("sync_timeout_seconds: 10", "sync_timeout_seconds: 4")
            .replace("download_chunk_size_bytes: 8192", "download_chunk_size_bytes: 512")
            .replace("MESH_00.50: MESH", "MESH_00.50: MESH_OVERRIDE"),
        encoding="utf-8",
    )

    dt = datetime(2026, 1, 24, 14, 0, tzinfo=timezone.utc)
    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    timeouts = []
    try:
        loader.export_config_root(config_dir)

        finder = https_client.HttpsFileFinder(dt)
        assert finder.construct_url("CONUS", "MESH_00.50") == "https://example.invalid/2D/MESH_OVERRIDE"

        def _fake_get(url, timeout=None):
            timeouts.append(timeout)
            raise RuntimeError("no network in tests")

        with mock.patch.object(https_client.requests, "get", _fake_get):
            assert finder.find_files_sync("CONUS", "MESH_00.50") == []
        assert timeouts == [4]

        assert asyncio.run(
            _record_download_read_sizes(https_client, dt, tmp_path / "out")
        ) == [512]
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()

    assert mrms_config.ncep_sync_timeout_seconds() == 10


def test_ncep_fuzzy_match_window_widens_and_narrows_with_the_catalog(tmp_path):
    """The one value with no other observable effect, pinned at both edges.

    `match_window_seconds` is reached only after an exact-minute match fails, so a
    test that offers an on-the-minute filename never exercises it. Both directions
    are asserted: a file 100 s away is accepted under the shipped 120 and rejected
    once the catalog narrows, so neither widening nor narrowing the literal back in
    can pass.
    """
    import asyncio
    import shutil
    from datetime import datetime, timedelta, timezone

    from common.config import loader
    import common.ingest.mrms.https_client as https_client

    dt = datetime(2026, 1, 24, 14, 0, 0, tzinfo=timezone.utc)
    # 100 s away: inside the shipped 120 s window, outside a narrowed 60 s one, and
    # far enough off the minute that the exact-match branch cannot claim it.
    offset_name = (
        f"MRMS_Test_{(dt + timedelta(seconds=100)).strftime('%Y%m%d-%H%M%S')}.grib2.gz"
    )

    def _attempt(outdir):
        """True if the offset filename was accepted as a match.

        Recorded rather than signalled by an exception: `download_matching` catches
        `Exception`, so anything raised from the fake session is swallowed and the
        return value is None either way.
        """
        requested = []

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            def get(self, url):
                requested.append(url)
                raise ConnectionError("no network in tests")

        with mock.patch.object(https_client.aiohttp, "ClientSession", _Session):
            asyncio.run(
                https_client.HttpsFileDownloader(dt).download_matching(
                    [f"https://example.invalid/{offset_name}"], outdir
                )
            )
        return bool(requested)

    assert _attempt(tmp_path / "wide") is True

    config_dir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    catalog = config_dir / "ingest.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            "match_window_seconds: 120", "match_window_seconds: 60"
        ),
        encoding="utf-8",
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.export_config_root(config_dir)
        assert _attempt(tmp_path / "narrow") is False
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()


# --- Integrate: --config-dir must reach every value ------------------------


def test_probsevere_field_map_has_no_module_level_owner():
    """`integrator.py` must not rebind the mapping as a module constant.

    Deleting `PROBSEVERE_FIELD_MAP` is what fixes the import-time freeze above;
    reintroducing it as a convenience alias would restore the bug, since the
    alias is evaluated at import.
    """
    from EdgeWARN.process.integrate.core import integrator

    assert not hasattr(integrator, "PROBSEVERE_FIELD_MAP")


# --- The 120-second scan cadence: two owners, kept distinct ----------------


def test_fallback_and_default_dt_seconds_agree():
    """`detection.fallback_dt_seconds` and `assignment_costs.default_dt_seconds`.

    These are deliberately NOT collapsed: the first is the tracker's fallback
    when elapsed time cannot be derived from timestamps, and belongs to the
    subsystem that owns the timestamps; the second is a cost-function fallback in
    the kalman package, unreachable in production because every live
    `compute_cost` call site forwards a concrete dt.

    They must nonetheless agree. Within one scan the state is propagated with
    `kf.predict(dt_seconds)` and candidates are scored with the same dt, so if
    the two diverged the cost function would score an implied velocity against a
    different baseline than the state was advanced by.
    """
    from common.config.loader import load_config

    detection = load_config("detection")["detection"]["fallback_dt_seconds"]
    kalman = load_config("kalman")["assignment_costs"]["default_dt_seconds"]
    assert detection == kalman == 120.0


def test_update_cells_dt_seconds_has_no_literal_default():
    """`track.py` must not restate the fallback as a signature default.

    The production caller (`detect/main.py`) always computes and passes
    `dt_seconds`, so a literal default there was reachable only from tests --
    and it was a second owner of a catalog value.
    """
    assert param_default(
        "EdgeWARN/process/detect/track.py",
        "StormCellTracker.update_cells",
        "dt_seconds",
    ) is None


# --- Kalman: the unused assignment regularization key is gone -------------


def test_assignment_has_no_second_regularization_owner():
    """`assignment.covariance_regularization` was a duplicate, now deleted.

    It was loaded into `AssignmentConfig` but consumed by nothing; the value
    actually applied to the innovation covariance is
    `filter_internals.innovation_covariance_regularization`, read at
    `filter.py:434`. Two keys, one concept, same number -- so the unread one was
    a second owner waiting to disagree.
    """
    import dataclasses

    from common.config.loader import load_config
    from EdgeWARN.process.detect.kalman.config import AssignmentConfig

    assert "covariance_regularization" not in load_config("kalman")["assignment"]
    fields = {field.name for field in dataclasses.fields(AssignmentConfig)}
    assert "covariance_regularization" not in fields


# --- detect: concurrency and cache bounds are catalog-owned ----------------


def test_dataset_load_worker_count_has_no_literal_owner():
    """`detect.py` must take its pool size from the threaded config.

    The literal `3` matched the three loads submitted below it, so raising it
    bought nothing -- but it also meant an operator diagnosing I/O contention
    had no way to serialize the loads without editing source.
    """
    from tests.architecture.source_inspect import SRC

    source = (SRC / "EdgeWARN/process/detect/detect.py").read_text(encoding="utf-8")
    assert "max_workers=detection_config.dataset_load_max_workers" in source
    assert "max_workers=3" not in source


def test_dataset_load_worker_count_reaches_the_typed_config():
    from common.config.loader import load_config
    from EdgeWARN.process.detect.config import DetectionConfig

    expected = load_config("detection")["detection"]["dataset_load_max_workers"]
    assert DetectionConfig.from_yaml().dataset_load_max_workers == expected


def test_alert_matcher_caches_are_bounded():
    """Both module-level caches were unbounded for the process lifetime.

    They are keyed by `(registry_dir, snapshot)`, so a historical replay -- which
    walks every retained snapshot -- grew them without limit. Eviction is
    insertion-ordered because a run only ever revisits the snapshot for the scan
    it is currently processing.
    """
    from EdgeWARN.process.detect.tools import alert_matcher

    cache = {}
    for index in range(10):
        alert_matcher._store_bounded(cache, ("snapshot", index), index, 4)

    assert list(cache) == [("snapshot", index) for index in range(6, 10)]


def test_alert_matcher_cache_bounds_are_read_from_the_catalog():
    """The bounds must be read per call, not frozen at import.

    `alert_matcher` is imported from `detect/main.py`, so a module-scope read of
    either bound would freeze the repo-default config directory the same way
    `azshear/constants.py` did.
    """
    from tests.architecture.source_inspect import SRC
    from common.config.loader import load_config

    catalog = load_config("detection")["alert_matching"]
    assert catalog["snapshot_cache_max_entries"] >= 1
    assert catalog["geometry_cache_max_entries"] >= 1

    source = (SRC / "EdgeWARN/process/detect/tools/alert_matcher.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if "cache_max_entries" in line:
            assert line.startswith(" "), f"module-scope read of a cache bound: {line!r}"


# --- MRMS/GOES: the retention and depth literals now have one owner each ------


def _ingest_yaml() -> dict:
    import yaml

    return yaml.safe_load((REPO_ROOT / "config" / "ingest.yaml").read_text(encoding="utf-8"))


def _src_text(relative_path: str) -> str:
    return (REPO_ROOT / "src" / relative_path).read_text(encoding="utf-8")


def test_decompress_chunk_size_has_no_module_level_owner():
    """`s3_common.DECOMPRESS_CHUNK_SIZE` was a second owner of the catalog key.

    It is distinct from `mrms.ncep_https.download_chunk_size_bytes` (8192): that
    one sizes network reads, this one sizes a local gzip expansion. Both are
    real, so the fix was to give this one an accessor, not to merge them.
    """
    from common.ingest.mrms import s3_common
    from common.ingest.mrms.config import mrms_decompress_chunk_size_bytes

    assert not hasattr(s3_common, "DECOMPRESS_CHUNK_SIZE")
    assert mrms_decompress_chunk_size_bytes() == _ingest_yaml()["mrms"]["decompress_chunk_size_bytes"]


def test_goes_hour_lookback_has_one_resolution_site():
    """The five GOES entry points must not restate the window as a default.

    `_get_goes_bucket_paths` is the only consumer, so it is the only resolver;
    the entry points pass `None` through, which keeps a per-call override
    possible without making any of them a second owner.
    """
    from datetime import datetime, timezone

    from common.ingest.mrms.downloader import _get_goes_bucket_paths

    source = _src_text("common/ingest/mrms/downloader.py")
    assert "hour_lookback=3" not in source

    expected = _ingest_yaml()["goes"]["hour_lookback"]
    paths = _get_goes_bucket_paths(datetime(2026, 8, 16, 12, tzinfo=timezone.utc), "ABI-L1b-RadC")
    assert len(paths) == expected


def test_goes_cleanup_retention_is_catalog_owned():
    """Six `max_age_minutes=60` literals in downloader.py, now none."""
    from common.ingest.mrms.config import goes_cleanup_max_age_minutes

    source = _src_text("common/ingest/mrms/downloader.py")
    assert "max_age_minutes=60" not in source
    assert goes_cleanup_max_age_minutes() == _ingest_yaml()["goes"]["cleanup_max_age_minutes"]


def test_mrms_and_goes_cleanup_windows_stay_separate_keys():
    """Both are 60 today and must remain two keys.

    GOES cleanup additionally enforces `max_files_per_spec`, so the two windows
    bound different retention policies; collapsing them would tie a count cap to
    an age cap that has no reason to move with it.
    """
    catalog = _ingest_yaml()
    assert catalog["mrms"]["cleanup_max_age_minutes"] == 60
    assert catalog["goes"]["cleanup_max_age_minutes"] == 60
    assert "max_files_per_spec" in catalog["goes"]


def test_mrms_cleanup_and_pruning_flag_are_catalog_owned():
    """main.py restated the retention window four times and the flag five."""
    from common.ingest.mrms.main import _cleanup_kwargs, _resolve_ingest_args

    source = _src_text("common/ingest/mrms/main.py")
    assert '"max_age_minutes": 60' not in source
    assert "remove_old_files=True" not in source

    catalog = _ingest_yaml()["mrms"]
    assert _cleanup_kwargs() == {"max_age_minutes": catalog["cleanup_max_age_minutes"]}
    assert _resolve_ingest_args(None, None)[1] == catalog["remove_old_files"]


def test_mrms_ingest_depth_is_owned_only_by_runtime_yaml():
    """`ingest.yaml download_max_entries` was a third copy of one number.

    `runtime.yaml cycle.ingest_max_entries` already owned the depth for the
    callers that pass one; main.py's seven `max_entries=10` defaults owned it for
    the callers that do not. Deleting the catalog key and defaulting the
    signatures to `None` leaves exactly one owner.
    """
    from common.config.loader import load_config
    from common.ingest.mrms.main import _ingest_max_entries

    assert "download_max_entries" not in _ingest_yaml()["mrms"]
    assert "max_entries=10" not in _src_text("common/ingest/mrms/main.py")
    assert _ingest_max_entries() == load_config("runtime")["cycle"]["ingest_max_entries"]


# --- package.json is the only owner of the version string -------------------


def test_no_javascript_file_restates_the_package_version():
    """Services and their tests each carried their own literal version string.

    Runnable without Node, which matters: the JS suite cannot run on every
    machine that touches this repo, so the invariant is pinned from Python.

    The `'2.x'` production mask is deliberately NOT covered here. It is not a
    copy of the version -- it is a decision to withhold the version -- so a
    string equal to package.json's is exactly what it must never be. It is
    owned by `api.yaml` as `server.production_version_label` and read at
    src/api/app.js:33, where it replaces the real version under NODE_ENV=production.
    """
    import json

    version = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    offenders = []
    for path in list((REPO_ROOT / "src").rglob("*.js")) + list((REPO_ROOT / "tests").rglob("*.js")):
        if f"'{version}'" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == [], f"version literal restated in {offenders}"


# --- zone_sync: one CLI, two config roots -----------------------------------


def test_zone_sync_resolves_its_identity_from_the_same_root_as_its_settings():
    """RESOLVED: `--config-dir X` gave X's zone settings and the default's UA.

    `_resolve_zone_sync_args` threads `args.config_dir` into every value it
    reads, which looks complete -- but the User-Agent is not one of those values.
    It is built in `NWSZoneSync.__init__` by `format_user_agent()`, which loads
    `runtime.yaml` on its own with `config_dir=None`. Nothing bridged the gap:
    zone_sync never published the root it had resolved, so one run identified
    itself to api.weather.gov out of a config root it was not otherwise using.

    Asserting on `contact` rather than the whole header keeps the test honest
    about which root won -- the version half comes from package.json either way.
    """
    import shutil
    import tempfile

    from common.config import loader
    from common.ingest.nws.zone_sync import _resolve_zone_sync_args

    config_dir = Path(tempfile.mkdtemp(prefix="cfgdir-zonesync-"))
    shutil.copytree(REPO_ROOT / "config", config_dir, dirs_exist_ok=True)
    catalog = config_dir / "runtime.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace("contact: ewsbackend@gmail.com", "contact: relocated@example.com"),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        config_dir=str(config_dir), assets_dir=None, zone_types=None,
        timeout_seconds=None, max_retries=None, max_workers=None,
        pause_seconds=None, progress=None,
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.reset_cache()
        resolved = _resolve_zone_sync_args(args)
        # The settings followed --config-dir before this fix, and still must.
        assert resolved.assets_dir == config_dir.parent / "assets/nws_zones"
        from common.ingest.nws.zone_sync import NWSZoneSync

        header = NWSZoneSync(resolved.assets_dir).headers["User-Agent"]
        assert "relocated@example.com" in header
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()


# --- The nws block: four values the catalog only documented -----------------


def test_alert_ingest_constants_have_no_module_level_owner():
    """`DROPPED_EVENTS` must be gone, not merely unread.

    It was a module-scope set in `main.py`, so `--config-dir` could never reach
    it -- and leaving it behind as an alias would restore that, since the alias
    is evaluated at import. The two TTL signature defaults are the subtler half:
    they read as per-call but Python binds them once.
    """
    from common.ingest.nws import main as nws_main

    assert not hasattr(nws_main, "DROPPED_EVENTS")

    for function in ("AlertRegistry.__init__", "get_registry"):
        assert param_default(
            "common/ingest/nws/registry.py", function, "ttl_hours"
        ) is None


def test_config_dir_reaches_the_nws_alert_values_after_import():
    """Import first, export second -- the order `src/run.py` uses."""
    import shutil
    import tempfile

    from common.config import loader
    from common.ingest.nws import config as nws_config
    from common.ingest.nws.registry import AlertRegistry

    config_dir = Path(tempfile.mkdtemp(prefix="cfgdir-nws-"))
    shutil.copytree(REPO_ROOT / "config", config_dir, dirs_exist_ok=True)
    catalog = config_dir / "nws.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace("registry_ttl_hours: 2.0", "registry_ttl_hours: 9.5")
            .replace("    - Administrative Message", "    - Overridden Event")
            .replace("event: Tornado Warning", "event: Overridden Warning"),
        encoding="utf-8",
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.export_config_root(config_dir)
        loader.reset_cache()
        assert nws_config.registry_ttl_hours() == 9.5
        assert "Overridden Event" in nws_config.dropped_events()
        assert "Administrative Message" not in nws_config.dropped_events()
        assert nws_config.tornado_upgrade_event() == "Overridden Warning"
        # The TTL reaches the object, not just the accessor.
        assert AlertRegistry(config_dir / "registry").ttl_hours == 9.5
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()

    assert nws_config.registry_ttl_hours() == 2.0


def test_tornado_upgrade_phrases_cannot_be_written_lowercase():
    """The description is uppercased before matching, so a lowercase phrase
    would never fire -- silently, with no alert renamed and no error raised.
    """
    import shutil
    import tempfile

    from common.config import loader
    from common.config.loader import ConfigError

    config_dir = Path(tempfile.mkdtemp(prefix="cfgdir-tornado-"))
    shutil.copytree(REPO_ROOT / "config", config_dir, dirs_exist_ok=True)
    catalog = config_dir / "nws.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace("description_contains: TORNADO EMERGENCY", "description_contains: Tornado Emergency"),
        encoding="utf-8",
    )

    loader.reset_cache()
    try:
        with pytest.raises(ConfigError) as excinfo:
            loader.load_config("nws", config_dir=config_dir)
        assert "description_contains" in str(excinfo.value)
    finally:
        loader.reset_cache()


def test_geometry_precision_has_no_literal_owner_in_any_signature():
    """One rounding precision, previously restated as four keyword defaults.

    `round_coords`, `_normalize_ring`, `_geometry_to_polygon_rings`, and
    `round_geojson_coords` each declared `precision: int = 4` and no caller
    overrode any of them, so the four agreed only by coincidence and changing
    the rounding meant finding all four. They keep the parameter -- a caller
    holding the value passes it down instead of re-resolving per ring -- but the
    default must be None so the catalog is the only owner.
    """
    for function in (
        "round_coords",
        "_normalize_ring",
        "_geometry_to_polygon_rings",
        "round_geojson_coords",
    ):
        assert param_default("common/ingest/nws/geomapper.py", function, "precision") is None, (
            f"{function} restates the precision as a literal default"
        )

    assert param_default("common/ingest/nws/geomapper.py", "extract_exterior_polygon", "tolerance") is None
    assert param_default("common/ingest/nws/geomapper.py", "extract_exterior_polygon", "precision") is None

    from common.ingest.nws import geomapper

    assert not hasattr(geomapper, "JUNK_KEYS"), (
        "the property blocklist is back at module scope, which freezes it at import"
    )


def test_config_dir_reaches_the_geomapper_geometry_values_after_import():
    """Import first, export second -- the order `src/run.py` uses.

    Asserts through `process_warning`, not just the accessors: the precision has
    to survive the call chain down to the ring that lands on disk, which is the
    thing the four duplicated defaults used to control.
    """
    import shutil
    import tempfile

    from common.config import loader
    from common.ingest.nws import config as nws_config
    from common.ingest.nws import geomapper

    config_dir = Path(tempfile.mkdtemp(prefix="cfgdir-geomapper-"))
    shutil.copytree(REPO_ROOT / "config", config_dir, dirs_exist_ok=True)
    catalog = config_dir / "nws.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace("  geometry_precision: 4", "  geometry_precision: 1")
            .replace("  simplify_tolerance: 0.01", "  simplify_tolerance: 0.5")
            .replace("    - eventCode", "    - overriddenKey"),
        encoding="utf-8",
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.export_config_root(config_dir)
        loader.reset_cache()

        assert nws_config.geometry_precision() == 1
        assert nws_config.simplify_tolerance() == 0.5
        assert "overriddenKey" in nws_config.junk_keys()

        assert geomapper.round_coords([[1.23456, 2.34567]]) == [[1.2, 2.3]]

        # The tolerance reaches the simplifier, not just the accessor: this notch
        # survives the catalog's 0.01 and is flattened away by the 0.5 above.
        notched = [[0, 0], [1, 0], [1, 1], [0.5, 0.9], [0, 1]]
        simplified = geomapper.extract_exterior_polygon([notched], precision=4)
        assert [0.5, 0.9] not in simplified[0], (
            "the catalog tolerance did not reach the simplifier"
        )

        feature = {
            "properties": {"event": "Test", "eventCode": "kept", "overriddenKey": "gone"},
            "geometry": {"type": "Polygon", "coordinates": [[[1.23456, 2.34567]] * 4]},
        }
        processed = geomapper.process_warning(feature)

        assert processed["geometry"]["coordinates"][0][0] == [1.2, 2.3], (
            "the catalog precision did not reach the geometry written to disk"
        )
        assert "overriddenKey" not in processed["properties"]
        assert processed["properties"]["eventCode"] == "kept", (
            "the source blocklist outranked the catalog"
        )
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()

    assert nws_config.geometry_precision() == 4
    assert "eventCode" in nws_config.junk_keys()


def test_zone_sync_settings_have_no_literal_owner_in_the_constructor():
    """Every NWSZoneSync setting used to restate its catalog value as a default.

    They agreed only for as long as nobody edited one side, and an instance built
    without arguments -- which the tests do -- ran on the source copy rather than
    the operator's. `zone_types` was the worst of them: a `ZONE_TYPES` module
    constant, bound at import and so unreachable from `--config-dir` even in
    principle.
    """
    for parameter in (
        "zone_types",
        "timeout_seconds",
        "max_retries",
        "max_workers",
        "pause_seconds",
        "show_progress",
    ):
        assert param_default("common/ingest/nws/zone_sync.py", "NWSZoneSync.__init__", parameter) is None, (
            f"NWSZoneSync.__init__ restates {parameter} as a literal default"
        )

    for parameter in ("precision", "precision_max"):
        assert param_default("common/ingest/nws/zone_sync.py", "geometry_to_rings", parameter) is None

    from common.ingest.nws import zone_sync

    assert not hasattr(zone_sync, "ZONE_TYPES"), (
        "the zone-type list is back at module scope, which freezes it at import"
    )


def test_config_dir_reaches_the_zone_sync_settings_after_import():
    """Import first, export second -- the order `src/run.py` uses.

    Covers the six keys that had no reader at all before: the backoff, both ends
    of the precision escalation, and both URL patterns. The patterns are asserted
    through the request the syncer actually makes, since an attribute that never
    reaches a URL would be indistinguishable from a wired one.
    """
    import shutil
    import tempfile
    from unittest.mock import patch

    from common.config import loader
    from common.ingest.nws import config as nws_config
    from common.ingest.nws import zone_sync as zone_sync_module
    from common.ingest.nws.zone_sync import NWSZoneSync, geometry_to_rings

    config_dir = Path(tempfile.mkdtemp(prefix="cfgdir-zonesync-"))
    shutil.copytree(REPO_ROOT / "config", config_dir, dirs_exist_ok=True)
    catalog = config_dir / "nws.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace("  zone_types: [forecast, fire, public, county, marine]", "  zone_types: [marine]")
            .replace("  timeout_seconds: 30", "  timeout_seconds: 7")
            .replace("  max_retries: 3", "  max_retries: 9")
            .replace("  max_workers: 16", "  max_workers: 2")
            .replace("  retry_backoff_seconds: 0.25", "  retry_backoff_seconds: 1.5")
            .replace("  geometry_precision: 5", "  geometry_precision: 6")
            .replace("  geometry_precision_max: 8", "  geometry_precision_max: 7")
            .replace("zones/{zone_type}'", "zones-relocated/{zone_type}'")
            .replace("zones/{zone_type}/{code}'", "zones-relocated/{zone_type}/{code}'"),
        encoding="utf-8",
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.export_config_root(config_dir)
        loader.reset_cache()

        assert nws_config.zone_geometry_precision() == (6, 7)

        syncer = NWSZoneSync(Path("."))
        assert syncer.zone_types == ("marine",)
        assert syncer.timeout_seconds == 7
        assert syncer.max_retries == 9
        assert syncer.max_workers == 2
        assert syncer.retry_backoff_seconds == 1.5

        # An explicit argument still outranks the catalog, which is how main()
        # delivers the CLI overlay.
        assert NWSZoneSync(Path("."), max_workers=5).max_workers == 5

        requested = []
        with patch.object(NWSZoneSync, "_request_json", side_effect=lambda url: requested.append(url) or {}):
            syncer.fetch_zone_catalog()
            syncer.fetch_zone_geometry("marine", "TXZ001")

        assert requested == [
            "https://api.weather.gov/zones-relocated/marine",
            "https://api.weather.gov/zones-relocated/marine/TXZ001",
        ], "the catalog URL patterns did not reach the request"

        # The backoff reaches the sleep, not just the attribute. Three attempts
        # sleep twice, at 1x and 2x the catalog value, linearly.
        retrier = NWSZoneSync(Path("."), max_retries=3)
        slept = []
        with patch("common.ingest.nws.zone_sync.time.sleep", side_effect=slept.append):
            with patch.object(NWSZoneSync, "_get_thread_session", side_effect=RuntimeError("boom")):
                with pytest.raises(RuntimeError):
                    retrier._request_json("https://example.invalid")
        assert slept == [1.5, 3.0], "the catalog backoff did not reach the sleep"

        # Both ends of the escalation reach the loop: a 6..7 window tries one
        # precision, where the repo default's 5..8 would try three.
        attempted = []
        square = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
        with patch.object(
            zone_sync_module,
            "_normalize_ring",
            side_effect=lambda coords, precision: attempted.append(precision) or [],
        ):
            geometry_to_rings(square)
        assert attempted == [6], f"the escalation window came from source, not the catalog: {attempted}"
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()

    assert nws_config.zone_geometry_precision() == (5, 8)


# --- the api.weather.gov Accept header: one owner across two subsystems ----

def test_the_weather_api_accept_header_has_no_literal_owner():
    """RESOLVED: `application/geo+json` was the same literal at four sites.

    They spanned two subsystems -- NWS zone sync, both NWS alert downloads, and
    the NEXRAD station catalog -- which is why the key could not live in either
    subsystem's catalog and sat in `nws.yaml zone_sync` as UNUSED instead. Wiring
    it there would have made one of the four configurable while leaving an
    operator believing all four had moved.
    """
    for relative in (
        "src/common/ingest/nws/zone_sync.py",
        "src/common/ingest/nws/main.py",
        "src/common/ingest/nexrad/weather_api.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert '"application/geo+json"' not in source, (
            f"{relative} carries its own copy of the Accept header again"
        )

    # The retired key is gone from the catalog, not merely unread -- a leftover
    # `zone_sync.accept` would read as the live owner of a value now held in
    # runtime.yaml.
    from common.config.loader import load_config

    assert "accept" not in load_config("nws")["zone_sync"]
    assert "weather_api_accept" in load_config("runtime")["identity"]


def test_config_dir_reaches_every_outbound_weather_api_header_after_import():
    """Import first, export second -- the order `src/run.py` uses.

    Asserts through the header dicts the request layer is actually handed, so a
    helper that resolved the catalog but was not reached by a call site would
    still fail here.
    """
    import shutil
    import tempfile

    from common.config import loader
    from common.ingest.nexrad.weather_api import RadarStationCatalog
    from common.ingest.nws.zone_sync import NWSZoneSync
    from util.release import weather_api_headers

    config_dir = Path(tempfile.mkdtemp(prefix="cfgdir-accept-"))
    shutil.copytree(REPO_ROOT / "config", config_dir, dirs_exist_ok=True)
    catalog = config_dir / "runtime.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
            .replace(
                "  weather_api_accept: application/geo+json",
                "  weather_api_accept: application/relocated+json",
            )
            .replace("  contact: ewsbackend@gmail.com", "  contact: relocated@example.org"),
        encoding="utf-8",
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.export_config_root(config_dir)
        loader.reset_cache()

        expected_accept = "application/relocated+json"
        assert weather_api_headers()["Accept"] == expected_accept
        assert "relocated@example.org" in weather_api_headers()["User-Agent"]

        assert NWSZoneSync(Path(".")).headers["Accept"] == expected_accept
        assert RadarStationCatalog()._headers()["Accept"] == expected_accept

        # The two callers that take an override still win over the catalog, and
        # overriding the agent must not disturb the Accept.
        overridden = NWSZoneSync(Path("."), user_agent="Explicit/1").headers
        assert overridden["User-Agent"] == "Explicit/1"
        assert overridden["Accept"] == expected_accept
        assert RadarStationCatalog(user_agent="Explicit/2")._headers()["User-Agent"] == "Explicit/2"
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()

    assert weather_api_headers()["Accept"] == "application/geo+json"


# --- the async alert download's chunk size ---------------------------------

def test_config_dir_reaches_the_alert_download_chunk_size_after_import():
    """The 8192 at main.py's `iter_chunked` had no catalog key at all.

    Asserted through the argument `iter_chunked` receives rather than through the
    accessor, because a resolved value that never reached the call would look
    identical from the outside.
    """
    import shutil
    import tempfile

    from common.config import loader
    from common.ingest.nws import config as nws_config

    source = (REPO_ROOT / "src/common/ingest/nws/main.py").read_text(encoding="utf-8")
    assert "iter_chunked(8192)" not in source, "the chunk size is a literal again"

    config_dir = Path(tempfile.mkdtemp(prefix="cfgdir-chunk-"))
    shutil.copytree(REPO_ROOT / "config", config_dir, dirs_exist_ok=True)
    catalog = config_dir / "nws.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            "  download_chunk_size_bytes: 8192", "  download_chunk_size_bytes: 4096"
        ),
        encoding="utf-8",
    )

    previous = os.environ.get("EDGEWARN_CONFIG_DIR")
    try:
        loader.export_config_root(config_dir)
        loader.reset_cache()

        assert nws_config.download_chunk_size_bytes() == 4096
        assert _chunk_size_reaching_iter_chunked() == 4096
    finally:
        if previous is None:
            os.environ.pop("EDGEWARN_CONFIG_DIR", None)
        else:
            os.environ["EDGEWARN_CONFIG_DIR"] = previous
        loader.reset_cache()

    assert nws_config.download_chunk_size_bytes() == 8192


def _chunk_size_reaching_iter_chunked() -> int:
    """Run `download_alerts_async` far enough to see `iter_chunked`'s argument.

    Everything past the streaming copy is stubbed: the aiohttp session, the
    registry, and the file parse. The download is driven to completion rather
    than aborted so the temp file it creates is still cleaned up by its own
    `finally`.
    """
    import asyncio
    from unittest.mock import patch

    from common.ingest.nws import main as nws_main

    recorded: list[int] = []

    class _Content:
        def iter_chunked(self, size):
            recorded.append(size)

            async def _gen():
                yield b"{}"

            return _gen()

    class _Response:
        content = _Content()

        def raise_for_status(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def get(self, url):
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    registry = mock.MagicMock()
    registry.alert_count = 0
    registry.reconcile_with_active_ids.return_value = 0
    registry.cleanup_expired.return_value = 0

    with patch.object(nws_main.aiohttp, "ClientSession", lambda *a, **k: _Session()), \
         patch.object(nws_main, "_get_registry", return_value=registry), \
         patch.object(
             nws_main, "_process_nws_file_with_registry", return_value=(0, 0, set())
         ):
        asyncio.run(nws_main.download_alerts_async(datetime(2026, 8, 16, tzinfo=timezone.utc)))

    assert len(recorded) == 1, f"expected one streaming copy, saw {recorded}"
    return recorded[0]


# --- EWMRS RAP uint16: three scalars the catalog used to only describe -----

def _config_dir_with_overrides(tmp_path, catalog_name, indent="", **overrides):
    """Copy `config/` and rewrite whole `key: value` lines in one catalog.

    Line-anchored rather than a bare substring replace: every one of these keys
    is also named in its own explanatory comment, and a substring rewrite would
    edit the comment instead of the key. `indent` selects the nesting level, so a
    top-level `resampling` could never be mistaken for a nested one.
    """
    import shutil

    config_dir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    catalog = config_dir / f"{catalog_name}.yaml"

    lines = catalog.read_text(encoding="utf-8").splitlines()
    for key, value in overrides.items():
        prefix = f"{indent}{key}:"
        replaced = 0
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                lines[index] = f"{prefix} {value}"
                replaced += 1
        assert replaced == 1, f"expected one {prefix!r} line, rewrote {replaced}"
    catalog.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_dir


def _rap_uint16_config_dir(tmp_path, **overrides):
    return _config_dir_with_overrides(tmp_path, "ewmrs_pipeline", indent="  ", **overrides)


def _with_config_root(config_dir):
    """Export `config_dir` as the config root, restoring the previous value."""
    import contextlib

    from common.config import loader

    @contextlib.contextmanager
    def _scope():
        previous = os.environ.get("EDGEWARN_CONFIG_DIR")
        try:
            loader.export_config_root(config_dir)
            yield
        finally:
            if previous is None:
                os.environ.pop("EDGEWARN_CONFIG_DIR", None)
            else:
                os.environ["EDGEWARN_CONFIG_DIR"] = previous

    return _scope()


def test_rap_uint16_retention_is_observed_at_both_of_its_call_sites(tmp_path):
    """RESOLVED (Phase 5): `max_timestamps` was inert, and doubly so.

    Two functions held their own `= 3` default and the one live caller passed a
    third literal `max_timestamps=3`. They are not independent knobs:
    `_update_product_index` publishes the timestamp list a client reads and
    `cleanup_old_rap_uint16_layers` deletes the directories behind it, so a
    disagreement advertises data that is already gone. Both are asserted here
    because wiring only the accessor, or only one caller, would still leave the
    catalog value half-honoured.
    """
    pytest.importorskip("eccodes")

    import util.file as fs
    from EWMRS.rap import uint16_pipeline

    config_dir = _rap_uint16_config_dir(tmp_path, max_timestamps=1)
    stamps = ["20260816-120000", "20260816-121500", "20260816-123000"]

    index_dir = tmp_path / "index_layer"
    index_dir.mkdir()
    (index_dir / "index.json").write_text(json.dumps(stamps), encoding="utf-8")

    gui_root = tmp_path / "gui_rap"
    layer_dir = gui_root / "Temperature_2m"
    for stamp in stamps:
        (layer_dir / stamp).mkdir(parents=True)

    # The published index is captured rather than read back off disk: this
    # platform's `os.fsync` raises inside `util.atomic`, which would fail the
    # test for a reason that has nothing to do with the value under test.
    published: dict = {}

    with _with_config_root(config_dir):
        with mock.patch.object(
            uint16_pipeline,
            "atomic_write_json",
            lambda path, data, **kwargs: published.update(data),
        ):
            uint16_pipeline._update_product_index(index_dir, "20260816-124500")

        with mock.patch.object(fs, "GUI_RAP_DIR", gui_root):
            removed = uint16_pipeline.cleanup_old_rap_uint16_layers()

    assert published["timestamps"] == ["20260816-124500"]
    assert removed == 2
    assert sorted(path.name for path in layer_dir.iterdir()) == ["20260816-123000"]

    # The repo default keeps three, so the assertions above are reading the
    # override rather than a coincidence.
    assert uint16_pipeline.rap_uint16_max_timestamps() == 3


def test_rap_uint16_timestamp_format_names_the_output_directory(tmp_path):
    """RESOLVED (Phase 5): the pattern was a raw string literal in `_timestamp_label`.

    Observed through `_timestamp_label` rather than the accessor: the accessor
    returning the catalog value proves nothing while its caller keeps a literal.
    """
    pytest.importorskip("eccodes")

    from EWMRS.rap import uint16_pipeline

    config_dir = _rap_uint16_config_dir(tmp_path, timestamp_format="'%Y%m%dT%H%M'")
    rap_path = tmp_path / "RAP.20260816-18z.grib2"
    rap_path.write_bytes(b"")

    with _with_config_root(config_dir):
        labelled = uint16_pipeline._timestamp_label(None, rap_path)

    assert labelled == "20260816T1800"
    assert uint16_pipeline._timestamp_label(None, rap_path) == "20260816-180000"


def test_rap_uint16_force_defaults_to_none_so_false_stays_a_caller_choice(tmp_path):
    """RESOLVED (Phase 5): `force: bool = False` shadowed the catalog value.

    The default had to become `None`, not stay `False`: with `False` there is no
    way to tell "the caller wants no re-encode" from "the caller said nothing",
    so a catalog `force: true` could never win. `layers=[]` returns before any
    GRIB is opened, which is far enough to see the resolution.
    """
    pytest.importorskip("eccodes")

    from EWMRS.rap import uint16_pipeline

    config_dir = _rap_uint16_config_dir(tmp_path, force="true")
    # A real file, because `_timestamp_label` runs before the early return and
    # falls back to `stat()` for a name it cannot parse.
    rap_path = tmp_path / "RAP.20260816-18z.grib2"
    rap_path.write_bytes(b"")

    observed: list[bool] = []
    real_force = uint16_pipeline.rap_uint16_force

    def _record():
        observed.append(real_force())
        return observed[-1]

    with _with_config_root(config_dir):
        with mock.patch.object(uint16_pipeline, "rap_uint16_force", _record):
            uint16_pipeline.run_rap_uint16_pipeline(rap_path, layers=[])
            assert observed == [True]

            # An explicit False must not consult the catalog at all.
            uint16_pipeline.run_rap_uint16_pipeline(rap_path, layers=[], force=False)
            assert observed == [True]


# --- EWMRS GOES reprojection: a key outvoted by three literals -------------

def _resampling_reaching_the_goes_reprojection(config_dir):
    """Return the resampling method that reaches the GOES reprojection helper.

    Recorded rather than raised: `reproject_goes_abi_to_web_mercator` wraps the
    whole payload branch in `except Exception: pass`, so anything thrown from a
    stub is swallowed and the function simply falls through to its second
    attempt. The stub returns None on purpose to let that fall-through happen --
    the argument has already been observed by then.
    """
    import numpy as np
    import xarray as xr
    from affine import Affine

    from EWMRS.render import goes_transform

    dataset = xr.Dataset(
        data_vars={"unknown": (("y", "x"), np.zeros((4, 4), dtype=np.float32))},
        coords={"y": [3.0, 2.0, 1.0, 0.0], "x": [0.0, 1.0, 2.0, 3.0]},
    )
    dataset = dataset.rio.set_spatial_dims(x_dim="x", y_dim="y", inplace=False)
    dataset = dataset.rio.write_crs("EPSG:4326", inplace=False)
    dataset = dataset.rio.write_transform(Affine.identity(), inplace=False)

    observed = []

    def _record(payload, *, shape, transform, resampling):
        observed.append(resampling)
        return None

    with _with_config_root(config_dir):
        with mock.patch.object(
            goes_transform, "_reproject_goes_payload_to_web_mercator", _record
        ):
            goes_transform.reproject_goes_abi_to_web_mercator(
                dataset, shape=(4, 4), transform=Affine.identity()
            )

    assert len(observed) == 1, f"expected one reprojection, saw {observed}"
    return observed[0]


def test_goes_resampling_is_read_from_the_catalog_not_a_signature_default(tmp_path):
    """RESOLVED (Phase 5): `goes_transform.resampling` had three copies over it.

    Two signature defaults (`reproject_goes_abi_to_web_mercator` and
    `load_reproject_goes_abi_render_array`) plus an explicit
    `resampling=Resampling.bilinear` at the one live call site in
    `EWMRS/pipeline.py`. Every one of them said `bilinear`, so the key looked
    honoured; editing it changed nothing.
    """
    pytest.importorskip("rioxarray")

    from rasterio.enums import Resampling

    config_dir = _config_dir_with_overrides(
        tmp_path, "ewmrs_render", indent="  ", resampling="lanczos"
    )
    assert _resampling_reaching_the_goes_reprojection(config_dir) is Resampling.lanczos

    # And the repo default still arrives, so the override above is the reason the
    # assertion passed rather than a coincidence of enum ordering.
    assert (
        _resampling_reaching_the_goes_reprojection(REPO_ROOT / "config")
        is Resampling.bilinear
    )


def test_the_non_goes_reprojection_keeps_nearest_and_is_not_this_key(tmp_path):
    """`EWMRS/pipeline.py` reprojects radar layers with `nearest` deliberately.

    Asserted because it is the one literal that must NOT be folded into
    `goes_transform.resampling`: it is a different layer family with a different
    requirement (crisp edges), and a reader auditing for stragglers would
    otherwise be right to "finish the job" and silently blur radar output.
    """
    source = (REPO_ROOT / "src/EWMRS/pipeline.py").read_text(encoding="utf-8")

    assert "resampling=Resampling.nearest," in source
    # The GOES branch now passes nothing, so the only Resampling literal left in
    # this file is the radar one.
    assert source.count("resampling=Resampling.") == 1


def test_an_unsupported_resampling_name_fails_at_the_catalog_not_in_rasterio(tmp_path):
    """A typo must name the file and key, not surface as a rasterio TypeError.

    `gauss` is the interesting case rather than a nonsense string: rasterio
    really does define it, so the schema enum is what rejects it, and the
    accessor's `SUPPORTED_RESAMPLING` check is the backstop for the day those two
    lists disagree.
    """
    from common.config import loader

    for name in ("gauss", "not_a_method"):
        config_dir = _config_dir_with_overrides(
            tmp_path / name, "ewmrs_render", indent="  ", resampling=name
        )
        with _with_config_root(config_dir):
            with pytest.raises(loader.ConfigError) as excinfo:
                loader.load_config("ewmrs_render")
        assert "resampling" in str(excinfo.value)

    # Now widen the *copied* schema so `gauss` validates, and check the accessor
    # still refuses it. Without this step the enum would be the only guard and the
    # backstop the accessor carries would be untested code.
    from EWMRS.render.config import goes_transform_resampling

    config_dir = _config_dir_with_overrides(
        tmp_path / "widened", "ewmrs_render", indent="  ", resampling="gauss"
    )
    schema_path = config_dir / "schema" / "ewmrs_render.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["goes_transform"]["properties"]["resampling"] = {"type": "string"}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    with _with_config_root(config_dir):
        loader.load_config("ewmrs_render")  # the schema no longer objects
        with pytest.raises(loader.ConfigError) as excinfo:
            goes_transform_resampling()
    assert "not supported for reprojection" in str(excinfo.value)
