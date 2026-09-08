"""Phase 4 contracts for the interactive package configuration editor."""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from textual.widgets import DataTable, Input, Label, OptionList, Static

from common.config import loader
from edgewarn_cli.main import main
from edgewarn_cli.tui import (
    EdgeWarnConfigApp,
    EditScreen,
    FileSelectionScreen,
    VariableBrowserScreen,
    flatten_document,
    load_catalog,
    schema_summary,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def config_tree(tmp_path):
    root = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", root)
    yield root
    loader.reset_cache()


def test_no_argument_configure_rejects_non_tty(config_tree, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["configure", "--config-path", str(config_tree)])

    assert excinfo.value.code == 2
    error = capsys.readouterr().err
    assert "requires TTY stdin and stdout" in error
    assert "FILE.KEY VALUE" in error


def test_no_argument_configure_launches_tui_on_tty(config_tree, monkeypatch):
    class TTYBuffer(io.StringIO):
        def isatty(self):
            return True

    calls = []
    monkeypatch.setattr(sys, "stdin", TTYBuffer())
    monkeypatch.setattr(sys, "stdout", TTYBuffer())
    monkeypatch.setattr(
        "edgewarn_cli.tui.run_tui",
        lambda root, names: calls.append((root, names)) or 0,
    )

    assert main(["configure", "--config-path", str(config_tree)]) == 0
    assert calls == [(config_tree.resolve(), loader.CONFIG_NAMES)]


def test_flattening_uses_canonical_sequence_paths_and_skips_aliases():
    shared = [10, 20]
    document = {"primary": shared, "alias": shared, "enabled": True}
    schema = {
        "type": "object",
        "properties": {
            "primary": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 2,
                "maxItems": 2,
            },
            "alias": {
                "type": "array",
                "items": {"type": "integer"},
            },
            "enabled": {"type": "boolean"},
        },
    }

    leaves = flatten_document(document, schema)

    assert [leaf.path for leaf in leaves] == ["primary.0", "primary.1", "enabled"]
    assert leaves[0].constraints == "type=integer; >= 0; items=2"


def test_flattening_preserves_typed_segments_for_dotted_keys():
    leaves = flatten_document({"directory_map": {"EchoTop_18_00.50": "x"}}, {})

    assert leaves[0].path == r"directory_map.EchoTop_18_00\.50"
    assert leaves[0].segments == ("directory_map", "EchoTop_18_00.50")


def test_schema_summary_covers_types_enums_numeric_and_array_bounds():
    assert schema_summary(
        {
            "type": ["integer", "null"],
            "enum": [1, 2, None],
            "exclusiveMinimum": 0,
            "maximum": 2,
            "minItems": 1,
            "maxItems": 3,
        }
    ) == "type=integer|null; enum=1, 2, None; <= 2; > 0; items>=1; items<=3"


@pytest.mark.asyncio
async def test_file_selection_and_every_runtime_leaf_render(config_tree):
    expected = load_catalog(config_tree, "runtime")
    app = EdgeWarnConfigApp(config_tree, loader.CONFIG_NAMES)

    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, FileSelectionScreen)
        assert (
            app.screen.query_one("#file-title", Label).content
            == "Select Configuration File:"
        )
        file_list = app.screen.query_one("#config-file", OptionList)
        assert [
            str(file_list.get_option_at_index(index).prompt)
            for index in range(file_list.option_count)
        ] == [f"{name}.yaml" for name in loader.CONFIG_NAMES]

        file_list.highlighted = list(loader.CONFIG_NAMES).index("runtime")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, VariableBrowserScreen)
        table = app.screen.query_one("#variables", DataTable)
        assert table.row_count == len(expected)
        assert {str(key.value) for key in table.rows} == {
            leaf.path for leaf in expected
        }
        assert table.get_row("run.disable_nexrad") == [
            "run.disable_nexrad",
            "false",
            "boolean",
            "type=boolean",
        ]


async def _open_runtime_leaf(app, pilot, path):
    from common.config import loader as config_loader

    file_list = app.screen.query_one("#config-file", OptionList)
    file_list.highlighted = list(config_loader.CONFIG_NAMES).index("runtime")
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    table = app.screen.query_one("#variables", DataTable)
    table.move_cursor(row=table.get_row_index(path))
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, EditScreen)


@pytest.mark.asyncio
async def test_valid_save_updates_disk_and_refreshes_row(config_tree):
    app = EdgeWarnConfigApp(config_tree, loader.CONFIG_NAMES)

    async with app.run_test(size=(140, 45)) as pilot:
        await _open_runtime_leaf(app, pilot, "run.disable_nexrad")
        editor = app.screen.query_one("#editor-value", Input)
        editor.value = "true"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, VariableBrowserScreen)
        document = yaml.safe_load(
            (config_tree / "runtime.yaml").read_text(encoding="utf-8")
        )
        assert document["run"]["disable_nexrad"] is True
        table = app.screen.query_one("#variables", DataTable)
        assert table.get_row("run.disable_nexrad")[1] == "true"
        assert "validation passed" in str(
            app.screen.query_one("#browser-status", Static).content
        )


@pytest.mark.asyncio
async def test_invalid_save_stays_open_and_does_not_write(config_tree):
    target = config_tree / "runtime.yaml"
    before = target.read_bytes()
    app = EdgeWarnConfigApp(config_tree, loader.CONFIG_NAMES)

    async with app.run_test(size=(140, 45)) as pilot:
        await _open_runtime_leaf(app, pilot, "run.disable_nexrad")
        app.screen.query_one("#editor-value", Input).value = "not-a-boolean"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, EditScreen)
        error = app.screen.query_one("#editor-error", Static)
        assert "not of type boolean" in str(error.content)
        assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_escape_navigation_and_q_quit_behavior(config_tree):
    app = EdgeWarnConfigApp(config_tree, loader.CONFIG_NAMES)

    async with app.run_test(size=(140, 45)) as pilot:
        await _open_runtime_leaf(app, pilot, "run.disable_nexrad")
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, EditScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, VariableBrowserScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, FileSelectionScreen)

        await pilot.press("q")
        await pilot.pause()
        assert not app.is_running
