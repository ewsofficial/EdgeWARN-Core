import json
from pathlib import Path
import util.file as fs

def get_cell_history(cell_id, limit=5):
    """
    Retrieve the history of a storm cell from the persistent JSON storage.
    
    Args:
        cell_id (str): The unique ID of the cell (e.g., "Cell_101").
        limit (int): Number of past entries to retrieve (most recent first).
        
    Returns:
        list: List of dictionaries representing past states, sorted by time descending.
              Returns [] if no history found.
    """
    try:
        from EdgeWARN.stormprob.database import StormProbRepository
        history = StormProbRepository().legacy_history(cell_id, limit=limit)
        if history:
            return list(reversed(history))
    except FileNotFoundError:
        pass

    history_file = fs.CELL_DIR / f"{cell_id}.json"
    
    if not history_file.exists():
        return []
        
    try:
        with open(history_file, 'r') as f:
            history = json.load(f)
            
        if not isinstance(history, list):
            return []
            
        # Ensure sorted by timestamp descending just in case
        # (Assuming newest is appended at end usually, so we reverse)
        history.reverse()
        
        return history[:limit]
        
    except Exception:
        return []
