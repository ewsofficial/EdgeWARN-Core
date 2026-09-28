"""Compatibility alias for the shared acquisition implementation."""
import sys as _sys
import common.ingest.mrms.acquisition as _impl

_sys.modules[__name__] = _impl
