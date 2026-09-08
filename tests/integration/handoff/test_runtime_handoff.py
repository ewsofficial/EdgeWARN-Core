"""Phase 2 durable-handoff contract tests.

Covers the failure modes called out in
plans/realtime-runner-decomposition-plan.md: crash between temp and rename,
malformed records, duplicate records, missing exact inputs, cleanup overlap
(deleted source), restarts, and backlog behavior.
"""

import json
import os
from datetime import datetime, timezone

import pytest

from common.ingest.manifest import CycleInputManifest, StagedInput
from util.runtime.handoff import (
    CONSUMER_CHECKPOINT_SCHEMA_VERSION,
    PHASE_RECORD_SCHEMA_VERSION,
    ConsumerCheckpointStore,
    PhaseRecord,
    PhaseRecordError,
    PhaseRecordPublisher,
    canonical_cycle_id,
    consumer_checkpoint_path,
    expected_layer_bindings,
    iter_committed_records,
    parse_cycle_id,
    phase_record_path,
    read_phase_record,
    select_pending_records,
    shadow_validate_phase_record,
)

CYCLE_DT = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
CYCLE_ID = "20240501T120000Z"


def make_manifest(tmp_path, *, filename="MergedReflectQC_00.50_20240101-120000.gz"):
    source = tmp_path / "mrms" / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"data")
    staged = StagedInput(
        product="MergedReflectQC",
        path=str(source),
        analysis_time=CYCLE_DT,
        source="mrms",
        family="mrms",
    )
    return CycleInputManifest(cycle_time=CYCLE_DT, inputs=(staged,))


class TestCycleIds:
    def test_canonical_cycle_id_round_trip(self):
        assert parse_cycle_id(canonical_cycle_id(CYCLE_DT)) == CYCLE_DT

    def test_parse_rejects_malformed_ids(self):
        with pytest.raises(ValueError):
            parse_cycle_id("not-a-cycle")


class TestPhaseRecordSchema:
    def test_from_manifest_and_round_trip(self):
        record = PhaseRecord.from_manifest(
            make_manifest_without_files(), phase="mrms-ready", run_id="run-1"
        )
        assert record.cycle_id == CYCLE_ID
        assert record.success is True
        assert record.as_dict()["schema_version"] == PHASE_RECORD_SCHEMA_VERSION
        restored = PhaseRecord.from_dict(record.as_dict())
        assert restored == record

    def test_missing_required_field_rejected(self):
        payload = PhaseRecord.from_manifest(
            make_manifest_without_files(), phase="rap-ready"
        ).as_dict()
        del payload["analysis_time"]
        with pytest.raises(PhaseRecordError):
            PhaseRecord.from_dict(payload)

    def test_unsupported_schema_version_rejected(self):
        payload = {"schema_version": 999}
        with pytest.raises(PhaseRecordError):
            PhaseRecord.from_dict(payload)

    def test_unknown_phase_name_rejected(self):
        manifest = make_manifest_without_files()
        with pytest.raises(PhaseRecordError):
            PhaseRecord.from_manifest(manifest, phase="goes-ready")

    def test_cycle_id_must_match_analysis_time(self):
        with pytest.raises(PhaseRecordError):
            PhaseRecord(
                cycle_id="19990101T000000Z",
                phase="mrms-ready",
                analysis_time=CYCLE_DT,
                published_at=CYCLE_DT,
            )


def make_manifest_without_files():
    return CycleInputManifest(cycle_time=CYCLE_DT)


