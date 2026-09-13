"""StormProb input collection (Phase 1).

Versioned, failure-isolated helpers that collect exact model inputs at the
right pipeline stages. The input database (Phase 2) and ONNX inference
(Phase 3) consume these records; no inference runs here.
"""

from . import features, geometry, records, tracks

__all__ = ["features", "geometry", "records", "tracks"]
