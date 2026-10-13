# Configurable MRMS ingestion: phase 7

Implemented downstream eligibility and producer agreement for ingest v2.
The shipped catalog uses schema v2; Core and independent ingest compare their
frozen registry/dependency agreement before consuming inputs.

Core manifest-aware MRMS reads select validated current records by family and
product. Detection also selects explicit previous roles, without promoting a
previous-only record to current data. No manifest-aware MRMS lookup falls back
to arbitrary latest files. Direct detection verifies reserved membership before
scanning. AzShear support checks eligibility before resolving either input.

## Verification

Multiprocessing and HTTP tests required execution with local socket access;
the initial sandboxed runs could not start their local servers/process managers.