class TestAtomicPublication:
    def test_commit_is_atomic_with_no_temp_siblings(self, tmp_path):
        publisher = PhaseRecordPublisher(tmp_path)
        committed = publisher.publish("mrms-ready", make_manifest(tmp_path))
        assert committed == phase_record_path(tmp_path, CYCLE_ID, "mrms-ready")
        assert [p.name for p in committed.parent.iterdir()] == ["mrms-ready.json"]

    def test_crash_between_temp_and_rename_leaves_no_record(self, tmp_path, monkeypatch):
        import util.atomic as atomic_module

        publisher = PhaseRecordPublisher(tmp_path)

        def explode(src, dst):
            raise OSError("crash before rename")

        monkeypatch.setattr(atomic_module.os, "replace", explode)
        with pytest.raises(OSError):
            publisher.publish("mrms-ready", make_manifest(tmp_path))
        cycle_directory = tmp_path / "state" / "realtime" / "cycles" / CYCLE_ID
        if cycle_directory.exists():
            assert list(cycle_directory.iterdir()) == []

    def test_crash_during_publication_leaves_other_records_intact(self, tmp_path, monkeypatch):
        import util.atomic as atomic_module

        publisher = PhaseRecordPublisher(tmp_path)
        first = publisher.publish("rap-ready", make_manifest(tmp_path))
        original_bytes = first.read_bytes()

        def explode(src, dst):
            raise OSError("crash before rename")

        monkeypatch.setattr(atomic_module.os, "replace", explode)
        later_manifest = CycleInputManifest(
            cycle_time=CYCLE_DT.replace(hour=13), inputs=()
        )
        with pytest.raises(OSError):
            publisher.publish("mrms-ready", later_manifest)
        assert first.read_bytes() == original_bytes
        later_dir = tmp_path / "state" / "realtime" / "cycles" / "20240501T130000Z"
        if later_dir.exists():
            assert list(later_dir.iterdir()) == []

    def test_duplicate_identical_publication_is_idempotent(self, tmp_path):
        publisher = PhaseRecordPublisher(tmp_path)
        manifest = make_manifest(tmp_path)
        first = publisher.publish("mrms-ready", manifest)
        second = publisher.publish("mrms-ready", manifest)
        assert first == second
        # Exactly one record file remains; no duplicate or attempt log.
        assert sorted(p.name for p in first.parent.iterdir()) == ["mrms-ready.json"]

    def test_incompatible_duplicate_is_refused_not_overwritten(self, tmp_path):
        logs = []
        publisher = PhaseRecordPublisher(tmp_path, log=logs.append)
        first = publisher.publish(
            "mrms-ready", make_manifest(tmp_path), warnings=("original",)
        )

        conflicting = make_manifest(tmp_path)
        second = publisher.publish("mrms-ready", conflicting, warnings=("different",))
        assert second is None
        assert any("Refusing to overwrite" in message for message in logs)
        re_read = read_phase_record(first)
        assert re_read.warnings == ("original",)

    def test_malformed_record_is_visible_but_unparseable(self, tmp_path):
        publisher = PhaseRecordPublisher(tmp_path)
        publisher.publish("mrms-ready", make_manifest(tmp_path))
        target = phase_record_path(tmp_path, CYCLE_ID, "mrms-ready")
        target.write_text("{not json at all")

        records = iter_committed_records(tmp_path, "mrms-ready")
        assert records == [(CYCLE_ID, None)]
        assert read_phase_record(target) is None


class TestExactPathConsumption:
    def test_shadow_validation_fails_when_source_file_removed(self, tmp_path):
        publisher = PhaseRecordPublisher(tmp_path)
        publisher.publish("mrms-ready", make_manifest(tmp_path))
        record = read_phase_record(phase_record_path(tmp_path, CYCLE_ID, "mrms-ready"))
        assert shadow_validate_phase_record(record) == ()

        # Cleanup overlap: the exact input is deleted before consumption.
        for staged in record.inputs:
            staged.local_path.unlink()
        problems = shadow_validate_phase_record(read_phase_record(
            phase_record_path(tmp_path, CYCLE_ID, "mrms-ready")
        ))
        assert any("missing exact input" in problem for problem in problems)

    def test_shadow_validation_reports_unbound_layers(self, tmp_path):
        publisher = PhaseRecordPublisher(tmp_path)
        publisher.publish("mrms-ready", make_manifest(tmp_path))
        record = read_phase_record(phase_record_path(tmp_path, CYCLE_ID, "mrms-ready"))
        layers = [{"name": "SomeLayer", "filepath": str(tmp_path / "empty-dir")}]
        problems = shadow_validate_phase_record(record, layers=layers)
        assert problems == ("SomeLayer: no manifest record pins a source file",)

    def test_expected_layer_bindings_mirror_pipeline_pinning(self, tmp_path):
        manifest = make_manifest(tmp_path)
        source_dir = str((tmp_path / "mrms"))
        bindings = expected_layer_bindings(
            manifest, [{"name": "MergedReflectQC", "filepath": source_dir}]
        )
        staged = manifest.inputs[0]
        assert bindings == {"MergedReflectQC": staged.path}

    def test_unsuccessful_record_is_flagged_by_shadow_validation(self, tmp_path):
        record = PhaseRecord(
            cycle_id=CYCLE_ID,
            phase="rap-ready",
            analysis_time=CYCLE_DT,
            published_at=CYCLE_DT,
            success=False,
        )
        problems = shadow_validate_phase_record(record)
        assert problems == ("rap-ready: record is marked unsuccessful",)


