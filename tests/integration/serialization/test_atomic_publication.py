"""Atomic publication regression tests for shared source/state writers.

Phase 0 prerequisite (plans/realtime-runner-decomposition-plan.md): every
writer whose final filename is observable by an independent process must
commit through a sibling temporary file and ``os.replace``. A failed write
must never leave truncated content visible under the real name.
"""

import json

import pytest


class TestNexradManifestWriter:
    def test_identical_content_is_skipped(self, tmp_path, monkeypatch):
        from common.ingest.nexrad.writer import _write_text_if_changed

        path = tmp_path / "manifest.json"
        path.write_text("same", encoding="utf-8")

        def explode(*args, **kwargs):
            raise AssertionError("atomic writer must not run for identical content")

        monkeypatch.setattr(
            "common.ingest.nexrad.writer.atomic_write_text", explode
        )
        assert _write_text_if_changed(path, "same") == path
        assert path.read_text(encoding="utf-8") == "same"

    def test_changed_content_replaces_atomically_with_no_temp_leftovers(self, tmp_path):
        from common.ingest.nexrad.writer import _write_text_if_changed

        path = tmp_path / "manifest.json"
        path.write_text("old", encoding="utf-8")
        _write_text_if_changed(path, "new")
        assert path.read_text(encoding="utf-8") == "new"
        assert list(tmp_path.iterdir()) == [path]

    def test_failed_publication_leaves_previous_manifest_intact(self, tmp_path, monkeypatch):
        from common.ingest.nexrad import writer as nexrad_writer

        path = tmp_path / "manifest.json"
        path.write_text("previous", encoding="utf-8")

        def fail(destination, payload):
            raise OSError("simulated crash mid publication")

        monkeypatch.setattr(nexrad_writer, "atomic_write_text", fail)
        with pytest.raises(OSError):
            nexrad_writer._write_text_if_changed(path, "replacement")
        # The old manifest is still whole; no partial file under the real name.
        assert path.read_text(encoding="utf-8") == "previous"
        assert list(tmp_path.iterdir()) == [path]


class TestNexradScanStateWriter:
    def test_state_file_publishes_atomically_and_survives_failure(self, tmp_path, monkeypatch):
        from common.ingest.nexrad import service as nexrad_service

        path = tmp_path / "scan_state.json"
        path.write_text('{"kept": true}', encoding="utf-8")

        def fail(destination, payload):
            raise OSError("simulated crash mid write")

        monkeypatch.setattr(nexrad_service, "atomic_write_text", fail)
        with pytest.raises(OSError):
            nexrad_service._write_text_if_changed(path, '{"new": true}')
        assert json.loads(path.read_text(encoding="utf-8")) == {"kept": True}
        assert list(tmp_path.iterdir()) == [path]


class TestOverlayManifestWriter:
    def test_save_to_json_writes_parseable_layers(self, tmp_path):
        from EWMRS.render.tools import OverlayManifestUtils

        utils = OverlayManifestUtils()
        utils.layers = [{"name": "MRMS_EchoTop18", "opacity": 1}]
        target = tmp_path / "overlay_manifest.json"
        utils.save_to_json(str(target))
        assert json.loads(target.read_text(encoding="utf-8")) == utils.layers
        assert list(tmp_path.iterdir()) == [target]

    def test_serialization_failure_never_touches_destination(self, tmp_path):
        from EWMRS.render.tools import OverlayManifestUtils

        utils = OverlayManifestUtils()
        utils.layers = [{"bad": object()}]  # not JSON-serializable
        target = tmp_path / "overlay_manifest.json"
        target.write_text("[]", encoding="utf-8")
        with pytest.raises(TypeError):
            utils.save_to_json(str(target))
        assert target.read_text(encoding="utf-8") == "[]"
        assert list(tmp_path.iterdir()) == [target]
