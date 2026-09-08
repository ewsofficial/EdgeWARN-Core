"""Phase 2 contracts for topology-aware ``edgewarn run`` dispatch."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import run_all
from common.config import loader as config_loader
from edgewarn_cli import main as cli
from edgewarn_cli import run as package_run


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_topology_table_is_complete_and_ordered():
    assert dict(package_run.TOPOLOGIES) == {
        "all": ("edgewarn", "ewmrs", "nexrad"),
        "core": ("edgewarn",),
        "ewmrs": ("edgewarn", "ewmrs"),
        "nexrad": ("nexrad",),
    }

    with pytest.raises(TypeError):
        package_run.TOPOLOGIES["other"] = ("edgewarn",)


@pytest.mark.parametrize(
    ("entries", "services", "message"),
    [
        ([('core', 'not-json')], ("edgewarn",), "not valid JSON"),
        ([('core', '{}')], ("edgewarn",), "must be a JSON array"),
        ([('core', '[1]')], ("edgewarn",), "only strings"),
        ([('ewmrs', '[]')], ("edgewarn",), "not part"),
        ([('bogus', '[]')], ("edgewarn",), "unknown worker"),
        ([('core', '[]'), ('core', '[]')], ("edgewarn",), "only once"),
        ([('core', '[\"--config-dir=/tmp/cfg\"]')], ("edgewarn",), "wrapper"),
        ([('core', '[\"--services\", \"nexrad\"]')], ("edgewarn",), "wrapper"),
    ],
)
def test_invalid_worker_argv_is_rejected(entries, services, message):
    with pytest.raises(ValueError, match=message):
        package_run.parse_worker_argv(entries, services)


def test_worker_argv_maps_core_to_edgewarn_without_retokenizing():
    parsed = package_run.parse_worker_argv(
        [
            ("core", '["--lat_limits", "20", "55", "value with spaces"]'),
            ("ewmrs", '["--disable-wpc"]'),
        ],
        package_run.TOPOLOGIES["ewmrs"],
    )

    assert parsed == {
        "edgewarn": ("--lat_limits", "20", "55", "value with spaces"),
        "ewmrs": ("--disable-wpc",),
    }


def test_worker_relative_base_directories_resolve_from_invocation_directory(tmp_path):
    normalized = package_run.canonicalize_worker_paths(
        {
            "edgewarn": ("--base-dir", "./runtime"),
            "ewmrs": ("--base_dir=~/ewmrs-runtime",),
        },
        tmp_path,
    )

    assert normalized["edgewarn"] == ("--base-dir", str(tmp_path / "runtime"))
    assert normalized["ewmrs"][0].startswith("--base_dir=/")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("--drop-ofset", "4"), "invalid arguments"),
        (("--drop-offset",), "invalid arguments"),
        (("--drop-offset", "not-a-number"), "invalid arguments"),
        (("--help",), "one-shot argument"),
        (("--check-ctam-modules",), "one-shot argument"),
    ],
)
def test_worker_argument_preflight_rejects_before_spawn(argv, message):
    with pytest.raises(ValueError, match=message):
        package_run.preflight_worker_argv({"edgewarn": argv})


def test_worker_argument_preflight_accepts_service_grammar():
    package_run.preflight_worker_argv(
        {
            "edgewarn": ("--drop-offset", "4"),
            "ewmrs": ("--disable-wpc",),
            "nexrad": ("--base-dir", "/runtime"),
        }
    )


@pytest.mark.parametrize(
    ("service", "abbreviation"),
    [
        ("edgewarn", ("--drop-off", "4")),
        ("ewmrs", ("--disable-wp",)),
        ("nexrad", ("--prof",)),
    ],
)
def test_worker_argument_preflight_rejects_abbreviations(service, abbreviation):
    with pytest.raises(ValueError, match="invalid arguments"):
        package_run.preflight_worker_argv({service: abbreviation})


@pytest.mark.parametrize(
    ("service", "argv"),
    [
        ("edgewarn", ("--lat_limits", "20", "55", "--disable-ctam")),
        ("ewmrs", ("--disable-wpc", "--no-profile")),
        ("nexrad", ("--profile", "--base-dir", "/runtime")),
    ],
)
def test_preflight_uses_the_direct_launcher_grammar(service, argv, monkeypatch):
    from util.cli import build_service_parser

    direct_parser = build_service_parser(service, add_help=False)
    direct = direct_parser.parse_args(argv)
    calls = []

    def parser_for(requested_service, *, add_help):
        calls.append((requested_service, add_help))
        return direct_parser

    monkeypatch.setattr("util.cli.build_service_parser", parser_for)
    package_run.preflight_worker_argv({service: argv})

    assert calls == [(service, False)]
    assert direct_parser.parse_args(argv) == direct


@pytest.mark.parametrize("one_shot", ["--list-ctam-modules", "--check-ctam-modules"])
def test_primary_one_shot_modes_cannot_bypass_supervision(one_shot):
    with pytest.raises(ValueError, match="one-shot argument"):
        package_run.preflight_worker_argv({"edgewarn": (one_shot,)})


def test_dispatch_validates_before_building_and_scopes_worker_argv(monkeypatch, tmp_path):
    events = []

    from edgewarn_cli import config_path

    monkeypatch.setattr(config_path, "resolve_config_root", lambda path: tmp_path.resolve())

    monkeypatch.setattr(config_loader, "reset_cache", lambda: events.append("reset"))

    def export(path):
        events.append(("export", path))
        return tmp_path.resolve()

    monkeypatch.setattr(config_loader, "export_config_root", export)
    monkeypatch.setattr(
        config_loader,
        "validate_all_configs",
        lambda **kwargs: events.append(("validate", kwargs["config_dir"])),
    )
    monkeypatch.setattr(config_loader, "load_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "common.config.overlay.resolve_base_dir", lambda *_args, **_kwargs: tmp_path / "runtime"
    )

    def build(args, services, src_root, *, service_argv):
        events.append(("build", services, service_argv, args.config_dir, src_root))
        return {service: [service] for service in services}

    monkeypatch.setattr(run_all, "build_service_commands", build)
    monkeypatch.setattr(
        run_all, "resolve_services", lambda args, requested: tuple(requested)
    )
    monkeypatch.setattr(
        run_all,
        "supervise",
        lambda commands, *, src_root: events.append(("supervise", commands)) or 0,
    )

    assert cli.main(
        [
            "run",
            "ewmrs",
            "--config-path",
            str(tmp_path),
            "--args",
            "core",
            '["--profile"]',
            "--args",
            "ewmrs",
            '["--disable-wpc"]',
        ]
    ) == 0

    assert events[:3] == [
        "reset",
        ("export", tmp_path),
        ("validate", tmp_path.resolve()),
    ]
    assert events[3][0:4] == (
        "build",
        ("edgewarn", "ewmrs"),
        {"edgewarn": ("--profile",), "ewmrs": ("--disable-wpc",)},
        str(tmp_path.resolve()),
    )
    assert events[4] == (
        "supervise",
        {"edgewarn": ["edgewarn"], "ewmrs": ["ewmrs"]},
    )


def test_dispatch_resolves_persisted_topology_before_building(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(
        "edgewarn_cli.config_path.resolve_config_root", lambda _path: tmp_path
    )
    monkeypatch.setattr(config_loader, "reset_cache", lambda: None)
    monkeypatch.setattr(config_loader, "export_config_root", lambda _path: None)
    monkeypatch.setattr(config_loader, "validate_all_configs", lambda **_kwargs: None)
    monkeypatch.setattr(config_loader, "load_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "common.config.overlay.resolve_base_dir", lambda *_args, **_kwargs: tmp_path / "runtime"
    )
    monkeypatch.setattr(
        run_all, "resolve_services", lambda args, requested: ["edgewarn"]
    )
    monkeypatch.setattr(
        run_all,
        "build_service_commands",
        lambda args, services, src_root, **kwargs: events.append(tuple(services)) or {},
    )
    monkeypatch.setattr(run_all, "supervise", lambda commands, *, src_root: 0)

    assert cli.main(["run", "all", "--config-path", str(tmp_path)]) == 0
    assert events == [("edgewarn",)]


def test_missing_config_exits_two_before_command_construction(monkeypatch, tmp_path):
    called = False

    def should_not_build(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("commands must not be constructed")

    monkeypatch.setattr(run_all, "build_service_commands", should_not_build)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "core", "--config-path", str(tmp_path / "missing")])

    assert excinfo.value.code == 2
    assert called is False


def test_malformed_config_exits_two_before_command_construction(monkeypatch, tmp_path):
    from yaml import YAMLError

    called = False

    monkeypatch.setattr(config_loader, "reset_cache", lambda: None)
    monkeypatch.setattr(config_loader, "export_config_root", lambda _path: tmp_path)

    def fail_validation(**_kwargs):
        raise YAMLError("runtime.yaml: malformed")

    monkeypatch.setattr(config_loader, "validate_all_configs", fail_validation)

    def should_not_build(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(run_all, "build_service_commands", should_not_build)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "core", "--config-path", str(tmp_path)])

    assert excinfo.value.code == 2
    assert called is False


def test_nexrad_launcher_owns_ingest_and_render(monkeypatch):
    from util.runtime import nexrad_service

    registrations = []

    class Supervisor:
        def add(self, name, target, **kwargs):
            registrations.append((name, target, kwargs))

    monkeypatch.setattr(nexrad_service, "heartbeat_stale_seconds", lambda: 10)
    monkeypatch.setattr(nexrad_service, "heartbeat_startup_grace_seconds", lambda: 20)
    nexrad_service.register_nexrad_supervision(
        Supervisor(),
        base_dir="/runtime",
        nexrad_log_queue=object(),
        nexrad_heartbeat_path="/runtime/heartbeat.json",
    )

    assert [name for name, _target, _kwargs in registrations] == [
        "NEXRAD Render",
        "NEXRAD Ingest",
    ]
    assert registrations[0][2]["daemon"] is True
    assert registrations[1][2]["daemon"] is False


def test_sigterm_to_package_runner_is_forwarded_and_reaped(tmp_path):
    sleeper = tmp_path / "run_edgewarn.py"
    sleeper.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "import run_all\n"
        f"run_all.SERVICE_SCRIPTS['edgewarn'] = {str(sleeper)!r}\n"
        "from edgewarn_cli.main import main\n"
        f"sys.exit(main(['run', 'core', '--config-path', {str(REPO_ROOT / 'config')!r}]))\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        cwd=tmp_path,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    try:
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    except BaseException:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        raise
