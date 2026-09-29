"""Atomic producer agreement for MRMS readers; contains no raw path authority."""
import json

from common.config.loader import load_config
from util.atomic import atomic_write_json
from util.runtime.services import classify_heartbeat_state, heartbeat_path, services_dir


class MrmsProducerUnavailable(RuntimeError):
    """MRMS scanning must wait for a matching, live Core producer."""


def descriptor_path(base_dir):
    return services_dir(base_dir) / "edgewarn-mrms-registry.json"


def publish_registry(registry, run_id):
    return atomic_write_json(descriptor_path(registry.base_dir), {
        "schema_version": 1,
        "contract_version": registry.contract_version,
        "fingerprint": registry.fingerprint,
        "products": sorted(p.product_id for p in registry.products),
        "run_id": run_id,
    })


def require_producer_agreement(registry, *, now=None):
    """Check each pass so restarts and repaired configuration recover automatically."""
    try:
        payload = json.loads(descriptor_path(registry.base_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MrmsProducerUnavailable("MRMS paused: Core registry descriptor missing/unreadable") from exc
    expected = {
        "schema_version": 1, "contract_version": registry.contract_version,
        "fingerprint": registry.fingerprint,
        "products": sorted(p.product_id for p in registry.products),
    }
    if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in expected.items()):
        raise MrmsProducerUnavailable("MRMS paused: Core/local registry mismatch; restart with matching configuration")
    import util.file as fs
    settings = load_config("api", config_dir=fs.MRMS_CONFIG_DIR)["server"]
    state, beat = classify_heartbeat_state(
        heartbeat_path(registry.base_dir, "edgewarn"),
        stale_after_seconds=settings["service_stale_after_seconds"], now=now,
    )
    if state not in ("active", "degraded") or beat is None or beat.service != "edgewarn" or beat.run_id != payload.get("run_id"):
        raise MrmsProducerUnavailable("MRMS paused: matching Core producer heartbeat unavailable/stale")
