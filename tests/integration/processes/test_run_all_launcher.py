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
        services="edgewarn,ewmrs,nexrad",
        base_dir=None,
        config_dir=None,
        profile=None,
        lat_limits=None,
        lon_limits=None,
        disable_ctam=None,
        disable_tracking=None,
        disable_polygon_expansion=None,
        disable_goes=None,
        disable_metar=None,
        disable_nws=None,
        disable_wpc=None,
        disable_ewmrs=None,
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

    def test_mrms_core_only_starts_only_the_primary(self):
        args, services = run_all._parse_args(["--mrms-core-only"])
        assert services == ["edgewarn"]

    def test_yaml_mrms_core_only_starts_only_the_primary(self, monkeypatch):
        monkeypatch.setattr(
            run_all.config_loader,
            "load_config",
            lambda *_args, **_kwargs: {"run": {
                "disable_ewmrs": False,
                "disable_nexrad": False,
                "mrms_core_only": True,
            }},
        )
        args, services = run_all._parse_args([])
        assert args.mrms_core_only is True
        assert services == ["edgewarn"]

    def test_disable_flags_omit_services(self, monkeypatch):
        # CLI values must win over the YAML layer.
        _, services = run_all._parse_args(["--no-disable-ewmrs", "--disable-nexrad"])
        assert "nexrad" not in services
        assert "ewmrs" in services

        _, services = run_all._parse_args(["--disable-ewmrs"])
        assert "ewmrs" not in services
        assert "nexrad" in services


class TestFlagRouting:
    def test_every_flag_routes_only_to_its_owners(self, src_root):
        args = _args(
            base_dir="/data",
            config_dir="/cfg",
            profile=True,
            lat_limits=[20.0, 55.0],
            lon_limits=[230.0, 300.0],
            disable_ctam=True,
            disable_tracking=True,
            disable_polygon_expansion=True,
            disable_goes=True,
            disable_metar=True,
            disable_nws=True,
            disable_wpc=True,
            refl_threshold=25.0,
            min_seed_percentage=15.0,
            drop_offset=1.0,
            mrms_core_only=True,
        )
        commands = run_all.build_service_commands(args, ["edgewarn", "ewmrs"], src_root)

        edgewarn = " ".join(commands["edgewarn"][2:])
        ewmrs = " ".join(commands["ewmrs"][2:])
        # Shared flags reach both...
        for token in ("--base-dir /data", "--config-dir /cfg", "--profile"):
            assert token in edgewarn
            assert token in ewmrs
        # ...primary-only flags stay primary-only...
        for token in (
            "--lat_limits 20.0 55.0", "--lon_limits 230.0 300.0",
            "--disable-ctam", "--disable-tracking", "--disable-polygon-expansion",
            "--refl-threshold 25.0", "--min-seed-percentage 15.0", "--drop-offset 1.0",
        ):
            assert token in edgewarn
            assert token not in ewmrs
        # The resolved topology reaches every child so direct and supervised
        # launches agree even if children inherited a different config root.
        assert "--mrms-core-only" in edgewarn
        assert "--mrms-core-only" in ewmrs
        # ...EWMRS-owned accessory flags stay EWMRS-only, and --disable-goes
        # is owned by both (scan-time GLM on primary, ABI on EWMRS).
        for token in ("--disable-metar", "--disable-nws", "--disable-wpc"):
            assert token in ewmrs
            assert token not in edgewarn
        assert "--disable-goes" in ewmrs
        assert "--disable-goes" in edgewarn

    def test_unset_flags_are_never_forwarded(self, src_root):
        commands = run_all.build_service_commands(_args(), ["nexrad"], src_root)
        assert commands["nexrad"] == [
            sys.executable, os.path.join(src_root, "run_nexrad.py")
        ]

    def test_explicit_false_forwards_no_form(self, src_root):
        commands = run_all.build_service_commands(
            _args(profile=False), ["ewmrs"], src_root
        )
        assert "--no-profile" in commands["ewmrs"]

    def test_scoped_argv_is_appended_once_and_config_is_canonical(self, src_root):
        commands = run_all.build_service_commands(
            _args(config_dir="/cfg"),
            ["edgewarn", "ewmrs"],
            src_root,
            service_argv={
                "edgewarn": ("--lat_limits", "20", "55"),
                "ewmrs": ("--disable-wpc",),
            },
        )

        assert commands["edgewarn"][-5:] == [
            "--lat_limits", "20", "55", "--config-dir", "/cfg"
        ]
        assert commands["ewmrs"][-3:] == ["--disable-wpc", "--config-dir", "/cfg"]
        assert commands["edgewarn"].count("--config-dir") == 1
        assert commands["ewmrs"].count("--config-dir") == 1

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


