"""Phase 6 optional-launcher contract tests.

Covers ``src/run_all.py`` without starting any scientific work: exact flag
routing, service-subset selection, subprocess signal forwarding, and
exit-code semantics, all driven against throwaway sleeper scripts.
"""

import argparse
from pathlib import Path
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

import run_all


@pytest.fixture()
def src_root(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    return str(root)


def _args(**overrides):
    defaults = dict(
        services="ingest,edgewarn,nexrad",
        base_dir=None,
        config_dir=None,
        profile=None,
        lat_limits=None,
        lon_limits=None,
        disable_ctam=None,
        disable_tracking=None,
        disable_polygon_expansion=None,
        disable_glm=None,
        disable_nexrad=None,
        refl_threshold=None,
        min_seed_percentage=None,
        drop_offset=None,
        mrms_core_only=None,
    )
    return argparse.Namespace(**{**defaults, **overrides})


class TestServiceSelection:
    def test_unknown_service_rejected(self):
        with pytest.raises(SystemExit):
            run_all._parse_args(["--services", "edgewarn,bogus"])

    def test_mrms_core_only_retains_ingest_and_primary(self):
        args, services = run_all._parse_args(["--mrms-core-only"])
        assert services == ["ingest", "edgewarn"]


class TestFlagRouting:

    def test_unset_flags_are_never_forwarded(self, src_root):
        commands = run_all.build_service_commands(_args(), ["nexrad"], src_root)
        assert commands["nexrad"] == [
            sys.executable, os.path.join(src_root, "run_nexrad.py")
        ]


    @pytest.mark.parametrize(
        "forwarded",
        [("--config-dir", "/other"), ("--config-path=/other",)],
    )
    def test_scoped_argv_cannot_override_config(self, src_root, forwarded):
        with pytest.raises(ValueError, match="cannot override"):
            run_all.build_service_commands(
                _args(config_dir="/cfg"),
                ["edgewarn"],
                src_root,
                service_argv={"edgewarn": forwarded},
            )


def _sleeper(tmp_path, *, name="sleeper.py", code=None, trap=False):
    """A child that sleeps until terminated (or exits with *code*)."""
    if code is not None:
        body = f"import sys\nsys.exit({code})\n"
    elif trap:
        # Ignores SIGTERM entirely: only SIGKILL ends it.
        body = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "\nwhile True:\n"
            "    time.sleep(0.1)\n"
        )
    else:
        body = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))\n"
            "\nwhile True:\n"
            "    time.sleep(0.1)\n"
        )
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def _supervise_until_stopped(commands, src_root, *, observe_seconds=3.0):
    """Run the launcher, let one child exit, then report sibling liveness.

    Returns ``(exit_code, siblings_alive)``, where *siblings_alive* records
    whether some child was still running after *observe_seconds*.
    """
    stop = threading.Event()
    started = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    result = {}

    def run():
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(run_all.subprocess, "Popen", popen)
            result["code"] = run_all.supervise(commands, src_root=src_root, stop_event=stop, restart_policy={"max_restarts": 0, "restart_window_seconds": 60})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(observe_seconds)
    alive = thread.is_alive() and any(proc.poll() is None for proc in started)
    stop.set()
    thread.join(timeout=30)
    assert not thread.is_alive(), "launcher did not stop after the stop request"
    return result["code"], alive


