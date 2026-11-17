from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import threading
import time

from .config import section


def stop_process(process, name, *, join_timeout=None):
    if process is None:
        return

    supervisor = section("supervisor")
    if join_timeout is None:
        join_timeout = supervisor["stop_join_timeout_seconds"]

    try:
        if process.is_alive():
            print(f"[Scheduler] Stopping {name} process...")
            process.terminate()

        process.join(timeout=join_timeout)

        if process.is_alive():
            print(f"[Scheduler] {name} did not stop in time; killing...")
            process.kill()
            process.join(timeout=supervisor["stop_kill_join_timeout_seconds"])
    except Exception as exc:
        print(f"[Scheduler] Failed to stop {name} process cleanly: {exc}")


@dataclass
class StartedProcessRegistry:
    processes: list[tuple[object, str]] = field(default_factory=list)

    def start(self, process, name):
        if process is None:
            return None

        process.start()
        self.processes.append((process, name))
        return process

    def shutdown(self, *, queue_sentinels=(), manager=None):
        for queue_obj, sentinel in queue_sentinels:
            try:
                queue_obj.put(sentinel)
            except Exception:
                pass

        while self.processes:
            process, name = self.processes.pop()
            stop_process(process, name)

        if manager is not None:
            try:
                manager.shutdown()
            except Exception:
                pass