class TestSupervision:
    def test_child_startup_exit_returns_nonzero_and_reaps_other_children(
        self, tmp_path, src_root, monkeypatch
    ):
        sleeper = _sleeper(tmp_path)
        missing = str(tmp_path / "missing.py")
        monkeypatch.setattr(
            run_all,
            "SERVICE_SCRIPTS",
            {"edgewarn": sleeper, "ewmrs": missing},
        )
        commands = run_all.build_service_commands(
            _args(), ["edgewarn", "ewmrs"], src_root
        )

        assert run_all.supervise(commands, src_root=src_root) == 1

    def test_popen_failure_returns_nonzero(self, src_root, monkeypatch):
        def fail_to_start(*_args, **_kwargs):
            raise OSError("cannot exec")

        monkeypatch.setattr(run_all.subprocess, "Popen", fail_to_start)
        commands = run_all.build_service_commands(_args(), ["edgewarn"], src_root)

        assert run_all.supervise(commands, src_root=src_root) == 1

    def test_stop_request_during_startup_prevents_remaining_spawns(
        self, src_root, monkeypatch
    ):
        stop_event = threading.Event()
        spawned = []

        class Child:
            pid = 1234
            returncode = None

            def poll(self):
                return self.returncode

            def send_signal(self, _signum):
                self.returncode = 0

            def wait(self, timeout):
                return self.returncode

        def spawn(command, **_kwargs):
            spawned.append(command)
            stop_event.set()
            return Child()

        monkeypatch.setattr(run_all.subprocess, "Popen", spawn)
        commands = {
            "edgewarn": ["edgewarn"],
            "ewmrs": ["ewmrs"],
            "nexrad": ["nexrad"],
        }

        assert run_all.supervise(
            commands, src_root=src_root, stop_event=stop_event
        ) == 0
        assert spawned == [["edgewarn"]]

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

    def test_signal_driven_shutdown_terminates_children_cleanly(self, tmp_path, src_root):
        """End-to-end: SIGINT reaches the launcher's children through the driver."""
        sleeper = _sleeper(tmp_path, name="run_edgewarn.py")
        sleeper_b = _sleeper(tmp_path, name="run_ewmrs.py")
        repo_src = str(Path(__file__).resolve().parents[3] / "src")
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {repo_src!r})\n"
            "import run_all\n"
            f"run_all.SERVICE_SCRIPTS['edgewarn'] = {sleeper!r}\n"
            f"run_all.SERVICE_SCRIPTS['ewmrs'] = {sleeper_b!r}\n"
            "sys.exit(run_all.main(['--services', 'edgewarn,ewmrs']))\n"
        )

        proc = subprocess.Popen(
            [sys.executable, str(driver)], start_new_session=True
        )
        try:
            time.sleep(2.0)  # launcher + two sleepers up

            proc.send_signal(signal.SIGINT)
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # Kill the whole process group so sleeper children cannot outlive
            # the failed assertion.
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            pytest.fail("launcher did not exit after SIGINT")

        # Clean signal shutdown exits zero even though the children were killed.
        assert code == 0

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
            f"run_all.SERVICE_SCRIPTS['edgewarn'] = {str(sleeper)!r}\n"
            "sys.exit(run_all.main(['--services', 'edgewarn']))\n",
            encoding="utf-8",
        )

        launcher = subprocess.Popen([sys.executable, str(driver)])
        try:
            time.sleep(1.0)
            launcher.send_signal(signal.SIGTERM)
            assert launcher.wait(timeout=10) == 0
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
            f"run_all.SERVICE_SCRIPTS['edgewarn'] = {str(failing_child)!r}\n"
            "sys.exit(run_all.main(['--services', 'edgewarn']))\n"
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

    def test_unexpected_child_exit_is_nonzero(self, tmp_path, src_root, monkeypatch):
        quitter = _sleeper(tmp_path, name="quitter.py", code=3)
        sleeper = _sleeper(tmp_path)
        monkeypatch.setattr(run_all, "SERVICE_SCRIPTS", {
            "edgewarn": sleeper, "ewmrs": quitter, "nexrad": sleeper,
        })
        commands = run_all.build_service_commands(_args(), list(run_all.SERVICE_SCRIPTS), src_root)

        started = time.time()
        code = run_all.supervise(commands, src_root=src_root)
        elapsed = time.time() - started

        assert code == 1
        # The remaining children were torn down promptly, not waited out.
        assert elapsed < 20

    def test_survivors_are_escalated_to_sigkill_within_grace(
        self, tmp_path, src_root, monkeypatch
    ):
        stubborn = _sleeper(tmp_path)  # ignores SIGTERM entirely
        monkeypatch.setattr(run_all, "SERVICE_SCRIPTS", {"edgewarn": stubborn})
        commands = run_all.build_service_commands(_args(), ["edgewarn"], src_root)

        stop = threading.Event()

        def run():
            run_all.supervise(commands, src_root=src_root, stop_event=stop)

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
            f"run_all.SERVICE_SCRIPTS['edgewarn'] = {str(worker)!r}\n"
            "sys.exit(run_all.main(['--services', 'edgewarn']))\n",
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
            assert launcher.wait(timeout=10) == 1

            deadline = time.time() + 5
            while Path(f"/proc/{pid}").exists() and time.time() < deadline:
                time.sleep(0.05)
            assert not Path(f"/proc/{pid}").exists()
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()


def test_launcher_imports_no_pipeline_module():
    """Import-isolation probe: the launcher stays a thin supervisor."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys\nsys.path.insert(0, 'src')\nimport run_all\n"
         "print('EWMRS:', 'EWMRS' in sys.modules)\n"
         "print('NEXRAD:', 'common.ingest.nexrad' in sys.modules)\n"
         "print('EDGEWARN:', 'EdgeWARN' in sys.modules)"],
        capture_output=True, text=True, timeout=120, cwd=os.getcwd(),
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "EWMRS: False" in result.stdout
    assert "NEXRAD: False" in result.stdout
    assert "EDGEWARN: False" in result.stdout
