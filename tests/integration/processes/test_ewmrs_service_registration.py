"""Registration contract tests for the EWMRS service child set.

The GOES Render and EWMRS Consumer children each run a render pipeline that
spawns a ``ProcessPoolExecutor``. Python forbids daemonic processes from
having children, so those two entries must always be registered
non-daemonic; the pure ingest loops have no such requirement.
"""

import threading

from util.runtime.ewmrs_service import register_ewmrs_accessories

POOL_SPAWNING_CHILDREN = ("GOES Render", "EWMRS Consumer")
PURE_INGEST_CHILDREN = ("METAR", "NWS", "WPC", "GOES")


def _registered_children():
    supervisor_calls = []

    class RecordingSupervisor:
        def add(self, name, target, **kwargs):
            entry = {"name": name, "target": target}
            entry.update(kwargs)
            supervisor_calls.append(entry)
            return entry

    events = (threading.Event(), threading.Event())
    register_ewmrs_accessories(
        RecordingSupervisor(),
        base_dir="/tmp/unused",
        metar_enabled=False,
        nws_enabled=False,
        wpc_enabled=False,
        goes_ingest_enabled=False,
        goes_render_enabled=False,
        consumer_enabled=False,
        goes_cycle_active=events[0],
        goes_render_active=events[1],
        goes_pause_ingest_during_render=False,
        goes_poll_seconds=60,
        child_log_queue=object(),
    )
    return {entry["name"]: entry for entry in supervisor_calls}


def test_all_children_registered():
    children = _registered_children()
    for name in POOL_SPAWNING_CHILDREN + PURE_INGEST_CHILDREN:
        assert name in children


def test_pool_spawning_children_are_non_daemonic():
    children = _registered_children()
    for name in POOL_SPAWNING_CHILDREN:
        assert children[name]["daemon"] is False, (
            f"{name} must be registered non-daemonic: it spawns a "
            "ProcessPoolExecutor, and daemonic processes cannot have children"
        )


def test_pure_ingest_children_stay_daemonic():
    children = _registered_children()
    for name in PURE_INGEST_CHILDREN:
        assert children[name]["daemon"] is True
