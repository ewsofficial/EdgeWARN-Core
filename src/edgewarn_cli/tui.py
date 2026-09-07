"""Interactive Textual editor for validated EdgeWARN configuration catalogs.

This module is imported only for an interactive ``edgewarn configure`` call.
All writes deliberately go through :func:`edit_configuration`; the widgets in
this module only discover and display leaves and collect a replacement scalar.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, Static

from edgewarn_cli.configure import (
    ConfigureError,
    ConfigureIOError,
    DottedTarget,
    _load_document,
    _safe_target,
    edit_configuration,
    format_segments,
)


@dataclass(frozen=True)
class ConfigLeaf:
    """One editable YAML scalar and the context shown in the browser."""

    path: str
    segments: tuple[str, ...]
    value: Any
    type_name: str
    constraints: str


@dataclass(frozen=True)
class EditOutcome:
    """Successful editor result returned to the variable browser."""

    path: str
    value: Any


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def scalar_text(value: Any) -> str:
    """Return an unambiguous YAML scalar suitable for the editor input."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def schema_summary(
    schema: Mapping[str, Any],
    *,
    array_schema: Mapping[str, Any] | None = None,
) -> str:
    """Render the supported schema keywords as a compact, non-authoritative hint."""
    parts: list[str] = []
    type_spec = schema.get("type")
    if isinstance(type_spec, list):
        parts.append("type=" + "|".join(str(item) for item in type_spec))
    elif type_spec is not None:
        parts.append(f"type={type_spec}")

    if "enum" in schema:
        parts.append("enum=" + ", ".join(repr(item) for item in schema["enum"]))
    if "const" in schema:
        parts.append(f"const={schema['const']!r}")

    for keyword, operator in (
        ("minimum", ">="),
        ("maximum", "<="),
        ("exclusiveMinimum", ">"),
        ("exclusiveMaximum", "<"),
    ):
        if keyword in schema:
            parts.append(f"{operator} {schema[keyword]}")

    bounds = array_schema if array_schema is not None else schema
    minimum = bounds.get("minItems")
    maximum = bounds.get("maxItems")
    if minimum is not None and maximum is not None and minimum == maximum:
        parts.append(f"items={minimum}")
    else:
        if minimum is not None:
            parts.append(f"items>={minimum}")
        if maximum is not None:
            parts.append(f"items<={maximum}")
    return "; ".join(parts)


def flatten_document(document: Any, schema: Mapping[str, Any]) -> tuple[ConfigLeaf, ...]:
    """Flatten unique YAML scalar leaves to canonical dotted assignment paths.

    Containers reached again through a YAML alias are skipped. Anchored scalars
    receive the same treatment without confusing ordinary repeated primitives
    (which Python may intern and therefore give the same object identity).
    """
    leaves: list[ConfigLeaf] = []
    seen_containers: set[int] = set()
    seen_anchored_scalars: set[int] = set()

    def visit(
        value: Any,
        current_schema: Mapping[str, Any],
        path: tuple[str, ...],
        parent_array_schema: Mapping[str, Any] | None = None,
    ) -> None:
        is_mapping = isinstance(value, Mapping)
        is_sequence = isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        )
        if is_mapping or is_sequence:
            identity = id(value)
            if identity in seen_containers:
                return
            seen_containers.add(identity)
        else:
            anchor = getattr(value, "anchor", None)
            if getattr(anchor, "value", None):
                identity = id(value)
                if identity in seen_anchored_scalars:
                    return
                seen_anchored_scalars.add(identity)

        if is_mapping:
            properties = current_schema.get("properties", {})
            additional = current_schema.get("additionalProperties", {})
            for key, child in value.items():
                key_text = str(key)
                child_schema = properties.get(key_text)
                if child_schema is None:
                    child_schema = additional if isinstance(additional, Mapping) else {}
                visit(child, child_schema, path + (key_text,))
            return

        if is_sequence:
            item_schema = current_schema.get("items", {})
            if not isinstance(item_schema, Mapping):
                item_schema = {}
            for index, child in enumerate(value):
                visit(child, item_schema, path + (str(index),), current_schema)
            return

        leaves.append(
            ConfigLeaf(
                path=format_segments(path),
                segments=path,
                value=value,
                type_name=_type_name(value),
                constraints=schema_summary(
                    current_schema, array_schema=parent_array_schema
                ),
            )
        )

    visit(document, schema, ())
    return tuple(leaves)


def load_catalog(config_root: Path, name: str) -> tuple[ConfigLeaf, ...]:
    """Load one already-validated catalog and its schema for presentation.

    The top-level ``schema_version`` marker is omitted: it must never change.
    """
    target = _safe_target(config_root, name)
    document, _original, _mode, _newline = _load_document(target)
    schema_path = config_root / "schema" / f"{name}.schema.json"
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    return tuple(
        leaf for leaf in flatten_document(document, schema) if leaf.path != "schema_version"
    )


