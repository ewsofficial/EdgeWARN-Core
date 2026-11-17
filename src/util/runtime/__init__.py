"""Lazy exports for Core orchestration, NEXRAD loops, and shared runtime helpers."""

_LAZY_EXPORTS = {
    # .background — accessory and NEXRAD loops
    "nexrad_ingest_loop": ".background",
    "nexrad_render_loop": ".background",
    # .cycle — primary cycle state and orchestration
    "CycleOutcome": ".cycle",
    "CycleRetryPolicy": ".cycle",
    "CycleStageResult": ".cycle",
    "CycleStateStore": ".cycle",
    "CycleStatus": ".cycle",
    "PersistedCycleState": ".cycle",
    "PrimaryCycleConfig": ".cycle",
    "run_primary_cycle_once": ".cycle",
    # .logging
    "drain_log_queue": ".logging",
    # .processes
    "AccessorySupervisor": ".processes",
    "StartedProcessRegistry": ".processes",
    "stop_process": ".processes",
    # .scheduler
    "load_last_processed_from_stormcells": ".scheduler",
}

__all__ = [
    "AccessorySupervisor",
    "CycleOutcome",
    "CycleRetryPolicy",
    "CycleStageResult",
    "CycleStateStore",
    "CycleStatus",
    "PersistedCycleState",
    "PrimaryCycleConfig",
    "StartedProcessRegistry",
    "drain_log_queue",
    "load_last_processed_from_stormcells",
    "nexrad_ingest_loop",
    "nexrad_render_loop",
    "run_primary_cycle_once",
    "stop_process",
]


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
