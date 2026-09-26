import pytest
from unittest.mock import patch
from datetime import datetime, timezone
from EdgeWARN.schedule.scheduler import MRMSUpdateChecker
import EdgeWARN.schedule.scheduler as scheduler_module

# Sample Timestamps
TS_OLD = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TS_NEW = datetime(2023, 1, 1, 12, 2, 0, tzinfo=timezone.utc)
TS_NEWER = datetime(2023, 1, 1, 12, 4, 0, tzinfo=timezone.utc)

@pytest.fixture
def update_checker(mock_io_manager):
    """Fixture for initialized MRMSUpdateChecker."""
    # We patch the module-level io_manager to suppress output during tests
    with patch("EdgeWARN.schedule.scheduler.io_manager", mock_io_manager):
        yield MRMSUpdateChecker(verbose=True)

def test_latest_common_minute_intersection(update_checker, mocker):
    """Test finding common timestamp across modifiers."""
    # Mock _get_modifier_times to return sets
    # Mod 1 has [OLD, NEW]
    # Mod 2 has [NEW, NEWER]
    # Common is NEW
    
    def side_effect(mod, ref_dt, trace_id=None, last_processed=None,
                    s3_bucket=None, max_entries=None):
        if mod[1] == "Mod1":
            return {TS_OLD, TS_NEW}
        else:
            return {TS_NEW, TS_NEWER}
            
    mocker.patch.object(update_checker, "_get_modifier_times", side_effect=side_effect)
    
    modifiers = [("R", "Mod1", "D"), ("R", "Mod2", "D")]
    common = update_checker.latest_common_minute_1h(modifiers)
    
    assert common == TS_NEW


def test_latest_common_minute_logs_only_when_timestamp_advances(update_checker, mocker):
    mocker.patch.object(update_checker, "_get_modifier_times", return_value={TS_NEW})

    modifiers = [("R", "Mod1", "D"), ("R", "Mod2", "D")]
    with patch("builtins.print") as mock_print:
        common = update_checker.latest_common_minute_1h(modifiers, last_processed=TS_OLD)

    assert common == TS_NEW
    mock_print.assert_any_call(f"[Scheduler] Latest common timestamp updated: {TS_NEW}")

def test_latest_common_minute_no_intersection(update_checker, mocker):
    """Test behavior when no common timestamps exist."""
    def side_effect(mod, ref_dt, trace_id=None, last_processed=None,
                    s3_bucket=None, max_entries=None):
        if mod[1] == "Mod1":
            return {TS_OLD}
        else:
            return {TS_NEWER} # Disjoint
            
    mocker.patch.object(update_checker, "_get_modifier_times", side_effect=side_effect)
    
    modifiers = [("R", "Mod1", "D"), ("R", "Mod2", "D")]
    common = update_checker.latest_common_minute_1h(modifiers)
    
    assert common is None


@pytest.mark.parametrize(
    ("source_seconds", "expected_cycle"),
    [
        ("004040", TS_OLD.replace(hour=0, minute=40)),
        ("004239", TS_OLD.replace(hour=0, minute=42)),
    ],
)
def test_source_seconds_do_not_shift_scheduler_cycle(
    update_checker, monkeypatch, source_seconds, expected_cycle
):
    cursor = datetime(2023, 1, 1, 0, 40, tzinfo=timezone.utc)
    source = datetime.strptime(
        f"20230101{source_seconds}", "%Y%m%d%H%M%S"
    ).replace(tzinfo=timezone.utc)

    class Finder:
        def __init__(self, *_args, **_kwargs):
            pass

        def lookup_files(self, *_args, **_kwargs):
            return [(f"MRMS_20230101-{source_seconds}.grib2.gz", source)]

    monkeypatch.setattr(scheduler_module, "FileFinder", Finder)
    monkeypatch.setattr(scheduler_module, "parse_mrms_bucket_path", lambda *_: "MRMS/")
    actual = update_checker._get_modifier_times(
        ("CONUS", "MergedReflectivityQCComposite", "unused"),
        cursor.replace(minute=44),
        last_processed=cursor,
        s3_bucket="unused",
        max_entries=2,
    )

    assert actual == {expected_cycle}
    assert (max(actual) > cursor) == (expected_cycle > cursor)
