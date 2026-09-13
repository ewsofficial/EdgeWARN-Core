"""Phase 0 characterization of cell-history semantics.

Phase 3 of ``plans/modular-ctam-internal-api-plan.md`` moves history publication
under a single coordinator and requires two current behaviors to survive:
"inactive cells are not refreshed and the same timestamp replaces the last entry
rather than duplicating it". Both are frozen here.

These tests observe the *decision* -- append, replace, or no write at all -- by
substituting a recorder for ``atomic_write_json`` rather than reading the file
back. Two reasons. The decision is the semantic the plan preserves, and the file
write is incidental to it. And ``util.atomic.atomic_write_json`` is broken on
Windows: ``atomic_output_path`` calls ``os.fsync`` on a read-only descriptor
(``src/util/atomic.py:36``), which returns ``EBADF`` there. Reading the file back
would make these tests pass on Linux and fail on Windows for a reason unrelated
to CTAM. The no-write cases still assert real mtime preservation, because the
skip happens before any write is attempted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import util.file as fs
from tests.core.ctam.baseline import assert_baseline

pytestmark = pytest.mark.ctam

CELL_ID = 146904
TS_A = "2026-08-05T11:55:00+00:00"
TS_B = "2026-08-05T12:00:00+00:00"


@pytest.fixture
def history_env(tmp_path, monkeypatch):
    """Isolate ``fs.CELL_DIR`` and record history writes instead of performing them."""
    import EdgeWARN.process.integrate.history as history_mod

    cell_dir = tmp_path / "cells"
    cell_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fs, "CELL_DIR", cell_dir)

    writes = []

    def recorder(destination, value, *, indent=None, default=None):
        writes.append((destination, json.loads(json.dumps(value, default=str))))
        return destination

    monkeypatch.setattr(history_mod, "atomic_write_json", recorder)

    from util.io import IOManager

    manager = history_mod.CellHistoryManager(IOManager("[test]"))
    return manager, cell_dir, writes


def make_cell(timestamp=TS_B, cell_id=CELL_ID, **overrides):
    cell = {
        "id": cell_id,
        "timestamp": timestamp,
        "centroid": [35.25, 262.75],
        "max_refl": 58.5,
        "properties": {"p100EchoTop30": 11.5},
        "modules": {"StormProb": {"status": "success"}},
    }
    if timestamp is None:
        del cell["timestamp"]
    cell.update(overrides)
    return cell


def write_history(cell_dir, entries, cell_id=CELL_ID):
    path = cell_dir / f"{cell_id}.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# File format
# ----------------------------------------------------------------------

def test_history_file_is_a_bare_json_array(history_env):
    """No envelope: the file is a list of full cell snapshots."""
    manager, cell_dir, writes = history_env
    manager.update_cell_histories([make_cell()])

    assert len(writes) == 1
    destination, payload = writes[0]
    assert destination == cell_dir / f"{CELL_ID}.json"
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert_baseline("cell_history_first_entry", payload)


def test_history_entry_is_the_whole_cell_including_modules(history_env):
    """Module output is persisted into history, not just the live snapshot."""
    manager, _, writes = history_env
    manager.update_cell_histories([make_cell()])

    entry = writes[0][1][0]
    assert entry["modules"] == {"StormProb": {"status": "success"}}
    assert entry["timestamp"] == TS_B


# ----------------------------------------------------------------------
# Append vs replace
# ----------------------------------------------------------------------

def test_new_timestamp_appends(history_env):
    manager, cell_dir, writes = history_env
    write_history(cell_dir, [make_cell(timestamp=TS_A)])

    manager.update_cell_histories([make_cell(timestamp=TS_B)])

    payload = writes[0][1]
    assert [entry["timestamp"] for entry in payload] == [TS_A, TS_B]


def test_duplicate_timestamp_replaces_last_entry(history_env):
    """Reprocessing the same cycle refreshes rather than duplicating."""
    manager, cell_dir, writes = history_env
    stale = make_cell(timestamp=TS_B, max_refl=40.0)
    write_history(cell_dir, [make_cell(timestamp=TS_A), stale])

    manager.update_cell_histories([make_cell(timestamp=TS_B, max_refl=58.5)])

    payload = writes[0][1]
    assert [entry["timestamp"] for entry in payload] == [TS_A, TS_B]
    assert payload[-1]["max_refl"] == 58.5
    assert_baseline(
        "cell_history_duplicate_timestamp_replacement",
        [entry["timestamp"] for entry in payload],
    )


def test_only_the_last_entry_is_considered_for_replacement(history_env):
    """A duplicate of an *earlier* entry appends, creating a repeated timestamp.

    ``history.py`` compares only ``history[-1]``. This is a real edge in the
    current behavior, and the plan's single publication coordinator must either
    keep it or change it deliberately -- StormProb already has to defend against
    duplicate history timestamps when it builds its track.
    """
    manager, cell_dir, writes = history_env
    write_history(cell_dir, [make_cell(timestamp=TS_A), make_cell(timestamp=TS_B)])

    manager.update_cell_histories([make_cell(timestamp=TS_A)])

    payload = writes[0][1]
    assert [entry["timestamp"] for entry in payload] == [TS_A, TS_B, TS_A]


# ----------------------------------------------------------------------
# Cells that are skipped entirely
# ----------------------------------------------------------------------

def test_inactive_cell_is_not_written_and_mtime_is_preserved(history_env):
    """A cell without a top-level ``timestamp`` is inactive: no write at all.

    Absence of ``timestamp`` is the marker -- ``track.py`` only assigns one to
    cells matched in the current scan. The skip happens before any file access,
    which is what preserves mtime for consumers that poll it.
    """
    manager, cell_dir, writes = history_env
    path = write_history(cell_dir, [make_cell(timestamp=TS_A)])
    old_mtime = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    import os

    os.utime(path, (old_mtime, old_mtime))
    before = path.stat().st_mtime

    manager.update_cell_histories([make_cell(timestamp=None)])

    assert writes == []
    assert path.stat().st_mtime == before
    assert json.loads(path.read_text(encoding="utf-8"))[0]["timestamp"] == TS_A


def test_cell_without_id_is_skipped(history_env):
    manager, _, writes = history_env
    cell = make_cell()
    del cell["id"]

    manager.update_cell_histories([cell])

    assert writes == []


def test_empty_cell_list_writes_nothing(history_env):
    manager, _, writes = history_env
    manager.update_cell_histories([])
    assert writes == []


def test_unreadable_history_is_preserved_not_overwritten(history_env):
    """A non-list history file is left alone rather than replaced."""
    manager, cell_dir, writes = history_env
    path = cell_dir / f"{CELL_ID}.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    manager.update_cell_histories([make_cell()])

    assert writes == []
    assert json.loads(path.read_text(encoding="utf-8")) == {"not": "a list"}


def test_corrupt_history_is_preserved_not_overwritten(history_env):
    manager, cell_dir, writes = history_env
    path = cell_dir / f"{CELL_ID}.json"
    path.write_text("{ this is not json", encoding="utf-8")

    manager.update_cell_histories([make_cell()])

    assert writes == []
    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_legacy_properties_timestamp_is_promoted_and_removed(history_env):
    """A ``properties.timestamp`` is moved to the top level, not left in place."""
    manager, _, writes = history_env
    cell = make_cell(timestamp=None)
    cell["timestamp"] = TS_B
    cell["properties"]["timestamp"] = TS_B

    manager.update_cell_histories([cell])

    entry = writes[0][1][0]
    assert entry["timestamp"] == TS_B
    assert "timestamp" not in entry["properties"]
