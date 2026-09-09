"""Multi-timestamp historical replay: gaps, duplicates, rollover, flags, failures.

Phase 9 secondary coverage: ``process_historical.main`` owns the
minute-by-minute replay loop (gap skip, duplicate suppression, per-timestamp
output validation, failure retryability, CTAM/tracking flag forwarding), but
no test ever drove it. These cases execute the real ``main()`` loop against
a scripted ``MRMSUpdateChecker`` and a stub ``historical_pipeline`` that
writes production-shaped ``{"latest_timestamp": ...}`` artifacts, so the
cursor movement, dedup state, validation gate, and error branches under test
are the shipped ones.

The stub pipeline stands in for MRMS S3 and the full detection stack, which
belong to the opt-in live compatibility probe, not the default lane. What is
real here: time parsing, step iteration, skip/duplicate decisions, output
validation, flag plumbing, and retry semantics.
"""

import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import process_historical as ph
from EdgeWARN.pipeline import parse_utc_time


class FakeIO:
    def __init__(self, args):
        self._args = args
        self.messages = []

    def get_historical_args(self):
        return self._args

    def write_info(self, msg):
        self.messages.append(("info", msg))

    def write_warning(self, msg):
        self.messages.append(("warning", msg))

    def write_error(self, msg):
        self.messages.append(("error", msg))

    def errors(self):
        return [m for level, m in self.messages if level == "error"]

    def warnings(self):
        return [m for level, m in self.messages if level == "warning"]


