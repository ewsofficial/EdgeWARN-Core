"""Phase 3 contracts for safe package configuration mutation."""

from __future__ import annotations

import shutil
import stat
import threading
import time
from pathlib import Path

import pytest
import yaml

from common.config import loader
from edgewarn_cli import configure
from edgewarn_cli.main import main


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def config_tree(tmp_path):
    root = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", root)
    yield root
    loader.reset_cache()


@pytest.mark.parametrize(
    ("text", "expected", "expected_type"),
    [
        ("2048", 2048, int),
        ("true", True, bool),
        ("null", None, type(None)),
        ("3.5", 3.5, float),
        ('"2048"', "2048", str),
    ],
)
def test_scalar_assignments_retain_type(text, expected, expected_type):
    result = configure.parse_scalar(text)
    assert result == expected
    assert type(result) is expected_type


@pytest.mark.parametrize(
    "text",
    ["[1, 2]", "{a: 1}", "first\n---\nsecond", "&value anchored", "*alias", "!!str 1"],
)
def test_scalar_parser_rejects_structures_documents_and_yaml_indirection(text):
    with pytest.raises(configure.ConfigureError):
        configure.parse_scalar(text)


def test_dotted_path_mapping_keys_take_precedence_over_indices():
    document = {"numeric": {"0": {"value": 1}}, "items": [{"value": 2}]}

    parent, key = configure.resolve_leaf(document, ("numeric", "0", "value"))
    assert parent[key] == 1
    parent, key = configure.resolve_leaf(document, ("items", "0", "value"))
    assert parent[key] == 2


def test_escaped_dotted_mapping_key_can_be_edited(config_tree):
    result = configure.edit_configuration(
        config_tree,
        r"ingest.mrms.ncep_https.directory_map.EchoTop_18_00\.50",
        '"changed"',
    )

    document = yaml.safe_load((config_tree / "ingest.yaml").read_text(encoding="utf-8"))
    assert document["mrms"]["ncep_https"]["directory_map"]["EchoTop_18_00.50"] == "changed"
    assert result.dotted_path.endswith(r"EchoTop_18_00\.50")


@pytest.mark.parametrize(
    "target",
    ["", ".runtime.key", "runtime..key", "runtime.key.", "runtime", "runtime.yaml.run"],
)
def test_invalid_dotted_targets_are_rejected(target):
    with pytest.raises(configure.ConfigureError):
        configure.parse_dotted_target(target, loader.CONFIG_NAMES)


@pytest.mark.parametrize("segments", [("missing",), ("items", "-1"), ("items", "2")])
def test_missing_negative_and_out_of_range_paths_are_rejected(segments):
    with pytest.raises(configure.ConfigureError):
        configure.resolve_leaf({"items": [1]}, segments)


def test_example_edit_preserves_comments_order_permissions_and_newline(config_tree):
    target = config_tree / "ewmrs_pipeline.yaml"
    before = target.read_text(encoding="utf-8")
    mode = stat.S_IMODE(target.stat().st_mode)

    result = configure.edit_configuration(
        config_tree, "ewmrs_pipeline.workers.budget_mb.goes", "2048"
    )

    after = target.read_text(encoding="utf-8")
    loaded = yaml.safe_load(after)
    assert result.old_value == 1200.0
    assert result.new_value == 2048
    assert loaded["workers"]["budget_mb"]["goes"] == 2048
    assert "# Per-worker memory estimates" in after
    assert after.index("goes:") < after.index("default:")
    assert after.endswith("\n") == before.endswith("\n")
    assert stat.S_IMODE(target.stat().st_mode) == mode


@pytest.mark.parametrize("value", ["on", "off", "yes", "no", "null"])
def test_ambiguous_yaml_strings_remain_strings_on_disk(config_tree, value):
    configure.edit_configuration(
        config_tree, "runtime.run.ctam_module_dir", f'"{value}"'
    )

    loaded = yaml.safe_load((config_tree / "runtime.yaml").read_text(encoding="utf-8"))
    assert loaded["run"]["ctam_module_dir"] == value
    assert isinstance(loaded["run"]["ctam_module_dir"], str)


