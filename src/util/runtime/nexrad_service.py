"""NEXRAD service supervision functions (decomposition Phase 3).

Owns the complete NEXRAD lifecycle registration: ingest (non-daemonic, with
its heartbeat-staleness restart policy) and GUI rendering. Consumed by the
standalone ``run_nexrad.py`` entry point.

Since Phase 3 the render loop lives in ``NEXRAD.gui_pipeline``; importing this
module loads neither any other service's scientific stack.
"""

from common.ingest.nexrad.config import (
    heartbeat_stale_seconds,
    heartbeat_startup_grace_seconds,
)
from util.runtime.background import nexrad_ingest_loop, nexrad_render_loop


def register_nexrad_supervision(
    supervisor,
    *,
    base_dir,
    nexrad_log_queue,
    nexrad_heartbeat_path,
    enabled=True,
):
    """Add NEXRAD ingest and render children to *supervisor*."""
    supervisor.add(
        "NEXRAD Render", nexrad_render_loop,
        enabled=enabled,
        args=(base_dir,),
        daemon=True,
    )
    supervisor.add(
        "NEXRAD Ingest", nexrad_ingest_loop,
        enabled=enabled,
        args=(nexrad_log_queue, base_dir, nexrad_heartbeat_path),
        daemon=False,
        heartbeat_path=nexrad_heartbeat_path,
        # Called at registration rather than bound at import: these resolve
        # through nexrad.yaml, which needs the exported config root that only
        # exists after argument parsing.
        heartbeat_stale_seconds=heartbeat_stale_seconds(),
        heartbeat_startup_grace_seconds=heartbeat_startup_grace_seconds(),
    )
