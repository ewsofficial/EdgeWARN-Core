"""Public EdgeWARN API with lazy pipeline imports.

Importing the package should not load rasterio, netCDF4, or the rest of the
weather-processing stack.  Entry points retain the same public names, resolved
only when a caller actually asks for them.
"""

from importlib import import_module

__all__ = [
    "historical_pipeline",
    "initialize_runtime",
    "parse_utc_time",
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".pipeline", __name__), name)
    globals()[name] = value
    return value
