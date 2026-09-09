"""Optional all-services launcher (decomposition Phase 6).

A thin subprocess supervisor only. It starts the three direct services with
``subprocess.Popen`` using explicit argument lists and the current Python
executable, forwards SIGINT/SIGTERM, and exits nonzero if a started child
exits unexpectedly.

It performs no ingest, scientific work, or rendering; imports no pipeline
module; creates no ``multiprocessing.Manager``, queues, readiness events,
worker pools, or runtime artifacts. The launcher is not part of the readiness
protocol: cross-service coordination happens through the durable records
beneath ``<BASE_DIR>/state/realtime/`` exactly as when the services are
started directly.

Usage:

    python src/run_all.py [--services edgewarn,ewmrs,nexrad] [flags...]

Every flag the launcher accepts is routed only to the services that own it;
unset flags are simply not forwarded, so each child keeps resolving its own
YAML/env defaults and explicit CLI values always win.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

from common.config import loader as config_loader, overlay
from util.runtime.process_identity import set_parent_death_signal


SERVICE_SCRIPTS = {
    "edgewarn": "run_edgewarn.py",
    "ewmrs": "run_ewmrs.py",
    "nexrad": "run_nexrad.py",
}

#: Bounded shutdown: SIGTERM first, then SIGKILL after this many seconds.
#: Compose allows 25 seconds in total; retain five seconds outside this budget
#: for forced cleanup, child reaping, log draining, and entrypoint teardown.
STOP_GRACE_SECONDS = 20.0

# Flag routing (plans/realtime-runner-decomposition-plan.md, CLI contract):
# each entry maps a launcher flag to the services that own it.
_ROUTING = {
    "--lat_limits": ("edgewarn",),
    "--lon_limits": ("edgewarn",),
    "--profile": ("edgewarn", "ewmrs", "nexrad"),
    "--disable-ctam": ("edgewarn",),
    "--disable-tracking": ("edgewarn",),
    "--disable-polygon-expansion": ("edgewarn",),
    "--disable-goes": ("edgewarn", "ewmrs"),
    "--disable-metar": ("ewmrs",),
    "--disable-nws": ("ewmrs",),
    "--disable-wpc": ("ewmrs",),
    "--refl-threshold": ("edgewarn",),
    "--min-seed-percentage": ("edgewarn",),
    "--drop-offset": ("edgewarn",),
}

#: Flags whose value is a space-separated list (nargs="+").
_LIST_FLAGS = {"--lat_limits", "--lon_limits"}


def resolve_services(args, requested):
    """Apply environment/YAML topology settings to requested services."""
    run_cfg = config_loader.load_config("runtime", config_dir=args.config_dir)["run"]
    omit_ewmrs = bool(overlay.resolve(
        args.disable_ewmrs, env_names=["EDGEWARN_DISABLE_EWMRS"],
        yaml_value=run_cfg["disable_ewmrs"], key="run.disable_ewmrs",
    ))
    omit_nexrad = bool(overlay.resolve(
        args.disable_nexrad, env_names=["EDGEWARN_DISABLE_NEXRAD"],
        yaml_value=run_cfg["disable_nexrad"], key="run.disable_nexrad",
    ))
    services = list(requested)
    if omit_ewmrs:
        services = [name for name in services if name != "ewmrs"]
    if omit_nexrad:
        services = [name for name in services if name != "nexrad"]
    args.mrms_core_only = bool(overlay.resolve(
        args.mrms_core_only, yaml_value=run_cfg["mrms_core_only"],
        key="run.mrms_core_only",
    ))
    if args.mrms_core_only:
        services = [name for name in services if name == "edgewarn"]
    if not services:
        raise ValueError("service selection resolved to an empty set")
    return services


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Optional supervisor starting the three EdgeWARN services"
    )
    parser.add_argument(
        "--services",
        type=str,
        default=",".join(SERVICE_SCRIPTS),
        help="Comma-separated subset of services to start (default: all three)",
    )
    parser.add_argument("--base_dir", "--base-dir", dest="base_dir", type=str, default=None)
    parser.add_argument("--config-dir", type=str, default=None)
    parser.add_argument("--profile", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--lat_limits", nargs=2, type=float, default=None)
    parser.add_argument("--lon_limits", nargs=2, type=float, default=None)
    for flag in (
        "disable-ctam", "disable-tracking", "disable-polygon-expansion",
        "disable-goes", "disable-metar", "disable-nws", "disable-wpc",
        "disable-ewmrs", "disable-nexrad",
    ):
        parser.add_argument(f"--{flag}", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--refl-threshold", type=float, default=None)
    parser.add_argument("--min-seed-percentage", type=float, default=None)
    parser.add_argument("--drop-offset", type=float, default=None)
    parser.add_argument("--mrms-core-only", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args(argv)

    requested = [
        name.strip() for name in args.services.split(",") if name.strip()
    ]
    unknown = [name for name in requested if name not in SERVICE_SCRIPTS]
    if unknown:
        parser.error(
            f"unknown service(s): {', '.join(unknown)}; "
            f"expected any of {', '.join(SERVICE_SCRIPTS)}"
        )

    # Service omission flags resolve from YAML when not given on the CLI so a
    # deployment unit can pin its topology without repeating flags. The other
    # disable flags are pure pass-through: children keep their own layering.
    try:
        services = resolve_services(args, requested)
    except ValueError as exc:
        parser.error(str(exc))
    return args, services


def build_service_commands(args, services, src_root, *, service_argv=None):
    """Build explicit argv per selected service without shell tokenization.

    ``service_argv`` contains already-validated, service-specific arguments.
    The configuration path remains launcher-owned and is injected exactly once.
    """
    service_argv = service_argv or {}
    unknown = [service for service in service_argv if service not in services]
    if unknown:
        raise ValueError(
            f"arguments supplied for unselected service(s): {', '.join(unknown)}"
        )
    for service, forwarded in service_argv.items():
        forbidden = next(
            (
                item
                for item in forwarded
                if item.split("=", 1)[0] in {"--config-dir", "--config-path"}
            ),
            None,
        )
        if forbidden is not None:
            raise ValueError(
                f"service argument {forbidden!r} cannot override --config-dir"
            )

    commands = {}
    for service in services:
        cmd = [sys.executable, str(os.path.join(src_root, SERVICE_SCRIPTS[service]))]
        if args.base_dir is not None:
            cmd += ["--base-dir", args.base_dir]
        for flag, owners in _ROUTING.items():
            if service not in owners:
                continue
            value = getattr(args, flag.lstrip("-").replace("-", "_"))
            if value is None:
                continue
            if value is True:
                cmd.append(flag)
            elif value is False:
                cmd.append(f"--no-{flag.lstrip('-')}")
            elif isinstance(value, list) or flag in _LIST_FLAGS:
                cmd += [flag] + [str(item) for item in value]
            else:
                cmd += [flag, str(value)]
        if args.mrms_core_only:
            # Pass the resolved topology to every selected child. This keeps
            # child behavior stable if its inherited config root differs.
            cmd.append("--mrms-core-only")
        cmd.extend(service_argv.get(service, ()))
        if args.config_dir is not None:
            cmd += ["--config-dir", str(args.config_dir)]
        commands[service] = cmd
    return commands


def supervise(commands, *, src_root, stop_event=None):
    """Start every command, forward signals, and wait; returns an exit code.

    A child exiting unexpectedly stops the launcher with a nonzero code after
    terminating the remaining children. A signal-driven shutdown exits zero
    when every service leader reports clean termination; bounded escalation of
    a leftover descendant is successful containment, not a service failure.
    """
    if stop_event is None:
        stop_event = threading.Event()

    previous_handlers = {}
    original_int = signal.getsignal(signal.SIGINT)

    def _forward(signum, _frame):
        print(f"[Launcher] Signal {signum} received; stopping children...")
        stop_event.set()

    # Install handlers BEFORE spawning so a signal landing during startup
    # still tears down whatever children already exist.
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.signal(signum, _forward)
        except ValueError:
            pass

    def _terminate_children(force=False):
        for proc in processes.values():
            signum = signal.SIGKILL if force else signal.SIGTERM
            try:
                if os.name == "posix":
                    # Every service is a session/process-group leader. Signal
                    # the group even if that leader has already exited: CTAM
                    # modules and other descendants may still be running.
                    os.killpg(proc.pid, signum)
                elif proc.poll() is None:
                    proc.send_signal(signum)
            except ProcessLookupError:
                # Test doubles and a concurrently reaped group can have no
                # process group; retain direct-child behavior when possible.
                if proc.poll() is None:
                    try:
                        proc.send_signal(signum)
                    except OSError:
                        pass
            except OSError:
                pass

    def _tree_is_alive(proc):
        # Reap an exited leader before probing its group. Otherwise the leader
        # remains a zombie and makes killpg(..., 0) report a false survivor.
        leader_alive = proc.poll() is None
        if os.name != "posix":
            return leader_alive
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _cleanup_after_error():
        """Best-effort cleanup that preserves the exception which triggered it."""
        _terminate_children()
        for proc in processes.values():
            try:
                proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            except BaseException:
                pass
        forced = any(_tree_is_alive(proc) for proc in processes.values())
        if forced:
            _terminate_children(force=True)
            for proc in processes.values():
                try:
                    proc.wait(timeout=STOP_GRACE_SECONDS)
                except BaseException:
                    pass

    processes: dict[str, subprocess.Popen] = {}
    exit_code = 0
    try:
        for service, cmd in commands.items():
            if stop_event.is_set():
                print("[Launcher] Shutdown requested; not starting remaining services")
                break
            try:
                processes[service] = subprocess.Popen(
                    cmd,
                    cwd=src_root,
                    start_new_session=True,
                    # Arm this in the forked child before exec rather than
                    # using an ambient environment marker that each service
                    # has to parse.  The setting survives exec on Linux.
                    # Windows has no PR_SET_PDEATHSIG equivalent here.
                    preexec_fn=set_parent_death_signal if os.name == "posix" else None,
                )
            except Exception as exc:
                print(
                    f"[Launcher] Failed to start '{service}': {exc}; "
                    "stopping started children"
                )
                exit_code = 1
                stop_event.set()
                break
        print(f"[Launcher] Started: {', '.join(f'{s} (pid {p.pid})' for s, p in processes.items())}")

        while not stop_event.is_set():
            for service, proc in processes.items():
                code = proc.poll()
                if code is None:
                    continue
                detail = (
                    "cleanly before shutdown"
                    if code == 0
                    else f"unexpectedly (rc={code})"
                )
                print(
                    f"[Launcher] Service '{service}' exited {detail}; "
                    "stopping the remaining children"
                )
                exit_code = 1
                stop_event.set()
                break
            if stop_event.is_set():
                break
            time.sleep(0.5)

        # Graceful stop: SIGTERM admits no new work in children; wait the
        # bounded interval, then escalate to SIGKILL for survivors.
        _terminate_children()
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if all(not _tree_is_alive(proc) for proc in processes.values()):
                break
            time.sleep(0.2)
        survivors = [s for s, p in processes.items() if _tree_is_alive(p)]
        if survivors:
            print(f"[Launcher] Escalating to SIGKILL for: {', '.join(survivors)}")
            _terminate_children(force=True)

        for service, proc in processes.items():
            try:
                returncode = proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                print(f"[Launcher] Service '{service}' did not exit after SIGKILL")
                exit_code = 1
                continue
            print(f"[Launcher] Service '{service}' terminated (rc={returncode})")
            expected_signal_exit = (
                os.name == "posix"
                and returncode == -signal.SIGTERM
                and exit_code == 0
            )
            if returncode != 0 and not expected_signal_exit:
                exit_code = 1
    except BaseException:
        _cleanup_after_error()
        raise
    finally:
        try:
            signal.signal(signal.SIGINT, original_int)
        except ValueError:
            # Signal handlers can only be (re)installed from the main thread;
            # supervised-in-thread callers (tests) restore nothing.
            pass
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except ValueError:
                pass

    return exit_code


def main(argv=None):
    args, services = _parse_args(argv)
    src_root = os.path.dirname(os.path.abspath(__file__))
    commands = build_service_commands(args, services, src_root)
    print(
        "[Launcher] This optional supervisor performs no ingest, rendering, or "
        "coordination work; the direct commands remain the production path."
    )
    return supervise(commands, src_root=src_root)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("CTRL+C detected, exiting ...")
        sys.exit(0)
