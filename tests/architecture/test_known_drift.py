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
    # Bounded by the 60-minute raw-input retention (catalog invariant).
    assert s3_lookback_hours() == recorded["s3_lookback_hours"] == 1
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


# --- METAR: the one subsystem that really does skip TLS verification -------


def _metar_source() -> str:
    return (REPO_ROOT / "src/common/ingest/metar.py").read_text(encoding="utf-8")


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


# --- zone_sync pause_seconds: flag default fights constructor default ------


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


# --- Import-time binding hazards -----------------------------------------


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


# --- MRMS/GOES: --config-dir could not reach the ingest catalog ------------


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
        assert mrms_config.mrms_bucket() == "noaa-mrms-pds"
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
        def __init__(self, *, timeout):
            assert timeout.total == https_client.ncep_sync_timeout_seconds()

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
        assert finder.construct_url("CONUS", "MESH_00.50") == "https://mrms.ncep.noaa.gov/data/2D/MESH"

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
            def __init__(self, *, timeout):
                assert timeout.total == https_client.ncep_sync_timeout_seconds()

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


# --- The nws block: four values the catalog only documented -----------------


# --- the api.weather.gov Accept header: one owner across two subsystems ----


# --- the async alert download's chunk size ---------------------------------


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
