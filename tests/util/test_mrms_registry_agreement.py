"""Producer restart, mismatch, stale heartbeat, and recovery contracts."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from common.ingest.mrms.registry import build_registry
from util.runtime.mrms_registry import (
    MrmsProducerUnavailable, descriptor_path, publish_registry, require_producer_agreement,
)
from util.runtime.services import ServiceHeartbeat, heartbeat_path, write_heartbeat


def test_agreement_requires_matching_live_generation_and_recovers(tmp_path):
    registry = build_registry({"products": []}, tmp_path)
    now = datetime.now(timezone.utc)
    with pytest.raises(MrmsProducerUnavailable, match="descriptor"):
        require_producer_agreement(registry, now=now)
    publish_registry(registry, "first")
    payload = json.loads(descriptor_path(tmp_path).read_text())
    assert "directory" not in str(payload)
    with pytest.raises(MrmsProducerUnavailable, match="heartbeat"):
        require_producer_agreement(registry, now=now)
    def beat(run_id, updated_at):
        write_heartbeat(ServiceHeartbeat("edgewarn", 123, run_id, updated_at),
                        heartbeat_path(tmp_path, "edgewarn"))
    beat("first", now)
    require_producer_agreement(registry, now=now)
    beat("second", now)
    with pytest.raises(MrmsProducerUnavailable, match="heartbeat"):
        require_producer_agreement(registry, now=now)
    publish_registry(registry, "second")
    require_producer_agreement(registry, now=now)
    beat("second", now - timedelta(hours=1))
    with pytest.raises(MrmsProducerUnavailable, match="stale"):
        require_producer_agreement(registry, now=now)
    beat("second", now)
    other = build_registry({"products": ["MRMS_MESH_00.50"]}, tmp_path)
    with pytest.raises(MrmsProducerUnavailable, match="mismatch"):
        require_producer_agreement(other, now=now)
    publish_registry(other, "second")
    require_producer_agreement(other, now=now)
