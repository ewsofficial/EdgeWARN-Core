from __future__ import annotations

import json
import os

import pytest

from EdgeWARN.ctam import publication
from EdgeWARN.ctam.publication import CTAMPublicationCoordinator


def test_publication_replaces_all_payloads_before_indexes(tmp_path):
    snapshot, history, journal = tmp_path / "stormcells.json", tmp_path / "7.json", tmp_path / "journals"
    snapshot.write_text('{"old":true}'); history.write_text('[]')
    seen = []
    path = CTAMPublicationCoordinator(journal).publish({snapshot: {"features": [1]}, history: [{"id": 7}]}, publish_indexes=lambda: seen.append((json.loads(snapshot.read_text()), json.loads(history.read_text()))), transaction_id="cycle")
    assert seen == [({"features": [1]}, [{"id": 7}])]
    assert json.loads(path.read_text())["state"] == "committed"


def test_publication_writes_only_prepared_and_committed_journals(tmp_path, monkeypatch):
    journal = tmp_path / "journals"
    writes = []
    write_json = publication.atomic_write_json

    def record_write(path, value, **kwargs):
        writes.append(json.loads(json.dumps(value)))
        return write_json(path, value, **kwargs)

    monkeypatch.setattr(publication, "atomic_write_json", record_write)
    CTAMPublicationCoordinator(journal).publish(
        {
            tmp_path / "first.json": {"v": 1},
            tmp_path / "second.json": {"v": 2},
            tmp_path / "third.json": {"v": 3},
        },
        transaction_id="two-writes",
    )

    assert [entry["state"] for entry in writes] == ["prepared", "committed"]
    assert [item["replaced"] for item in writes[0]["targets"]] == [False, False, False]
    assert [item["replaced"] for item in writes[1]["targets"]] == [True, True, True]


def test_publication_runs_alerts_after_payloads_and_before_indexes(tmp_path):
    target = tmp_path / "stormcells.json"; journal = tmp_path / "journals"; events = []
    CTAMPublicationCoordinator(journal).publish(
        {target: {"ready": True}},
        publish_alerts=lambda: events.append(("alerts", json.loads(target.read_text()))),
        publish_indexes=lambda: events.append(("indexes", json.loads(target.read_text()))),
    )
    assert events == [("alerts", {"ready": True}), ("indexes", {"ready": True})]


def test_recovery_rolls_forward_after_fault_between_replacements(tmp_path):
    first, second, journal = tmp_path / "first.json", tmp_path / "second.json", tmp_path / "journals"
    calls = 0
    def fail_second(source, destination):
        nonlocal calls; calls += 1
        if calls == 2: raise OSError("injected process death")
        os.replace(source, destination)
    coordinator = CTAMPublicationCoordinator(journal, replace=fail_second)
    with pytest.raises(OSError): coordinator.publish({first: {"v": 1}, second: {"v": 2}}, transaction_id="fault")
    assert json.loads(first.read_text()) == {"v": 1}
    prepared = json.loads((journal / "fault.json").read_text())
    assert prepared["state"] == "prepared"
    assert [item["replaced"] for item in prepared["targets"]] == [False, False]
    CTAMPublicationCoordinator(journal).recover()
    assert json.loads(second.read_text()) == {"v": 2}
    assert json.loads((journal / "fault.json").read_text())["state"] == "committed"


def test_index_failure_leaves_prepared_journal_recoverable(tmp_path):
    target, journal = tmp_path / "target.json", tmp_path / "journals"

    def fail_indexes():
        on_disk = json.loads((journal / "index-fault.json").read_text())
        assert json.loads(target.read_text()) == {"v": 1}
        assert on_disk["state"] == "prepared"
        assert on_disk["targets"][0]["replaced"] is False
        raise RuntimeError("index publication failed")

    with pytest.raises(RuntimeError, match="index publication failed"):
        CTAMPublicationCoordinator(journal).publish(
            {target: {"v": 1}},
            publish_indexes=fail_indexes,
            transaction_id="index-fault",
        )

    recovered = CTAMPublicationCoordinator(journal).recover()
    assert recovered == [journal / "index-fault.json"]
    committed = json.loads((journal / "index-fault.json").read_text())
    assert committed["state"] == "committed"


def test_recovery_quarantines_journal_when_remaining_part_is_corrupt(tmp_path):
    target, journal = tmp_path / "target.json", tmp_path / "journals"
    coordinator = CTAMPublicationCoordinator(journal, replace=lambda _source, _target: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError): coordinator.publish({target: {"v": 1}}, transaction_id="bad")
    part = next(tmp_path.glob(".target.json.bad.ctam-part")); part.write_text("not json")
    assert CTAMPublicationCoordinator(journal).recover() == []
    assert (journal / "quarantine" / "bad.json").exists()