class FileSelectionScreen(Screen[None]):
    """First layer: choose one registered configuration catalog."""

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, config_names: Sequence[str]) -> None:
        super().__init__()
        self.config_names = tuple(config_names)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Select Configuration File:", id="file-title")
        yield OptionList(
            *(f"{name}.yaml" for name in self.config_names),
            id="config-file",
        )
        yield Footer()

    @on(OptionList.OptionSelected, "#config-file")
    def select_file(self, event: OptionList.OptionSelected) -> None:
        self.app.open_catalog(self.config_names[event.option_index])

    def action_quit(self) -> None:
        self.app.exit(0)


class VariableBrowserScreen(Screen[None]):
    """Second layer: show all editable leaves for one catalog."""

    BINDINGS = [
        Binding("escape", "back", "Files"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config_root: Path, name: str) -> None:
        super().__init__()
        self.config_root = config_root
        self.catalog_name = name
        self.leaves: dict[str, ConfigLeaf] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"{self.catalog_name}.yaml", id="catalog-title")
        yield DataTable(id="variables", zebra_stripes=True, cursor_type="row")
        yield Static("Select a row to edit its YAML scalar.", id="browser-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#variables", DataTable)
        table.add_columns("Path", "Current value", "Type", "Schema constraints")
        self.refresh_document()
        table.focus()

    def refresh_document(self) -> None:
        leaves = load_catalog(self.config_root, self.catalog_name)
        self.leaves = {leaf.path: leaf for leaf in leaves}
        table = self.query_one("#variables", DataTable)
        table.clear(columns=False)
        for leaf in leaves:
            table.add_row(
                leaf.path,
                scalar_text(leaf.value),
                leaf.type_name,
                leaf.constraints,
                key=leaf.path,
            )

    @on(DataTable.RowSelected, "#variables")
    def edit_row(self, event: DataTable.RowSelected) -> None:
        path = str(event.row_key.value)
        leaf = self.leaves.get(path)
        if leaf is not None:
            self.app.push_screen(
                EditScreen(self.config_root, self.catalog_name, leaf),
                self.edit_closed,
            )

    def edit_closed(self, outcome: EditOutcome | None) -> None:
        if outcome is None:
            return
        self.refresh_document()
        self.query_one("#browser-status", Static).update(
            f"Saved {outcome.path} = {scalar_text(outcome.value)}; validation passed."
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit(0)


class EditScreen(ModalScreen[EditOutcome | None]):
    """Scalar editor whose save action delegates to the Phase 3 transaction."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, config_root: Path, name: str, leaf: ConfigLeaf) -> None:
        super().__init__()
        self.config_root = config_root
        self.catalog_name = name
        self.leaf = leaf

    def compose(self) -> ComposeResult:
        with Vertical(id="editor"):
            yield Label(
                f"Edit {self.catalog_name}.{self.leaf.path}", id="editor-title"
            )
            yield Static(
                self.leaf.constraints or "No schema constraints", id="editor-hint"
            )
            yield Input(value=scalar_text(self.leaf.value), id="editor-value")
            yield Static("", id="editor-error")
            yield Footer()

    def on_mount(self) -> None:
        editor = self.query_one("#editor-value", Input)
        editor.focus()
        editor.select_all()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        from common.config import loader
        from yaml import YAMLError

        value = self.query_one("#editor-value", Input).value
        try:
            result = edit_configuration(
                self.config_root,
                DottedTarget(self.catalog_name, self.leaf.segments),
                value,
            )
        except (ConfigureError, loader.ConfigError, ValueError, YAMLError) as exc:
            self.query_one("#editor-error", Static).update(str(exc))
            return
        except (ConfigureIOError, OSError) as exc:
            self.query_one("#editor-error", Static).update(f"write failed: {exc}")
            return
        self.dismiss(EditOutcome(result.dotted_path, result.new_value))


class EdgeWarnConfigApp(App[int]):
    """Textual application hosting file selection, browsing, and editing."""

    TITLE = "EdgeWARN Configuration Menu"

    CSS = """
    #file-title, #catalog-title, #editor-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #config-file {
        height: 1fr;
    }
    #catalog-title, #browser-status {
        padding: 0 1;
    }
    #variables {
        height: 1fr;
    }
    EditScreen {
        align: center middle;
    }
    #editor {
        width: 80%;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #editor-error {
        color: $error;
        min-height: 1;
        margin-top: 1;
    }
    """

    def __init__(self, config_root: Path, config_names: Sequence[str]) -> None:
        super().__init__()
        self.config_root = config_root
        self.config_names = tuple(config_names)

    def on_mount(self) -> None:
        self.push_screen(FileSelectionScreen(self.config_names))

    def open_catalog(self, name: str) -> None:
        self.push_screen(VariableBrowserScreen(self.config_root, name))


def run_tui(config_root: Path, config_names: Sequence[str]) -> int:
    """Run the interactive editor and normalize its exit result."""
    result = EdgeWarnConfigApp(config_root, config_names).run()
    return int(result or 0)
