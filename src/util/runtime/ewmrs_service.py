"""EWMRS/accessory service supervision functions (decomposition Phase 4).

Registers every child of the standalone EWMRS service (``run_ewmrs.py``):
METAR, NWS, and WPC continuous ingest, GOES ABI ingest and poll-based
rendering, and the input render consumer that turns each committed source input
into independent, acknowledged per-layer work.

NEXRAD is deliberately absent: it is supervised by
``util.runtime.nexrad_service``, never here. The primary EdgeWARN service does
not import this module.
"""

from util.runtime.background import goes_loop, goes_render_loop, metar_loop, nws_loop, wpc_loop
from util.runtime.ewmrs_consumer import ewmrs_consumer_loop


def register_ewmrs_accessories(
    supervisor,
    *,
    base_dir,
    metar_enabled,
    nws_enabled,
    wpc_enabled,
    goes_ingest_enabled,
    goes_render_enabled,
    consumer_enabled=True,
    goes_cycle_active,
    goes_render_active,
    goes_pause_ingest_during_render,
    goes_poll_seconds,
    child_log_queue,
    run_id=None,
):
    """Add every EWMRS-owned child loop to *supervisor*.

    ``goes_ingest_enabled`` gates ABI ingest only; scan-time GLM is acquired by
    the independent ingest service for Core integration; it has no ABI layer.
    Accessory loops are optional inputs — stopping one degrades those inputs
    visibly without blocking other products, and a crash-looped child is
    reported as a degraded entry in the EWMRS heartbeat rather than hidden.
    """
    supervisor.add(
        "METAR", metar_loop,
        enabled=metar_enabled,
        daemon=True,
    )
    supervisor.add(
        "NWS", nws_loop,
        enabled=nws_enabled,
        daemon=True,
    )
    supervisor.add(
        "WPC", wpc_loop,
        enabled=wpc_enabled,
        daemon=True,
    )
    supervisor.add(
        "GOES", goes_loop,
        enabled=goes_ingest_enabled,
        args=(goes_cycle_active, goes_render_active, goes_pause_ingest_during_render, goes_poll_seconds),
        daemon=True,
        cleanup_event=goes_cycle_active,
    )
    if child_log_queue is None:
        raise ValueError(
            "child_log_queue is required: the GOES Render and EWMRS Consumer "
            "children route their logs through it"
        )
    supervisor.add(
        "GOES Render", goes_render_loop,
        enabled=bool(goes_render_enabled),
        args=(base_dir, child_log_queue, goes_render_active),
        daemon=False,
        cleanup_event=goes_render_active,
    )
    supervisor.add(
        "EWMRS Consumer", ewmrs_consumer_loop,
        enabled=consumer_enabled,
        args=(base_dir, child_log_queue),
        kwargs={"run_id": run_id, "disable_goes": not goes_ingest_enabled},
        daemon=False,
    )