class TestSupervision:


    def test_popen_failure_returns_nonzero(self, src_root, monkeypatch):
        def fail_to_start(*_args, **_kwargs):
            raise OSError("cannot exec")

        monkeypatch.setattr(run_all.subprocess, "Popen", fail_to_start)
        commands = run_all.build_service_commands(_args(), ["edgewarn"], src_root)

        assert run_all.supervise(commands, src_root=src_root, restart_policy={"max_restarts": 0, "restart_window_seconds": 60}) == 1


    def test_unexpected_supervisor_error_terminates_and_reaps_children(
        self, src_root, monkeypatch
    ):
        class Child:
            pid = 1234

            def __init__(self):
                self.returncode = None
                self.signals = []
                self.waits = []

            def poll(self):
                return self.returncode

            def send_signal(self, signum):
                self.signals.append(signum)
                self.returncode = 0

            def wait(self, timeout):
                self.waits.append(timeout)
                return self.returncode

        child = Child()
        monkeypatch.setattr(run_all.subprocess, "Popen", lambda *_a, **_k: child)
        monkeypatch.setattr(
            run_all.time, "sleep", lambda _seconds: (_ for _ in ()).throw(
                RuntimeError("supervisor failed")
            )
        )

        with pytest.raises(RuntimeError, match="supervisor failed"):
            run_all.supervise({"edgewarn": ["edgewarn"]}, src_root=src_root)

        assert child.signals == [signal.SIGTERM]
        assert child.waits == [run_all.STOP_GRACE_SECONDS]


    @pytest.mark.skipif(os.name != "posix", reason="POSIX signal return code")
    def test_signal_driven_shutdown_accepts_raw_sigterm_exit(
        self, tmp_path, src_root
    ):
        sleeper = tmp_path / "run_edgewarn.py"
        sleeper.write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
        driver = tmp_path / "driver.py"
        repo_src = str(Path(__file__).resolve().parents[3] / "src")
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {repo_src!r})\n"
            "import run_all\n"
            f"run_all.SERVICE_SCRIPTS['ingest'] = {str(sleeper)!r}\n"
            "sys.exit(run_all.main(['--services', 'ingest']))\n",
            encoding="utf-8",
        )

        launcher = subprocess.Popen([sys.executable, str(driver)])
        try:
            time.sleep(1.0)
            launcher.send_signal(signal.SIGTERM)
            assert launcher.wait(timeout=45) == 0
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()

    def test_signal_driven_shutdown_reports_child_cleanup_failure(
        self, tmp_path, src_root
    ):
        failing_child = tmp_path / "run_edgewarn.py"
        failing_child.write_text(
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_a: sys.exit(7))\n"
            "\nwhile True:\n"
            "    time.sleep(0.1)\n"
        )
        driver = tmp_path / "driver.py"
        repo_src = str(Path(__file__).resolve().parents[3] / "src")
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {repo_src!r})\n"
            "import run_all\n"
            f"run_all.SERVICE_SCRIPTS['ingest'] = {str(failing_child)!r}\n"
            "sys.exit(run_all.main(['--services', 'ingest']))\n"
        )

        proc = subprocess.Popen(
            [sys.executable, str(driver)], start_new_session=True
        )
        try:
            time.sleep(1.0)
            proc.send_signal(signal.SIGTERM)
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            pytest.fail("launcher did not exit after SIGTERM")

        assert code == 1


    def test_survivors_are_escalated_to_sigkill_within_grace(
        self, tmp_path, src_root, monkeypatch
    ):
        stubborn = _sleeper(tmp_path)  # ignores SIGTERM entirely
        monkeypatch.setattr(run_all, "SERVICE_SCRIPTS", {"edgewarn": stubborn})
        commands = run_all.build_service_commands(_args(), ["edgewarn"], src_root)

        stop = threading.Event()

        def run():
            run_all.supervise(commands, src_root=src_root, stop_event=stop, restart_policy={"max_restarts": 0, "restart_window_seconds": 60})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(1.0)
        stop.set()
        deadline = time.time() + run_all.STOP_GRACE_SECONDS + 10
        while thread.is_alive() and time.time() < deadline:
            time.sleep(0.2)

        assert not thread.is_alive()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
    def test_shutdown_kills_descendant_after_service_leader_exits(
        self, tmp_path, src_root
    ):
        descendant_pid = tmp_path / "descendant.pid"
        worker = tmp_path / "run_edgewarn.py"
        worker.write_text(
            "import pathlib, signal, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(300)'])\n"
            f"pathlib.Path({str(descendant_pid)!r}).write_text(str(child.pid))\n"
            "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))\n"
            "while True:\n"
            "    time.sleep(0.05)\n",
            encoding="utf-8",
        )
        driver = tmp_path / "driver.py"
        repo_src = str(Path(__file__).resolve().parents[3] / "src")
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {repo_src!r})\n"
            "import run_all\n"
            "run_all.STOP_GRACE_SECONDS = 0.5\n"
            f"run_all.SERVICE_SCRIPTS['ingest'] = {str(worker)!r}\n"
            "sys.exit(run_all.main(['--services', 'ingest']))\n",
            encoding="utf-8",
        )

        launcher = subprocess.Popen([sys.executable, str(driver)])
        try:
            deadline = time.time() + 10
            while not descendant_pid.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert descendant_pid.exists()
            pid = int(descendant_pid.read_text())

            launcher.send_signal(signal.SIGTERM)
            # The service leader handled TERM cleanly. Bounded SIGKILL of its
            # deliberately stubborn descendant is successful containment.
            assert launcher.wait(timeout=45) == 0

            deadline = time.time() + 5
            while Path(f"/proc/{pid}").exists() and time.time() < deadline:
                time.sleep(0.05)
            assert not Path(f"/proc/{pid}").exists()
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()