class TestConsumerCheckpoints:
    def test_store_round_trip_and_restart_persistence(self, tmp_path):
        store = ConsumerCheckpointStore(tmp_path, "ewmrs")
        assert store.load() is None
        checkpoint = store.record(CYCLE_ID)
        assert checkpoint.last_processed_cycle_id == CYCLE_ID
        assert store.load() == checkpoint
        payload = json.loads(consumer_checkpoint_path(tmp_path, "ewmrs").read_text())
        assert payload["schema_version"] == CONSUMER_CHECKPOINT_SCHEMA_VERSION

    def test_checkpoint_never_moves_backward(self, tmp_path):
        from util.runtime.handoff import PhaseRecordError

        store = ConsumerCheckpointStore(tmp_path, "ewmrs")
        store.record("20240501T120000Z")
        with pytest.raises(PhaseRecordError):
            store.record("20240501T110000Z")
        assert store.load().last_processed_cycle_id == "20240501T120000Z"

    def test_corrupt_checkpoint_fails_loudly(self, tmp_path):
        path = consumer_checkpoint_path(tmp_path, "ewmrs")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken")
        with pytest.raises(PhaseRecordError, match="corrupt consumer checkpoint"):
            ConsumerCheckpointStore(tmp_path, "ewmrs").load()


class TestBacklogSelection:
    def _commit_cycles(self, tmp_path, count, phase="mrms-ready"):
        publisher = PhaseRecordPublisher(tmp_path)
        committed_ids = []
        for hour in range(count):
            dt = CYCLE_DT.replace(hour=hour)
            publisher.publish(phase, make_manifest_without_files_for(dt))
            committed_ids.append(canonical_cycle_id(dt))
        return committed_ids

    def test_all_pending_within_cap(self, tmp_path):
        ids = self._commit_cycles(tmp_path, 3)
        selected = select_pending_records(tmp_path, "mrms-ready", max_backlog=10)
        assert [cycle_id for cycle_id, _, status in selected] == ids
        assert all(status == "pending" for _, _, status in selected)

    def test_backlog_excess_marks_oldest_abandoned(self, tmp_path):
        self._commit_cycles(tmp_path, 5)
        selected = select_pending_records(tmp_path, "mrms-ready", max_backlog=2)
        statuses = {cycle_id: status for cycle_id, _, status in selected}
        assert statuses["20240501T000000Z"] == "abandoned-backlog"
        assert statuses["20240501T010000Z"] == "abandoned-backlog"
        assert statuses["20240501T020000Z"] == "abandoned-backlog"
        assert statuses["20240501T030000Z"] == "pending"
        assert statuses["20240501T040000Z"] == "pending"

    def test_processed_cycles_are_excluded_after_checkpoint(self, tmp_path):
        self._commit_cycles(tmp_path, 3)
        checkpoint = ConsumerCheckpointStore(tmp_path, "ewmrs").record(
            "20240501T000000Z"
        )
        selected = select_pending_records(
            tmp_path, "mrms-ready", checkpoint=checkpoint, max_backlog=10
        )
        assert [cycle_id for cycle_id, _, status in selected if status == "pending"] == [
            "20240501T010000Z",
            "20240501T020000Z",
        ]

    def test_restart_resumes_at_oldest_still_valid_record(self, tmp_path):
        ids = self._commit_cycles(tmp_path, 4)
        # Simulate a crash mid-backlog: two cycles already consumed.
        checkpoint = ConsumerCheckpointStore(tmp_path, "ewmrs").record(ids[1])
        selected = select_pending_records(
            tmp_path, "mrms-ready", checkpoint=checkpoint, max_backlog=10
        )
        assert selected[-2][0] == ids[2]
        assert selected[-2][2] == "pending"
        assert all(status == "already-processed" for _, _, status in selected[:2])


def make_manifest_without_files_for(dt):
    return CycleInputManifest(cycle_time=dt)
