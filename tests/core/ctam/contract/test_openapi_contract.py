"""Phase 0 contract tests for the CTAM internal API OpenAPI document.

Phase 0's acceptance criterion is that every API example validates against the
checked-in schema. Because the supported keyword set has no ``$ref`` and no
``oneOf``, the envelope schema cannot describe the payload it wraps, so
validation is two-step: the whole example against the envelope, then its ``data``
member against the resource schema for that route.

The pairing is not inferred. Each response declares it with
``x-edgewarn-data-schema``, or ``x-edgewarn-item-schema`` for a collection, and
these tests fail if a JSON example carries neither and is not a known exception.
That way adding a route without saying what its payload is cannot quietly opt out
of validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.config.loader import _walk

pytestmark = pytest.mark.ctam

REPO_ROOT = Path(__file__).resolve().parents[4]
OPENAPI_PATH = REPO_ROOT / "docs" / "ctam" / "openapi" / "ctam-internal-v1.json"
SCHEMA_DIR = REPO_ROOT / "docs" / "ctam" / "schema"

# Every route in the plan's "Internal API v1" resource table, which is the
# minimum surface Phase 0 freezes. Kept literal so dropping a route from the
# document fails here rather than shrinking a glob.
PLAN_OPERATIONS = frozenset({
    ("get", "/health"),
    ("get", "/cycle"),
    ("get", "/files"),
    ("get", "/files/{file_id}"),
    ("get", "/files/{file_id}/content"),
    ("get", "/requirements"),
    ("post", "/requirements/check"),
    ("get", "/stormcells"),
    ("get", "/stormcells/{cell_id}"),
    ("patch", "/stormcells/{cell_id}"),
    ("get", "/cells/{cell_id}"),
    ("patch", "/cells/{cell_id}/entries/{timestamp}"),
    ("post", "/alerts"),
    ("put", "/routes/{routeId}"),
    ("get", "/transaction"),
    ("post", "/transaction/validate"),
    ("post", "/transaction/commit"),
    ("delete", "/transaction"),
})

# Payloads with no dedicated resource schema. Each is validated against the
# envelope only, and the reason is recorded so the list cannot grow by accident.
ENVELOPE_ONLY = {
    "getCtamHealth.responses.200": "liveness payload is two literal fields, not a resource",
    "listCtamStormcells.responses.200": "the working snapshot mirrors detection's cell shape, which Phase 0 freezes by snapshot test rather than by schema",
    "getCtamStormcell.responses.200": "same as listCtamStormcells",
    "getCtamCellHistory.responses.200": "history entries mirror the stored cell shape, frozen by the cell-history baseline tests",
    "registerCtamRoute.responses.200": "route staging returns only the accepted route id and byte count",
}

# Request bodies that are not patch requests.
REQUEST_ENVELOPE_ONLY = {
    "stageCtamAlert.requestBody": "alert payload shape is owned by EdgeWARN.alerts.schema, not by this API",
    "commitCtamTransaction.requestBody": "commit body is an api_version and an idempotency key",
    "registerCtamRoute.requestBody": "a public route may contain any finite JSON value",
}

HTTP_METHODS = ("get", "post", "patch", "delete", "put")


def load_openapi() -> dict:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def load_schema(name: str) -> dict:
    path = SCHEMA_DIR / name
    assert path.is_file(), f"declared schema {name} does not exist"
    return json.loads(path.read_text(encoding="utf-8"))


def check(schema: dict, value, label: str) -> None:
    errors: list = []
    _walk(schema, value, [], errors)
    assert not errors, f"{label}: " + "; ".join(
        f"{'.'.join(str(p) for p in path) or '<root>'} {message}" for path, message in errors
    )


def resolve(doc: dict, node):
    """Follow one internal ``$ref``. External refs are left alone."""
    seen = 0
    while isinstance(node, dict) and "$ref" in node and node["$ref"].startswith("#/"):
        seen += 1
        assert seen < 10, "ref cycle"
        cursor = doc
        for segment in node["$ref"][2:].split("/"):
            cursor = cursor[segment]
        node = cursor
    return node


def iter_json_examples(doc: dict):
    """Yield ``(label, example, media_object)`` for every application/json example.

    Walks operations rather than globbing for the word "example", so a response
    that forgot its example is visible as a missing label instead of silently
    contributing nothing.
    """
    for route, path_item in doc["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation["operationId"]

            body = operation.get("requestBody")
            if body is not None:
                media = resolve(doc, body).get("content", {}).get("application/json")
                if media is not None and "example" in media:
                    yield f"{operation_id}.requestBody", media["example"], media, route, method

            for status, response in operation.get("responses", {}).items():
                media = resolve(doc, response).get("content", {}).get("application/json")
                if media is None or "example" not in media:
                    continue
                yield f"{operation_id}.responses.{status}", media["example"], media, route, method


def test_document_declares_exactly_the_plan_surface():
    doc = load_openapi()
    found = {
        (method, route)
        for route, path_item in doc["paths"].items()
        for method in path_item
        if method in HTTP_METHODS
    }
    assert found == PLAN_OPERATIONS


def test_operation_ids_are_unique():
    doc = load_openapi()
    ids = [
        operation["operationId"]
        for path_item in doc["paths"].values()
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    ]
    assert len(ids) == len(set(ids))


def test_examples_were_actually_found():
    """Guards every parametrized test below against an empty collection."""
    labels = [label for label, *_ in iter_json_examples(load_openapi())]
    assert len(labels) == len(set(labels)), "duplicate example labels"
    assert len(labels) >= 17


def openapi_examples():
    return [
        pytest.param(label, example, media, id=label)
        for label, example, media, _route, _method in iter_json_examples(load_openapi())
    ]


@pytest.mark.parametrize("label,example,media", openapi_examples())
def test_response_example_validates_against_the_envelope(label, example, media):
    """Every response example is a well-formed envelope.

    Request bodies are excluded: they are not enveloped.
    """
    if label.endswith(".requestBody"):
        pytest.skip("request bodies are not enveloped")
    check(load_schema("response-envelope.schema.json"), example, label)


@pytest.mark.parametrize("label,example,media", openapi_examples())
def test_example_payload_validates_against_its_declared_schema(label, example, media):
    """The 'every API example validates against the checked-in schema' criterion.

    A payload with no declared schema must be listed in ``ENVELOPE_ONLY`` or
    ``REQUEST_ENVELOPE_ONLY`` with a reason, so a new route cannot silently skip
    validation.
    """
    if label.endswith(".requestBody"):
        declared = media.get("x-edgewarn-request-schema")
        if declared is None:
            assert label in REQUEST_ENVELOPE_ONLY, f"{label} declares no request schema and is not a recorded exception"
            return
        check(load_schema(declared), example, label)
        return

    declared = media.get("x-edgewarn-data-schema")
    item = media.get("x-edgewarn-item-schema")

    if declared is not None:
        check(load_schema(declared), example["data"], f"{label}.data")
        return

    if item is not None:
        cursor = example["data"]
        for segment in item["pointer"].lstrip("/").split("/"):
            cursor = cursor[segment]
        assert isinstance(cursor, list) and cursor, f"{label}: item pointer resolved to no items"
        schema = load_schema(item["schema"])
        for index, element in enumerate(cursor):
            check(schema, element, f"{label}.data{item['pointer']}[{index}]")
        return

    # No resource schema. Legitimate only for an error payload, whose shape the
    # envelope fully describes, or a recorded exception.
    if example.get("errors"):
        assert example["data"] is None, f"{label}: an error response should carry a null data member"
        return
    assert label in ENVELOPE_ONLY, f"{label} declares no data schema and is not a recorded exception"


def test_recorded_exceptions_are_all_still_reachable():
    """A stale exception is a silently unvalidated payload waiting to happen."""
    labels = {label for label, *_ in iter_json_examples(load_openapi())}
    stale = (set(ENVELOPE_ONLY) | set(REQUEST_ENVELOPE_ONLY)) - labels
    assert not stale, f"exceptions recorded for payloads that no longer exist: {sorted(stale)}"


def test_every_declared_schema_reference_exists():
    doc = load_openapi()
    for label, _example, media, _route, _method in iter_json_examples(doc):
        for key in ("x-edgewarn-data-schema", "x-edgewarn-request-schema"):
            if key in media:
                assert (SCHEMA_DIR / media[key]).is_file(), f"{label}: {media[key]} missing"
        if "x-edgewarn-item-schema" in media:
            assert (SCHEMA_DIR / media["x-edgewarn-item-schema"]["schema"]).is_file()


def test_external_schema_refs_resolve_from_the_document_location():
    """``$ref`` targets are relative to the OpenAPI file, so a move breaks them."""
    doc = load_openapi()
    refs = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str) and not value.startswith("#/"):
                    refs.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    assert refs, "expected the document to reference the schema files"
    for ref in refs:
        assert (OPENAPI_PATH.parent / ref).resolve().is_file(), f"unresolved external ref {ref}"


def test_every_error_code_in_the_envelope_enum_is_exercised():
    """An error code nobody has produced an example for is an untested branch."""
    enum = set(
        load_schema("response-envelope.schema.json")["properties"]["errors"]["items"]["properties"]["code"]["enum"]
    )
    used = {
        error["code"]
        for _label, example, _media, _route, _method in iter_json_examples(load_openapi())
        if isinstance(example, dict)
        for error in example.get("errors", [])
    }
    # Recorded rather than asserted-empty: these have no natural single-route
    # example, and Phase 2 adds them with the handlers that raise them.
    deferred = {"unsupported_version", "requirement_unmet", "conflict", "timed_out", "internal_error", "route_not_declared"}
    assert used <= enum, f"example uses a code absent from the enum: {sorted(used - enum)}"
    assert enum - used == deferred, f"error-code example coverage changed: missing {sorted(enum - used - deferred)}"


def test_patch_examples_only_target_the_two_containers():
    """The document must not model a write that the allowlist forbids.

    An example is documentation that module authors copy, so an example reaching
    outside ``modules``/``properties`` would teach the wrong thing even if the
    runtime rejected it.
    """
    doc = load_openapi()
    paths = [
        operation["path"]
        for label, example, media, _route, _method in iter_json_examples(doc)
        if label.endswith(".requestBody") and isinstance(example, dict)
        for operation in example.get("operations", [])
    ]
    assert paths, "expected patch examples to be present"
    for path in paths:
        assert path.startswith(("/modules/", "/properties/")), path
        assert path.split("/")[2], f"{path}: names a container with no key below it"


def test_parameter_patterns_are_the_schema_patterns_with_an_ecma_anchor():
    r"""The anchor differs on purpose, and only the anchor may differ.

    ``docs/ctam/schema/`` is validated by this repo's Python walker, so those
    patterns end in ``\Z``. This document is also read by OpenAPI tooling, whose
    regex dialect has no ``\Z``, so its inline patterns end in ``$``. That makes
    two spellings of the same constraint, which is exactly the drift
    ``test_repeated_patterns_are_identical_across_schemas`` guards inside the
    schema directory and cannot see across the boundary.

    Requiring every inline pattern to be a checked-in schema pattern with the
    anchor swapped keeps the pair in step, and fails if someone copies a pattern
    into a schema file without re-anchoring it.
    """
    schema_patterns = set()
    for path in SCHEMA_DIR.glob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))

        def collect(node):
            if isinstance(node, dict):
                if isinstance(node.get("pattern"), str):
                    schema_patterns.add(node["pattern"])
                for value in node.values():
                    collect(value)
            elif isinstance(node, list):
                for value in node:
                    collect(value)

        collect(document)

    # Not derived from a resource schema: these constrain the request line, not a
    # payload field, so they have no counterpart to stay in step with.
    REQUEST_ONLY = {"^bytes=[0-9]*-[0-9]*$"}

    parameters = load_openapi()["components"]["parameters"]
    checked = 0
    for name, parameter in parameters.items():
        pattern = parameter.get("schema", {}).get("pattern")
        if pattern is None:
            continue
        checked += 1
        assert pattern.endswith("$"), f"{name}: {pattern!r} should use the ECMA anchor here"
        assert "\\Z" not in pattern, f"{name}: {pattern!r} uses a Python-only anchor"
        if pattern in REQUEST_ONLY:
            continue
        equivalent = pattern[:-1] + "\\Z"
        assert equivalent in schema_patterns, f"{name}: {pattern!r} matches no checked-in schema pattern"
    assert checked >= 3, f"expected the pattern-bearing parameters, checked {checked}"


def test_patch_examples_use_only_supported_operations():
    doc = load_openapi()
    ops = {
        operation["op"]
        for label, example, _media, _route, _method in iter_json_examples(doc)
        if label.endswith(".requestBody") and isinstance(example, dict)
        for operation in example.get("operations", [])
    }
    assert ops
    assert ops <= {"add", "replace", "test"}, f"unsupported op in an example: {sorted(ops)}"