@pytest.mark.parametrize("services", [["edgewarn"], ["ingest", "ingest"]])
def test_preflight_rejects_missing_or_duplicate_producer(services):
    with pytest.raises(ValueError):
        run_all.preflight_topology(_args(), services)


def test_preflight_rejects_disabled_handoff(monkeypatch):
    monkeypatch.setenv("EDGEWARN_HANDOFF_ENABLED", "false")
    with pytest.raises(ValueError, match="handoff.enabled"):
        run_all.preflight_topology(_args(), ["ingest", "edgewarn"])


@pytest.mark.parametrize("forwarded", [
    {"ingest": ("--disable-glm",), "edgewarn": ("--no-disable-glm",)},
    {"ingest": ("--base-dir", "/a"), "edgewarn": ("--base-dir", "/b")},
    {"ingest": ("--no-disable-ctam",), "edgewarn": ("--disable-ctam",)},
    {"ingest": ("--disable-stormprob",), "edgewarn": ("--no-disable-stormprob",)},
])
def test_preflight_rejects_conflicting_worker_settings(forwarded):
    with pytest.raises(ValueError, match="conflicting"):
        run_all.preflight_topology(_args(), ["ingest", "edgewarn"], forwarded)


def test_supervisor_restarts_failed_child_without_stopping_sibling(tmp_path, src_root, monkeypatch):
    marker = tmp_path / "first-run"
    crashy = tmp_path / "crashy.py"
    crashy.write_text(f"""from pathlib import Path
import time, sys
marker=Path({str(marker)!r})
if not marker.exists():
    marker.write_text('started')
    sys.exit(1)
while True: time.sleep(0.05)
""")
    sibling = _sleeper(tmp_path)
    started = []
    sibling_alive_on_restart = []
    popen = run_all.subprocess.Popen
    def spawn(*args, **kwargs):
        if len(started) == 2:
            sibling_alive_on_restart.append(started[1].poll() is None)
        process = popen(*args, **kwargs)
        started.append(process)
        return process
    monkeypatch.setattr(run_all.subprocess, "Popen", spawn)
    stop = threading.Event()
    timer = threading.Timer(1.5, stop.set)
    timer.start()
    try:
        run_all.supervise({"edgewarn": [sys.executable, str(crashy)],
                           "nexrad": [sys.executable, sibling]}, src_root=src_root,
            stop_event=stop, restart_policy={"max_restarts": 1, "restart_window_seconds": 60,
                                           "base_backoff_seconds": 0, "max_backoff_seconds": 0})
    finally:
        timer.cancel()
    assert len(started) == 3
    assert sibling_alive_on_restart == [True]
    assert all(process.poll() is not None for process in started)


def test_core_dependency_flags_reach_ingest_and_analysis_only(src_root):
    args = _args(disable_ctam=True, disable_glm=True, lat_limits=[20, 55], profile=False)
    commands = run_all.build_service_commands(args, ['ingest', 'edgewarn', 'nexrad'], src_root)
    for owner in ('ingest', 'edgewarn'):
        assert '--disable-ctam' in commands[owner]
        assert '--disable-glm' in commands[owner]
    assert '--disable-ctam' not in commands['nexrad']
    assert '--disable-glm' not in commands['nexrad']
    assert '--lat_limits' in commands['edgewarn']
    assert '--lat_limits' not in commands['ingest']
    assert all('--no-profile' in command for command in commands.values())


def test_scoped_arguments_keep_one_configuration_root(src_root):
    commands = run_all.build_service_commands(
        _args(config_dir='/cfg'), ['ingest', 'edgewarn'], src_root,
        service_argv={'edgewarn': ('--lat_limits', '20', '55'), 'ingest': ('--no-profile',)},
    )
    assert commands['edgewarn'][-5:] == ['--lat_limits', '20', '55', '--config-dir', '/cfg']
    assert commands['ingest'][-3:] == ['--no-profile', '--config-dir', '/cfg']
    assert all(command.count('--config-dir') == 1 for command in commands.values())
