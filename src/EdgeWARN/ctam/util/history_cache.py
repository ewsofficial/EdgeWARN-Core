import json
from typing import Dict, List, Any
import util.file as fs

class CellHistoryCache:
    """In-memory cache for storm cell histories to prevent redundant disk I/O."""
    
    def __init__(self):
        self._cache: Dict[str, List[Dict[str, Any]]] = {}

    def get(self, cell_id: str, limit: int = None) -> List[Dict[str, Any]]:
        """
        Retrieve cell history from cache, or load from disk if not present.
        Returns the full history list, optionally sliced to `limit` most recent entries.
        """
        if cell_id not in self._cache:
            history_file = fs.CELL_DIR / f"{cell_id}.json"
            history = []
            try:
                from EdgeWARN.stormprob.database import StormProbRepository
                history = list(reversed(StormProbRepository().legacy_history(cell_id)))
            except FileNotFoundError:
                pass
            
            if not history and history_file.exists():
                try:
                    with open(history_file, 'r') as f:
                        history = json.load(f)
                        
                    if isinstance(history, list):
                        # Ensure sorted by timestamp descending
                        # Assumes newest was appended at the end initially
                        history.reverse()
                    else:
                        history = []
                except Exception:
                    history = []
                    
            self._cache[cell_id] = history
            
        full_history = self._cache[cell_id]
        if limit is not None:
            return full_history[:limit]
        return full_history
        
    def clear(self):
        """Clear the cache."""
        self._cache.clear()
        
    def preload_active(self, active_cells: List[str]):
        """Optional: pre-load a set of active cells."""
        for cell_id in active_cells:
            self.get(cell_id)
