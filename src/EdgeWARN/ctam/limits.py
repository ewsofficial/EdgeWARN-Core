"""Python half of the CTAM internal API limits table.

Every constant here restates one row of the Values table in
``docs/ctam/internal-api-limits.md``. **That table is the decision record**: it
carries the grounding citation for each number and the "On excess" behavior the
host must implement. This module is a convenience for Python callers, not a
second source of truth -- if a number here and a number there disagree, the
document wins and this module is the bug.

``tests/core/ctam/contract/test_limits_constants.py`` parses that table and
asserts each constant below equals its documented row, so the two copies cannot
drift silently. Add a constant only when the document already has the row.

The identifier constants (`API_VERSION`, the schema versions, and the two
reserved sets) are not limits with table rows; they come from the frozen Phase 0
schemas under ``docs/ctam/schema/`` and from the reserved-name rules the plan
states. They live here because manifest validation, discovery, readiness, and
the runner all need them and none of those should import each other for them.
"""

from __future__ import annotations

# --- Protocol and schema identity -------------------------------------------
# `api_version` is `{"const": "1"}` in docs/ctam/schema/status-record.schema.json
# and the internal API is mounted at /internal/ctam/v1. A module declaring an
# unsupported version is `invalid` at discovery, never launched.
API_VERSION = "1"
SUPPORTED_API_VERSIONS = ("1",)

# `schema_version = 1` in the plan's manifest example. Kept as a tuple so a
# future v2 host can accept both without a version-comparison rule appearing in
# three places.
MANIFEST_SCHEMA_VERSION = 1
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = (1,)

# `schema_version` is `{"const": 1}` in docs/ctam/schema/status-record.schema.json.
STATUS_SCHEMA_VERSION = 1

# --- Discovery-enforced limits ----------------------------------------------
# Row "Maximum external module count". StormCast is not counted; modules past
# the 8th in stable dependency-then-ID order are recorded `invalid`.
MAX_EXTERNAL_MODULES = 8

# Rows "Minimum manifest `timeout_seconds`", "Maximum manifest
# `timeout_seconds`", "Default `timeout_seconds` when omitted".
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 10

# Row "Maximum module ID length". This is the quantifier boundary of the
# `^[a-z0-9][a-z0-9_-]{0,127}\Z` module_id pattern frozen in three schemas: the
# leading character class matches separately, so 127 + 1 == 128.
MAX_MODULE_ID_LENGTH = 128

# --- Runtime-enforced limits, declared here so discovery can pre-reject ------
# Rows "Default history read window" and "Maximum history read window". The
# maximum is a read-side clamp at runtime, but a manifest asking for more
# entries than any read can return is a manifest bug, so discovery rejects it.
DEFAULT_HISTORY_WINDOW = 5
MAX_HISTORY_WINDOW = 120

# Row "Maximum streamed file size".
MAX_STREAMED_FILE_BYTES = 268435456

# Runtime request and stream bounds.  See the identically named rows in
# docs/ctam/internal-api-limits.md.
MAX_REQUEST_BODY_BYTES = 1048576
STREAM_CHUNK_BYTES = 1048576

# --- Reserved names ---------------------------------------------------------
# `stormprob` is the built-in adapter's id. An external installation must not be
# able to shadow it, because a shadowing module would silently replace the
# forecast producer the alerts stage consumes.
RESERVED_MODULE_IDS = frozenset({"stormprob", "stormcast"})

# Output keys no external module may claim, matched case-insensitively for
# display names. `StormProb` is the built-in's `modules` key; `_grid_outputs` is
# the legacy grid-module container written by the host.
RESERVED_OUTPUT_KEYS = frozenset({"StormProb", "StormCast", "_grid_outputs"})
