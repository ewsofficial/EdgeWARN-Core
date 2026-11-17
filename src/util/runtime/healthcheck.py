"""Read-only container health check for enabled Python producers."""
from datetime import datetime, timezone
from common.config.loader import load_config
from common.config.overlay import resolve_base_dir
from util.runtime.services import classify_heartbeat_state, heartbeat_path


def main():
    api = load_config("api")["server"]
    root = resolve_base_dir(None, load_config("filesystem")).expanduser().resolve()
    now = datetime.now(timezone.utc)
    for service in api["enabled_services"]:
        state, _ = classify_heartbeat_state(
            heartbeat_path(root, service), stale_after_seconds=api["service_stale_after_seconds"],
            progress_stale_after_seconds=api["service_progress_stale_after_seconds"], now=now)
        if state not in {"active", "degraded"}:
            print(f"{service}: {state}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
