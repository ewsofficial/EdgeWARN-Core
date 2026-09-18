# CTAM protocol compatibility

The internal API uses a quoted, digits-only `api_version` string (`"1"`) in
module manifests. The SDK sends the same value in the optional
`X-CTAM-API-Version` request header; an omitted header currently defaults to
the supported version. The protocol is v1, mounted at `/internal/ctam/v1`; it
has no earlier supported version. The separate `schema_version` field is the
integer `1`.

When v2 is introduced, the host should support v2 and v1 for a documented
compatibility window, preserve v1's OpenAPI/schema artifacts, and add contract
tests for both versions. A module declaring an unsupported version is marked
invalid at discovery and excluded from the runnable set. After successful
Bearer authentication, a request carrying an unsupported version receives
HTTP 426. The current OpenAPI artifact does not yet document that header or
426 response.