@dataclass
class AccessorySupervisor:
    """Monitor and restart accessory child processes with bounded backoff.

    Each registered process is checked periodically.  Dead processes are
    restarted with exponential backoff.  Crash-loop detection disables
    restarts after *max_restarts* deaths within *restart_window_seconds*.
    """

    _process_info: list = field(default_factory=list)
    _restart_times: dict = field(default_factory=lambda: defaultdict(list))
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    max_restarts: int = field(default_factory=lambda: section("supervisor")["max_restarts"])
    restart_window_seconds: float = field(default_factory=lambda: section("supervisor")["restart_window_seconds"])
    base_backoff_seconds: float = field(default_factory=lambda: section("supervisor")["base_backoff_seconds"])
    max_backoff_seconds: float = field(default_factory=lambda: section("supervisor")["max_backoff_seconds"])
    health_path: str | None = None

    def add(
        self,
        name,
        target,
        *,
        enabled=True,
        args=None,
        kwargs=None,
        daemon=True,
        cleanup_event=None,
        heartbeat_path=None,
        heartbeat_stale_seconds=None,
        heartbeat_startup_grace_seconds=None,
    ):
        entry = {
            "name": name,
            "target": target,
            "args": args or (),
            "kwargs": kwargs or {},
            "daemon": daemon,
            "enabled": enabled,
            "process": None,
            "cleanup_event": cleanup_event,
            "heartbeat_path": heartbeat_path,
            "heartbeat_stale_seconds": heartbeat_stale_seconds,
            "heartbeat_startup_grace_seconds": max(
                0.0,
                float(heartbeat_startup_grace_seconds)
                if heartbeat_startup_grace_seconds is not None
                else 0.0,
            ),
            "started_monotonic": None,
        }
        self._process_info.append(entry)
        return entry

    def start_all(self):
        import multiprocessing
        for info in self._process_info:
            if not info["enabled"]:
                continue
            proc = multiprocessing.Process(
                target=info["target"],
                args=info["args"],
                kwargs=info["kwargs"],
                name=info["name"],
            )
            proc.daemon = info["daemon"]
            proc.start()
            info["process"] = proc
            info["started_monotonic"] = time.monotonic()
            self._record_health(info["name"], "running")

    def request_stop(self):
        self._stop_event.set()

    def disabled_names(self):
        """Children whose restarts were disabled by crash-loop detection."""
        return [
            info["name"]
            for info in self._process_info
            if info.get("process") is not None and not info["enabled"]
        ]

    def check(self):
        import multiprocessing
        now = time.monotonic()
        for info in self._process_info:
            if self._stop_event.is_set():
                continue
            if not info["enabled"]:
                cooldown = info.get("cooldown_until")
                if cooldown is None or now < cooldown:
                    continue
                info["enabled"] = True
                info["cooldown_until"] = None
                self._restart_times[info["name"]].clear()
                self._restart(info)
                continue
            proc = info.get("process")
            if proc is None or not proc.is_alive():
                if info.get("restart_not_before") is not None:
                    if now >= info["restart_not_before"]:
                        self._restart(info)
                    continue
                self._handle_death(info, now)
            else:
                stale_age = self._stale_heartbeat_age(info, proc, now)
                if stale_age is not None:
                    reason = f"stale heartbeat ({stale_age:.1f}s old)"
                    print(f"[Supervisor] {info['name']} process is alive but has {reason}; restarting")
                    stop_process(proc, info["name"])
                    self._handle_death(info, now, reason=reason)

    def _stale_heartbeat_age(self, info, proc, monotonic_now):
        path = info.get("heartbeat_path")
        stale_after = info.get("heartbeat_stale_seconds")
        if not path or stale_after is None:
            return None
        started_at = info.get("started_monotonic") or monotonic_now
        try:
            stale_after = float(stale_after)
        except (TypeError, ValueError):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                heartbeat = json.load(handle)
            if int(heartbeat.get("pid")) != proc.pid:
                raise ValueError("heartbeat belongs to a different process")
            updated_at = datetime.fromisoformat(str(heartbeat["updated_at"]))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            return age if age < -stale_after or age > stale_after else None
        except Exception:
            startup_grace = info.get("heartbeat_startup_grace_seconds", 0.0)
            age = monotonic_now - started_at
            return age if age > max(stale_after, startup_grace) else None

    def _handle_death(self, info, now, *, reason="dead"):
        import multiprocessing
        name = info["name"]
        with self._lock:
            times = self._restart_times[name]
            times.append(now)
            times[:] = [t for t in times if now - t < self.restart_window_seconds]

            attempt = len(times)
            exit_code = info["process"].exitcode if info["process"] is not None else "N/A"
            print(f"[Supervisor] {name} process is {reason} (pid exited {exit_code}); restart attempt {attempt}")

            # Clear any shared flag the process may have left asserted before
            # deciding on a restart: blocking loops (e.g. GOES ingest pausing
            # while the renderer is active) must never stay stuck behind a
            # dead process, whether or not it is going to be restarted.
            cleanup_event = info.get("cleanup_event")
            if cleanup_event is not None:
                try:
                    cleanup_event.clear()
                except Exception:
                    pass

            if attempt > self.max_restarts:
                print(
                    f"[Supervisor] {name} has crashed {attempt} times "
                    f"in {self.restart_window_seconds}s; disabling restarts"
                )
                info["enabled"] = False
                info["cooldown_until"] = now + self.restart_window_seconds
                self._record_health(name, "crashed", error=f"crashed {attempt} times")
                return

            delay = min(
                self.max_backoff_seconds,
                self.base_backoff_seconds * (2 ** (attempt - 1)),
            )
            self._record_health(name, "restarting", error=f"{reason}, restart pending", attempt=attempt)

        # Do not block the owning service's supervision/heartbeat loop.  The
        # next periodic check admits the restart once its deadline arrives.
        info["restart_not_before"] = now + delay
        return

    def _restart_due(self, info, now):
        deadline = info.get("restart_not_before")
        return deadline is None or now >= deadline

    def _restart(self, info):
        import multiprocessing
        if self._stop_event.is_set() or not info["enabled"]:
            return
        proc = multiprocessing.Process(
            target=info["target"], args=info["args"], kwargs=info["kwargs"], name=info["name"],
        )
        proc.daemon = info["daemon"]
        proc.start()
        info["process"] = proc
        info["started_monotonic"] = time.monotonic()
        info["restart_not_before"] = None
        self._record_health(info["name"], "running")

    def _record_health(self, name, status, *, error=None, attempt=None):
        if self.health_path is None:
            return
        try:
            os.makedirs(os.path.dirname(self.health_path), exist_ok=True)
            current = {}
            if os.path.isfile(self.health_path):
                try:
                    with open(self.health_path, "r") as f:
                        current = json.load(f)
                except Exception:
                    current = {}
            now_iso = datetime.now(timezone.utc).isoformat()
            entry = dict(current.get(name, {}))
            entry["status"] = status
            entry["last_updated"] = now_iso
            if error is not None:
                entry["last_error"] = error
            if attempt is not None:
                entry["restart_attempt"] = attempt
            current[name] = entry
            from util.atomic import atomic_write_json
            atomic_write_json(self.health_path, current, indent=2)
        except Exception:
            pass

    def shutdown(self):
        for info in self._process_info:
            proc = info.get("process")
            if proc is not None:
                stop_process(proc, info["name"])