def test_serialized_document_is_validated_before_atomic_replace(config_tree, monkeypatch):
    called = False

    def invalid_runtime_parse(_content):
        raise loader.ConfigError("runtime.yaml", "run", "serialized mismatch")

    def replacement_must_not_run(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr(configure, "_runtime_parse", invalid_runtime_parse)
    monkeypatch.setattr(configure, "_atomic_replace", replacement_must_not_run)

    with pytest.raises(loader.ConfigError, match="serialized mismatch"):
        configure.edit_configuration(
            config_tree, "runtime.run.ctam_module_dir", '"on"'
        )
    assert called is False


def test_cli_prints_only_edit_summary(config_tree, capsys):
    assert main(
        [
            "configure",
            "--config-path",
            str(config_tree),
            "runtime.run.disable_nexrad",
            "true",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "file: runtime.yaml" in output
    assert "path: run.disable_nexrad" in output
    assert "old: False" in output
    assert "new: True" in output
    assert "validation: passed" in output


def test_cli_validation_error_exits_two_without_writing(config_tree, capsys):
    target = config_tree / "runtime.yaml"
    before = target.read_bytes()

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "configure",
                "--config-path",
                str(config_tree),
                "runtime.run.disable_nexrad",
                "not-a-boolean",
            ]
        )

    assert excinfo.value.code == 2
    assert "not of type boolean" in capsys.readouterr().err
    assert target.read_bytes() == before


def test_cli_write_error_returns_one_without_writing(config_tree, capsys):
    target = config_tree / "runtime.yaml"
    before = target.read_bytes()
    target.chmod(0o444)
    try:
        assert main(
            [
                "configure",
                "--config-path",
                str(config_tree),
                "runtime.run.disable_nexrad",
                "true",
            ]
        ) == 1
        assert "read-only" in capsys.readouterr().err
        assert target.read_bytes() == before
    finally:
        target.chmod(0o644)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("runtime.run.does_not_exist", "true"),
        ("runtime.run.disable_nexrad", "not-a-boolean"),
        ("ewmrs_pipeline.workers.psutil_fallback_max", "0"),
    ],
)
def test_path_and_schema_failures_leave_original_bytes(config_tree, path, value):
    target = config_tree / f"{path.split('.', 1)[0]}.yaml"
    before = target.read_bytes()
    with pytest.raises((configure.ConfigureError, loader.ConfigError)):
        configure.edit_configuration(config_tree, path, value)
    assert target.read_bytes() == before


def test_malformed_tree_leaves_target_unchanged(config_tree):
    target = config_tree / "runtime.yaml"
    before = target.read_bytes()
    (config_tree / "nws.yaml").write_text("bad: [", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        configure.edit_configuration(config_tree, "runtime.run.disable_nexrad", "true")
    assert target.read_bytes() == before


def test_symlink_escape_is_rejected_before_external_file_is_read(config_tree, tmp_path):
    external = tmp_path / "external.yaml"
    external.write_text("schema_version: 1\n", encoding="utf-8")
    target = config_tree / "runtime.yaml"
    target.unlink()
    target.symlink_to(external)

    with pytest.raises(configure.ConfigureError, match="outside configuration root"):
        configure.edit_configuration(config_tree, "runtime.schema_version", "1")
    assert external.read_text(encoding="utf-8") == "schema_version: 1\n"


def test_read_only_target_is_unchanged(config_tree):
    target = config_tree / "runtime.yaml"
    before = target.read_bytes()
    target.chmod(0o444)
    try:
        with pytest.raises(configure.ConfigureIOError, match="read-only"):
            configure.edit_configuration(config_tree, "runtime.run.disable_nexrad", "true")
        assert target.read_bytes() == before
    finally:
        target.chmod(0o644)


@pytest.mark.parametrize("failure_point", ["temporary", "write", "fsync", "replace"])
def test_atomic_write_failures_leave_original_bytes(config_tree, monkeypatch, failure_point):
    target = config_tree / "runtime.yaml"
    before = target.read_bytes()

    if failure_point == "temporary":
        monkeypatch.setattr(configure.tempfile, "mkstemp", lambda **_kwargs: (_ for _ in ()).throw(OSError("write")))
    elif failure_point == "write":
        monkeypatch.setattr(
            configure,
            "_write_temporary",
            lambda *_args: (_ for _ in ()).throw(OSError("write")),
        )
    elif failure_point == "fsync":
        monkeypatch.setattr(configure.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
    else:
        monkeypatch.setattr(configure.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))

    with pytest.raises(configure.ConfigureIOError):
        configure.edit_configuration(config_tree, "runtime.run.disable_nexrad", "true")
    assert target.read_bytes() == before


def test_post_write_validation_failure_rolls_back(config_tree, monkeypatch):
    target = config_tree / "runtime.yaml"
    before = target.read_bytes()
    real_validate = loader.validate_all_configs
    calls = 0

    def fail_once_after_write(*, config_dir=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise loader.ConfigError("runtime.yaml", "run", "simulated post-write failure")
        return real_validate(config_dir=config_dir)

    monkeypatch.setattr(loader, "validate_all_configs", fail_once_after_write)
    with pytest.raises(configure.ConfigureIOError, match="original file restored"):
        configure.edit_configuration(config_tree, "runtime.run.disable_nexrad", "true")

    assert target.read_bytes() == before
    real_validate(config_dir=config_tree)


def test_concurrent_editors_are_serialized_without_lost_updates(config_tree, monkeypatch):
    real_load = configure._load_document
    first_load_started = threading.Event()

    def slow_first_load(target):
        result = real_load(target)
        if not first_load_started.is_set():
            first_load_started.set()
            time.sleep(0.2)
        return result

    monkeypatch.setattr(configure, "_load_document", slow_first_load)
    errors = []

    def edit(path):
        try:
            configure.edit_configuration(config_tree, path, "true")
        except Exception as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    first = threading.Thread(target=edit, args=("runtime.run.disable_nexrad",))
    second = threading.Thread(target=edit, args=("runtime.run.disable_ewmrs",))
    first.start()
    assert first_load_started.wait(timeout=5)
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    document = yaml.safe_load((config_tree / "runtime.yaml").read_text(encoding="utf-8"))
    assert document["run"]["disable_nexrad"] is True
    assert document["run"]["disable_ewmrs"] is True
