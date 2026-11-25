"""The per-file parse memo must never change a readiness verdict."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import util.file as fs
from EdgeWARN.ctam import readiness as R
from util import fastjson

CYCLE_ID = "20260805-120000"
CYCLE_TIME = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_cache():
    R._facts_cache.clear()
    yield
    R._facts_cache.clear()


@pytest.fixture
def cell_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "cells"
    directory.mkdir()
    monkeypatch.setattr(fs, "CELL_DIR", str(directory))
    return directory


@pytest.fixture
def parses(monkeypatch):
    calls = []
    real = fastjson.loads

    def counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(R.fastjson, "loads", counting)
    return calls


def _reference_payload(path: Path):
    """The uncached parse, kept verbatim."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), None
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is not parseable JSON: line {exc.lineno} {exc.msg}"
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc.strerror or exc}"
    except UnicodeDecodeError:
        return None, f"{path.name} is not valid UTF-8 JSON"


def _catalog(cells, stormcell_path):
    return R.build_catalog(cells=cells, timestamp=CYCLE_ID, stormcell_path=stormcell_path,
                           input_manifest=None, now=CYCLE_TIME)


def _write_cycle(tmp_path, cell_dir):
    snapshot = tmp_path / "stormcells.json"
    snapshot.write_text(json.dumps({"features": [], "latest_timestamp": CYCLE_ID}), encoding="utf-8")
    (cell_dir / "1.json").write_text(json.dumps([{"t": 1}, {"t": 2}]), encoding="utf-8")
    (cell_dir / "2.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    (cell_dir / "3.json").write_text("[1, 2,", encoding="utf-8")
    (cell_dir / "4.json").write_bytes(b"[\xff]")
    return snapshot, [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]


def test_second_catalog_is_identical_and_parses_nothing(tmp_path, cell_dir, parses):
    snapshot, cells = _write_cycle(tmp_path, cell_dir)
    first = _catalog(cells, snapshot)
    parsed = len(parses)
    second = _catalog(cells, snapshot)
    assert second == first
    # Successful parses (snapshot, 1.json, 2.json) are reused; failures re-run.
    assert len(parses) - parsed == 1
    assert R._history_entry_count(cell_dir / "1.json") == 2
    assert len(parses) - parsed == 1


@pytest.mark.parametrize("name, content", [
    ("list.json", "[1, 2, 3]"), ("dict.json", '{"features": [], "latest_timestamp": "x"}'),
    ("nofeat.json", '{"latest_timestamp": 5}'), ("str.json", '"text"'), ("bad.json", "[1,"),
    ("nan.json", "[NaN]"), ("utf.json", b"\xff\xfe"), ("empty.json", ""),
    ("wide.json", "[123456789012345678901234567890]"),
])
def test_facts_match_the_uncached_parse(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    payload, reason = _reference_payload(path)
    for _ in range(2):
        facts, facts_reason = R._json_facts(path)
        assert facts_reason == reason
        if reason is None:
            assert facts == R._facts_of(payload)
            assert facts.is_list == isinstance(payload, list)
            assert facts.type_name == type(payload).__name__


def test_missing_file_reason_matches(tmp_path):
    assert R._json_facts(tmp_path / "gone.json") == _reference_payload(tmp_path / "gone.json")


def test_atomic_replace_is_reparsed(tmp_path, cell_dir):
    path = cell_dir / "1.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert R._history_entry_count(path) == 2
    staged = cell_dir / ".1.json.tmp"
    staged.write_text("[1, 2, 3]", encoding="utf-8")
    os.replace(staged, path)
    assert R._history_entry_count(path) == 3


def test_same_size_in_place_rewrite_with_restored_mtime_is_reparsed(tmp_path):
    path = tmp_path / "h.json"
    path.write_text("[1, 2]", encoding="utf-8")
    stat = path.stat()
    assert R._json_facts(path)[0].list_length == 2
    path.write_text('"abcd"', encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    # ctime cannot be restored, so the rewrite is still detected.
    assert R._json_facts(path)[0].type_name == "str"


def test_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_FACTS_CACHE_SIZE", 3)
    for index in range(10):
        path = tmp_path / f"{index}.json"
        path.write_text("[]", encoding="utf-8")
        R._json_facts(path)
    assert len(R._facts_cache) == 3
