"""Atomic producer agreement for MRMS readers; contains no raw path authority.

Two descriptor contracts coexist during the coordinated cutover:

``core`` (schema 1, ``edgewarn-mrms-registry.json``)
    The historical Core-owned acquisition descriptor. It proves only that a
    Core process published an MRMS registry. It is retained for the explicit
    drain step and is **not** a valid ingestor identity.

``ingest`` (schema 2, ``ingest-mrms-registry.json``)
    The independent producer's descriptor. A separate destination file and a
    separate schema version mean an old Core descriptor can never satisfy an
    ingest agreement check, and a Core restart cannot be mistaken for a live
    ingestor. The producer is additionally bound to the ``ingest`` run ID and
    heartbeat so a stale descriptor from a crashed producer is rejected.

Both contracts validate the effective registry fingerprint, so a configuration
change on either side fails visibly instead of silently weakening a gate.
"""
import json

from common.config.loader import load_config
from util.atomic import atomic_write_json
from util.runtime.services import classify_heartbeat_state, heartbeat_path, services_dir

CORE_DESCRIPTOR_SCHEMA_VERSION = 1
INGEST_DESCRIPTOR_SCHEMA_VERSION = 2
INGEST_DESCRIPTOR_NAME = "ingest-mrms-registry.json"
INGEST_PRODUCER_SERVICE = "ingest"


class MrmsProducerUnavailable(RuntimeError):
    """MRMS scanning must wait for a matching, live producer."""


def descriptor_path(base_dir):
    return services_dir(base_dir) / "edgewarn-mrms-registry.json"


def ingest_descriptor_path(base_dir):
    return services_dir(base_dir) / INGEST_DESCRIPTOR_NAME


def _products(registry):
    return sorted(p.product_id for p in registry.products)


def publish_registry(registry, run_id):
    return atomic_write_json(descriptor_path(registry.base_dir), {
        "schema_version": CORE_DESCRIPTOR_SCHEMA_VERSION,
        "contract_version": registry.contract_version,
        "fingerprint": registry.fingerprint,
        "products": _products(registry),
        "run_id": run_id,
    })


def publish_ingest_registry(registry, run_id, *, dependency_fingerprint):
    """Publish the independent producer's descriptor for consumer agreement.

    ``dependency_fingerprint`` is the resolved ``IngestDependencies`` identity,
    not just the MRMS registry fingerprint: consumers must fail visibly when
    the effective check/detection/integration sets differ.
    """
    from common.ingest.mrms.core_contract import CORE_CONTRACT_VERSION
    return atomic_write_json(ingest_descriptor_path(registry.base_dir), {
        "schema_version": INGEST_DESCRIPTOR_SCHEMA_VERSION,
        "producer_service": INGEST_PRODUCER_SERVICE,
        "contract_version": CORE_CONTRACT_VERSION,
        "fingerprint": registry.fingerprint,
        "dependency_fingerprint": dependency_fingerprint,
        "products": _products(registry),
        "run_id": run_id,
    })


def require_producer_agreement(registry, *, now=None):
    """Check each pass so restarts and repaired configuration recover automatically."""
    try:
        payload = json.loads(descriptor_path(registry.base_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MrmsProducerUnavailable("MRMS paused: Core registry descriptor missing/unreadable") from exc
    expected = {
        "schema_version": CORE_DESCRIPTOR_SCHEMA_VERSION, "contract_version": registry.contract_version,
        "fingerprint": registry.fingerprint,
        "products": _products(registry),
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


def read_ingest_agreement(registry, dependency_fingerprint):
    """Parse the ingest descriptor without consulting any heartbeat.

    Raises :class:`MrmsProducerUnavailable` for a missing, legacy, or
    mismatched descriptor. The returned run ID identifies the producer whose
    heartbeat :func:`require_ingest_agreement` must then confirm.
    """
    try:
        payload = json.loads(ingest_descriptor_path(registry.base_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MrmsProducerUnavailable(
            "Ingest paused: independent ingest descriptor missing/unreadable; start run_ingest.py"
        ) from exc
    expected = {
        "schema_version": INGEST_DESCRIPTOR_SCHEMA_VERSION,
        "producer_service": INGEST_PRODUCER_SERVICE,
        "contract_version": registry.contract_version,
        "fingerprint": registry.fingerprint,
        "dependency_fingerprint": dependency_fingerprint,
        "products": _products(registry),
    }
    if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in expected.items()):
        raise MrmsProducerUnavailable(
            "Ingest paused: local/effective ingest agreement mismatch; "
            "restart the producer with matching configuration"
        )
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise MrmsProducerUnavailable("Ingest paused: descriptor is missing a producer run ID")
    return run_id


def require_ingest_agreement(registry, dependency_fingerprint, *, now=None):
    """Confirm the descriptor *and* the live producer heartbeat.

    Checked on each consumption pass so an ingestor restart or a repaired
    configuration recovers automatically. A heartbeat is poll liveness only;
    it never substitutes for a locally valid input.
    """
    run_id = read_ingest_agreement(registry, dependency_fingerprint=dependency_fingerprint)
    import util.file as fs
    settings = load_config("api", config_dir=fs.MRMS_CONFIG_DIR)["server"]
    state, beat = classify_heartbeat_state(
        heartbeat_path(registry.base_dir, INGEST_PRODUCER_SERVICE),
        stale_after_seconds=settings["service_stale_after_seconds"], now=now,
    )
    if (state not in ("active", "degraded") or beat is None
            or beat.service != INGEST_PRODUCER_SERVICE or beat.run_id != run_id):
        raise MrmsProducerUnavailable(
            "Ingest paused: matching independent ingest heartbeat unavailable/stale"
        )
    return run_id
