"""Behavioral matrix for ProbSevere identifier and field integration."""

from unittest.mock import MagicMock

import pytest

from EdgeWARN.process.integrate.core import integrator as integrator_module
from EdgeWARN.process.integrate.integrate import StormCellIntegrator


def _feature(*, feature_id=None, property_id=None, value="42"):
    feature = {"properties": {"VALUE": value}}
    if feature_id is not None:
        feature["id"] = feature_id
    if property_id is not None:
        feature["properties"]["ID"] = property_id
    return feature


@pytest.mark.parametrize(
    ("feature_id", "property_id", "cell_id"),
    [("17", 99, 17), (17, None, "17"), (None, "17", 17)],
)
def test_probsevere_identifier_forms_match(monkeypatch, feature_id, property_id, cell_id):
    monkeypatch.setattr(integrator_module, "probsevere_field_map", lambda: {"target": "VALUE"})
    cell = {"id": cell_id, "properties": {"preserved": "yes"}}

    result = StormCellIntegrator(MagicMock()).integrate_probsevere(
        {"features": [_feature(feature_id=feature_id, property_id=property_id)]}, [cell]
    )

    assert result[0]["properties"] == {"preserved": "yes", "target": 42.0}


def test_feature_id_takes_precedence_over_properties_id(monkeypatch):
    monkeypatch.setattr(integrator_module, "probsevere_field_map", lambda: {"target": "VALUE"})
    cells = [{"id": "outer", "properties": {}}, {"id": "inner", "properties": {}}]
    result = StormCellIntegrator(MagicMock()).integrate_probsevere(
        {"features": [_feature(feature_id="outer", property_id="inner")]}, cells
    )

    assert result[0]["properties"]["target"] == 42.0
    assert result[1]["properties"] == {}


@pytest.mark.parametrize("value", [None, "not-numeric", ""])
def test_missing_or_malformed_values_are_match_errors(monkeypatch, value):
    monkeypatch.setattr(integrator_module, "probsevere_field_map", lambda: {"alternate": "ALT"})
    feature = _feature(feature_id=17)
    if value is not None:
        feature["properties"]["ALT"] = value

    result = StormCellIntegrator(MagicMock()).integrate_probsevere(
        {"features": [feature]}, [{"id": 17, "properties": {"keep": 1}}]
    )

    assert result[0]["properties"] == {"keep": 1, "alternate": "MATCH_ERROR"}


@pytest.mark.parametrize("cell_id", [None, "unmatched"])
def test_missing_and_unmatched_identifiers_do_not_mutate_cells(monkeypatch, cell_id):
    monkeypatch.setattr(integrator_module, "probsevere_field_map", lambda: {"target": "VALUE"})
    cell = {"id": cell_id, "properties": {"keep": 1}}

    StormCellIntegrator(MagicMock()).integrate_probsevere(
        {"features": [_feature()]}, [cell]
    )

    assert cell["properties"] == {"keep": 1}