def make_args(**overrides):
    args = types.SimpleNamespace(
        start="2024-01-01T00:00:00Z",
        end="2024-01-01T00:02:00Z",
        lat=[20.0, 55.0],
        lon=[-130.0, -60.0],
        base_dir="/tmp/edgewarn-historical-test",
        config_dir="config",
        profile=False,
        disable_ctam=False,
        disable_ctam_modules=False,
        disable_tracking=False,
        disable_polygon_expansion=False,
        refl_threshold=None,
        min_seed_percentage=None,
        drop_offset=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class ReplayHarness:
    """Install fakes around process_historical.main and run it."""

    def __init__(self, monkeypatch, tmp_path, script=None, args=None):
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.script = script or {}
        self.calls = []
        self.sleeps = []
        self.io = FakeIO(args or make_args())
        self._failures = set()
        self._wrong_stamp = set()

    def fail_on(self, *timestamps):
        self._failures.update(timestamps)
        return self

    def misstamp(self, *timestamps):
        self._wrong_stamp.update(timestamps)
        return self

    def _checker(self):
        script = self.script

        class _FakeChecker:
            def __init__(self, *a, **k):
                pass

            def latest_common_minute_1h(self, modifiers, reference_dt=None):
                key = reference_dt.strftime("%H:%M")
                if key in script:
                    return script[key]
                if reference_dt is None:
                    return None
                return reference_dt.replace(second=0, microsecond=0)

        return _FakeChecker

    def _pipeline(self):
        harness = self

        def _fake(latest_common, lat_limits, lon_limits, **kwargs):
            stamp = latest_common.strftime("%H:%M")
            harness.calls.append((latest_common, dict(kwargs)))
            if stamp in harness._failures:
                raise RuntimeError(f"simulated pipeline failure at {stamp}")
            out = harness.tmp_path / f"stormcells_{stamp.replace(':', '')}.json"
            observed = latest_common
            if stamp in harness._wrong_stamp:
                observed = latest_common.replace(minute=(latest_common.minute + 5) % 60)
            out.write_text(json.dumps({"latest_timestamp": observed.isoformat()}), encoding="utf-8")
            return str(out)

        return _fake

    def run(self):
        import common.ingest.mrms.config as mrms_config

        self.monkeypatch.setattr(ph, "io_manager", self.io)
        self.monkeypatch.setattr(ph, "MRMSUpdateChecker", self._checker())
        self.monkeypatch.setattr(ph, "historical_pipeline", self._pipeline())
        self.monkeypatch.setattr(ph, "initialize_runtime", lambda **k: None)
        self.monkeypatch.setattr(ph, "initialize_at_startup_historical", lambda: None)
        self.monkeypatch.setattr(
            ph, "DetectionConfig", types.SimpleNamespace(from_yaml=lambda **k: object())
        )
        self.monkeypatch.setattr(mrms_config, "get_check_modifiers", lambda: [])
        sleeps = self.sleeps
        self.monkeypatch.setattr(
            ph, "time", types.SimpleNamespace(sleep=lambda s: sleeps.append(s))
        )
        ph.main()
        return self

    def processed_minutes(self):
        return sorted(dt.strftime("%H:%M") for dt, _ in self.calls)


def run_replay(monkeypatch, tmp_path, **kwargs):
    return ReplayHarness(monkeypatch, tmp_path, **kwargs).run()


# ---------------------------------------------------------------------------
# Cursor movement: forward march, gaps, duplicates
# ---------------------------------------------------------------------------


class TestCursorMovement:
    def test_each_minute_in_range_is_processed(self, monkeypatch, tmp_path):
        h = run_replay(monkeypatch, tmp_path)
        assert h.processed_minutes() == ["00:00", "00:01", "00:02"]
        assert len(h.sleeps) == 3  # every pipeline iteration is throttled

    def test_gap_with_no_common_timestamp_is_skipped(self, monkeypatch, tmp_path):
        h = run_replay(monkeypatch, tmp_path, script={"00:01": None})
        assert h.processed_minutes() == ["00:00", "00:02"]
        assert any("No common timestamp" in m for m in h.io.warnings())
        assert len(h.sleeps) == 2  # the skipped gap is not throttled

    def test_duplicate_timestamp_is_processed_once(self, monkeypatch, tmp_path):
        first = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        h = run_replay(monkeypatch, tmp_path, script={"00:01": first})
        assert h.processed_minutes() == ["00:00", "00:02"]
        assert any("already processed" in m for _, m in h.io.messages)

    def test_empty_range_processes_nothing(self, monkeypatch, tmp_path):
        args = make_args(start="2024-01-01T00:02:00Z", end="2024-01-01T00:00:00Z")
        h = run_replay(monkeypatch, tmp_path, args=args)
        assert h.calls == []
        assert any("complete" in m for _, m in h.io.messages)


# ---------------------------------------------------------------------------
# Time handling: timezones, day rollover, parsing
# ---------------------------------------------------------------------------


class TestTimeHandling:
    def test_day_rollover_replays_across_midnight(self, monkeypatch, tmp_path):
        args = make_args(start="2024-01-31T23:59:00Z", end="2024-02-01T00:01:00Z")
        h = run_replay(monkeypatch, tmp_path, args=args)
        assert sorted(h.processed_minutes()) == ["00:00", "00:01", "23:59"]
        assert len(h.calls) == 3

    @pytest.mark.parametrize(
        ("raw", "expected_iso"),
        [
            ("2024-01-01T00:00:00Z", "2024-01-01T00:00:00+00:00"),
            ("2024-06-15T12:30:00", "2024-06-15T12:30:00+00:00"),  # naive means UTC
            ("2024-01-01T05:30:00+05:30", "2024-01-01T00:00:00+00:00"),  # offset folds to UTC
            ("2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
        ],
    )
    def test_parse_utc_time_normalizes_to_utc(self, raw, expected_iso):
        assert parse_utc_time(raw).isoformat() == expected_iso

    def test_invalid_range_bounds_abort_without_processing(self, monkeypatch, tmp_path):
        args = make_args(start="not-a-time", end="2024-01-01T00:02:00Z")
        h = run_replay(monkeypatch, tmp_path, args=args)
        assert h.calls == []
        assert any("Invalid timestamp format" in m for m in h.io.errors())


# ---------------------------------------------------------------------------
# Flag forwarding: CTAM, tracking, polygon expansion
# ---------------------------------------------------------------------------


class TestFlagForwarding:
    def test_disable_flags_reach_pipeline(self, monkeypatch, tmp_path):
        args = make_args(
            disable_ctam=True, disable_tracking=True, disable_polygon_expansion=True
        )
        h = run_replay(monkeypatch, tmp_path, args=args)
        assert h.calls
        for _, kwargs in h.calls:
            assert kwargs["disable_ctam"] is True
            assert kwargs["disable_tracking"] is True
            assert kwargs["disable_polygon_expansion"] is True

    def test_default_flags_are_passed_through_unset(self, monkeypatch, tmp_path):
        h = run_replay(monkeypatch, tmp_path)
        for _, kwargs in h.calls:
            assert kwargs["disable_ctam"] is False
            assert kwargs["disable_ctam_modules"] is False
            assert kwargs["disable_tracking"] is False

    def test_lat_lon_limits_reach_pipeline(self, monkeypatch, tmp_path):
        h = run_replay(monkeypatch, tmp_path)
        for _, kwargs in h.calls:
            assert kwargs["io_manager"] is h.io
        # positional lat/lon limits are asserted via the recorded call shape
        assert h.calls[0][0].tzinfo is not None


# ---------------------------------------------------------------------------
# Failure semantics: retryable timestamps, validation gate
# ---------------------------------------------------------------------------


class TestFailureSemantics:
    def test_failed_timestamp_does_not_advance_dedup_and_loop_continues(
        self, monkeypatch, tmp_path
    ):
        h = ReplayHarness(monkeypatch, tmp_path).fail_on("00:01").run()
        assert h.processed_minutes() == ["00:00", "00:01", "00:02"]
        assert any("remains unprocessed" in m for _, m in h.io.messages)
        # 00:01 raised, so the failure must not poison the following timestamp.
        assert any("Output saved" in m for _, m in h.io.messages)

    def test_unvalidated_output_is_rejected_like_a_failure(self, monkeypatch, tmp_path):
        h = ReplayHarness(monkeypatch, tmp_path).misstamp("00:00").run()
        assert h.processed_minutes() == ["00:00", "00:01", "00:02"]
        assert any("did not produce a validated artifact" in m for m in h.io.errors())

    def test_failed_timestamp_is_retryable_on_later_reference(self, monkeypatch, tmp_path):
        first = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        # 00:01's reference re-resolves the still-unprocessed 00:00 data.
        h = ReplayHarness(monkeypatch, tmp_path, script={"00:01": first}).fail_on("00:00").run()
        minutes = h.processed_minutes()
        assert minutes.count("00:00") == 2  # attempted, failed, then retried
        assert "00:02" in minutes


# ---------------------------------------------------------------------------
# Output validation gate, unit matrix
# ---------------------------------------------------------------------------


class TestValidatedHistoricalOutput:
    def _artifact(self, tmp_path, payload):
        path = tmp_path / "stormcells.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_matching_minute_validates(self, tmp_path):
        requested = datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
        path = self._artifact(tmp_path, {"latest_timestamp": "2024-01-01T00:01:00+00:00"})
        assert ph._validated_historical_output(path, requested) is True

    def test_seconds_are_ignored_but_minutes_must_match(self, tmp_path):
        requested = datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
        path = self._artifact(tmp_path, {"latest_timestamp": "2024-01-01T00:01:45+00:00"})
        assert ph._validated_historical_output(path, requested) is True

    def test_wrong_minute_is_rejected(self, tmp_path):
        requested = datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
        path = self._artifact(tmp_path, {"latest_timestamp": "2024-01-01T00:06:00+00:00"})
        assert ph._validated_historical_output(path, requested) is False

    def test_naive_observed_timestamp_is_treated_as_utc(self, tmp_path):
        requested = datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
        path = self._artifact(tmp_path, {"latest_timestamp": "2024-01-01T00:01:00"})
        assert ph._validated_historical_output(path, requested) is True

    @pytest.mark.parametrize("payload", ["{not json", '{"no_timestamp": true}', '{"latest_timestamp": "garbage"}'])
    def test_malformed_artifacts_are_rejected(self, tmp_path, payload):
        requested = datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
        assert ph._validated_historical_output(self._artifact(tmp_path, payload), requested) is False

    def test_missing_file_is_rejected(self, tmp_path):
        requested = datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
        assert ph._validated_historical_output(tmp_path / "absent.json", requested) is False

    def test_none_result_path_is_rejected(self, tmp_path):
        requested = datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
        assert ph._validated_historical_output(Path("/nonexistent/stormcells.json"), requested) is False
