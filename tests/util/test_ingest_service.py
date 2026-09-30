"""Ingest service resource, ledger, and producer-agreement contracts.

These are pure/unit-level: no timers run, no threads start, and nothing here
touches an operational runtime tree.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest
from common.config.loader import load_config
from common.ingest.mrms.acquisition import _sync_s3  # noqa: F401  (import parity)
from common.ingest.mrms.config import get_ingest_dependencies, get_ingest_settings
from common.ingest.mrms.registry import build_registry
from common.ingest.mrms.source import source_for
from common.config.mrms_products import parse_product_id
from common.ingest.mrms.source import DiscoveredObject
from util.runtime.ingest_service import (
    AcquisitionBacklogFull,
    AcquisitionJob,
    AcquisitionLedger,
    IngestResources,
)
from util.runtime.mrms_registry import (
    CORE_DESCRIPTOR_SCHEMA_VERSION,
    INGEST_DESCRIPTOR_SCHEMA_VERSION,
    MrmsProducerUnavailable,
    descriptor_path,
    ingest_descriptor_path,
    publish_ingest_registry,
    publish_registry,
    read_ingest_agreement,
    require_ingest_agreement,
    require_producer_agreement,
)
from util.runtime.services import ServiceHeartbeat, heartbeat_path, write_heartbeat

DEPENDENCY_FINGERPRINT = "c" * 64
UTC = timezone.utc


def registry(base="/runtime"):
    return build_registry(load_config("ingest")["mrms"], Path(base))


class TestIngestResources:
    def test_bounds_come_from_the_catalogs_only(self):
        assert get_ingest_dependencies().check
        resolved = IngestResources.resolve()
        inherited = get_ingest_settings()
        scheduler = load_config("scheduler")["scheduler"]
        assert resolved.poll_seconds == scheduler["ingest_poll_seconds"]
        assert resolved.lookback_hours == scheduler["s3_lookback_hours"]
        assert resolved.listing_concurrency == inherited["listing_concurrency"]
        assert resolved.download_concurrency == inherited["download_concurrency"]
        assert resolved.retention_minutes == load_config("ingest")["mrms"]["cleanup_max_age_minutes"]
        assert resolved.rap_max_files == load_config("synoptic_rap")["rap"]["max_files"]

    def test_retry_delay_is_bounded_exponential_backoff(self):
        resolved = IngestResources.resolve()
        first = resolved.retry_delay(1)
        second = resolved.retry_delay(2)
        assert first == resolved.retry_initial_seconds
        assert second == min(resolved.retry_max_seconds, resolved.retry_initial_seconds * 2)
        assert resolved.retry_delay(99) == resolved.retry_max_seconds

    def test_reserved_slots_never_consume_the_whole_window(self):
        assert IngestResources.resolve().reserved_slots == 1
        single = IngestResources.resolve().with_overrides(download_concurrency=1)
        assert single.reserved_slots == 0
        wide = IngestResources.resolve().with_overrides(download_concurrency=4)
        assert wide.reserved_slots == 1

    @pytest.mark.parametrize('overrides', [
        {'poll_seconds': 0},
        {'download_concurrency': 0},
        {'pending_max_jobs': 0},
        {'check_reserved_slots': -1},
    ])
    def test_invalid_bounds_are_rejected(self, overrides):
        with pytest.raises(ValueError):
            IngestResources.resolve().with_overrides(**overrides)


class TestAcquisitionLedger:
    def job(self, identity, protected=False, at=None):
        return AcquisitionJob(identity=identity, kind="mrms", product_id=identity.split(":")[-1],
                              target=at or datetime(2026, 1, 1, tzinfo=UTC), protected=protected)

    def test_duplicate_discoveries_create_no_extra_job(self):
        ledger = AcquisitionLedger(capacity=8)
        assert ledger.offer(self.job("mrms:A|2026-01-01T00:00:00+00:00")) is True
        assert ledger.offer(self.job("mrms:A|2026-01-01T00:00:00+00:00")) is False
        assert ledger.counts()["pending_jobs"] == 1

    def test_due_orders_check_inputs_first_then_oldest_observation(self):
        ledger = AcquisitionLedger(capacity=8)
        later = self.job("mrms:B", at=datetime(2026, 1, 1, 0, 2, tzinfo=UTC))
        earlier = self.job("mrms:C", at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
        check_later = self.job("mrms:D", protected=True, at=datetime(2026, 1, 1, 0, 4, tzinfo=UTC))
        for job in (later, earlier, check_later):
            ledger.offer(job)
        assert [job.identity for job in ledger.due(0.0)] == [
            "mrms:D", "mrms:C", "mrms:B"]

    def test_backlog_is_bounded_and_reported_explicitly(self):
        ledger = AcquisitionLedger(capacity=2)
        ledger.offer(self.job("a"))
        ledger.offer(self.job("b"))
        with pytest.raises(AcquisitionBacklogFull):
            ledger.offer(self.job("c"))
        assert ledger.counts()["rejected_objects"] == 1

    def test_failure_is_remembered_separately_from_success(self):
        ledger = AcquisitionLedger(capacity=8)
        ledger.take("mrms:x")
        ledger.settle("mrms:x", "committed")
        assert ledger.state("mrms:x") == ("committed", "")
        ledger.take("mrms:y")
        ledger.settle("mrms:y", "abandoned", "timeout")
        assert ledger.state("mrms:y") == ("abandoned", "timeout")
        counts = ledger.counts()
        assert counts["committed_objects"] == 1
        assert counts["failed_objects"] == 1

    def test_retry_requeues_without_a_second_job(self):
        ledger = AcquisitionLedger(capacity=8)
        ledger.offer(self.job("mrms:z"))
        job = ledger.take("mrms:z")
        ledger.requeue(job, delay=5.0)
        assert ledger.due(0.0) == ()
        assert [item.identity for item in ledger.due(1e9)] == ["mrms:z"]

    def test_settled_memory_is_bounded(self):
        ledger = AcquisitionLedger(capacity=2)
        for name in ("a", "b", "c"):
            ledger.settle(name, "committed")
        assert len(ledger._settled) == 2
        assert ledger.state("a") is None


class TestIngestDescriptorContract:
    def test_ingest_descriptor_is_separate_from_the_core_descriptor(self, tmp_path):
        effective = registry(tmp_path)
        publish_registry(effective, "core-run")
        publish_ingest_registry(effective, "ingest-run",
                                dependency_fingerprint=DEPENDENCY_FINGERPRINT)
        assert json.loads(descriptor_path(tmp_path).read_text())["schema_version"] == \
            CORE_DESCRIPTOR_SCHEMA_VERSION
        assert json.loads(ingest_descriptor_path(tmp_path).read_text())["schema_version"] == \
            INGEST_DESCRIPTOR_SCHEMA_VERSION
        assert read_ingest_agreement(effective, DEPENDENCY_FINGERPRINT) == "ingest-run"

    def test_a_legacy_core_descriptor_cannot_satisfy_the_ingest_agreement(self, tmp_path):
        effective = registry(tmp_path)
        publish_registry(effective, "core-run")
        with pytest.raises(MrmsProducerUnavailable, match="independent ingest descriptor"):
            read_ingest_agreement(effective, DEPENDENCY_FINGERPRINT)

    def test_dependency_fingerprint_mismatch_fails_visibly(self, tmp_path):
        effective = registry(tmp_path)
        publish_ingest_registry(effective, "ingest-run", dependency_fingerprint=DEPENDENCY_FINGERPRINT)
        with pytest.raises(MrmsProducerUnavailable, match="mismatch"):
            read_ingest_agreement(effective, "d" * 64)

    def test_registry_fingerprint_mismatch_fails_visibly(self, tmp_path):
        from common.ingest.mrms.registry import _plain

        effective = registry(tmp_path)
        publish_ingest_registry(effective, "ingest-run", dependency_fingerprint=DEPENDENCY_FINGERPRINT)
        changed = _plain(load_config("ingest")["mrms"])
        changed["downloads"]["max_concurrency"] = 3
        other = build_registry(changed, tmp_path)
        assert other.fingerprint != effective.fingerprint
        with pytest.raises(MrmsProducerUnavailable, match="mismatch"):
            read_ingest_agreement(other, DEPENDENCY_FINGERPRINT)

    def test_agreement_requires_the_matching_live_producer_heartbeat(self, tmp_path):
        import util.file as fs

        fs.initialize_filesystem(tmp_path)
        effective = registry(tmp_path)
        publish_ingest_registry(effective, "ingest-run", dependency_fingerprint=DEPENDENCY_FINGERPRINT)
        with pytest.raises(MrmsProducerUnavailable, match="heartbeat"):
            require_ingest_agreement(effective, DEPENDENCY_FINGERPRINT)
        write_heartbeat(ServiceHeartbeat("ingest", 4242, "ingest-run", datetime.now(UTC)),
                        heartbeat_path(tmp_path, "ingest"))
        assert require_ingest_agreement(effective, DEPENDENCY_FINGERPRINT) == "ingest-run"

    def test_a_stale_producer_run_id_is_rejected(self, tmp_path):
        import util.file as fs

        fs.initialize_filesystem(tmp_path)
        effective = registry(tmp_path)
        publish_ingest_registry(effective, "ingest-run", dependency_fingerprint=DEPENDENCY_FINGERPRINT)
        write_heartbeat(ServiceHeartbeat("ingest", 4242, "previous-run", datetime.now(UTC)),
                        heartbeat_path(tmp_path, "ingest"))
        with pytest.raises(MrmsProducerUnavailable, match="heartbeat"):
            require_ingest_agreement(effective, DEPENDENCY_FINGERPRINT)

    def test_the_core_agreement_still_keys_off_the_core_heartbeat(self, tmp_path):
        import util.file as fs

        fs.initialize_filesystem(tmp_path)
        effective = registry(tmp_path)
        publish_registry(effective, "core-run")
        with pytest.raises(MrmsProducerUnavailable, match="Core"):
            require_producer_agreement(effective)


def discovered(spec, at, *, transport="s3"):
    source = source_for(parse_product_id("MRMS_" + spec.product_id))
    name = (f"MRMS_PROBSEVERE_{at:%Y%m%d_%H%M%S}.json" if spec.adapter == "probsevere_json"
            else f"MRMS_{spec.product_id}_{at:%Y%m%d-%H%M%S}.grib2.gz")
    locator = (source.s3_prefix(at) if transport == "s3" else source.https_url + "/") + name
    return DiscoveredObject(spec.product_id, at, transport, locator)


def test_discovered_object_helper_matches_the_source_grammar():
    effective = registry()
    for spec in effective.products:
        obj = discovered(spec, datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
        assert obj.product_id == spec.product_id
        assert obj.acquisition_identity[2] == "s3"
        assert hashlib.sha256(str(obj.logical_identity).encode()).hexdigest()
        mirror = obj.mirror("https")
        assert mirror.logical_identity == obj.logical_identity
