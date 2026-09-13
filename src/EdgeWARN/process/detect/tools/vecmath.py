import json
from datetime import datetime
from math import radians, cos
import util.file as fs


class StormVectorCalculator:
    """
    Calculates storm motion vectors (dx, dy, dt) for storm cells
    by comparing current state with the last entry in persistent history.
    """

    @staticmethod
    def calculate_vectors(entries, previous_entries=None):
        """
        Updates each cell with dx, dy, dt by comparing against 
        the last recorded history entry in CELL_DIR.
        
        entries: list of current cell dictionaries
        """
        previous_by_id = StormVectorCalculator._build_previous_entry_map(previous_entries)

        for cell in entries:
            # Skip if cell has no timestamp (unmatched/inactive)
            if "timestamp" not in cell:
                continue

            cell_id = cell.get('id')
            if not cell_id:
                continue

            prev_entry = previous_by_id.get(int(cell_id))
            if prev_entry is None:
                prev_entry = StormVectorCalculator._load_previous_entry_from_history(
                    cell_id, before=cell.get("timestamp"))

            if prev_entry is None:
                continue

            # Extract timestamps
            # Support both top-level (new) and properties (old)
            t1_str = prev_entry.get('timestamp') or prev_entry.get('properties', {}).get('timestamp')
            t2_str = cell.get('timestamp')

            if not t1_str or not t2_str:
                continue
            
            # If timestamps are identical, we haven't moved in time (re-processing?) -> skip
            if t1_str == t2_str:
                continue

            try:
                t1 = datetime.fromisoformat(t1_str)
                t2 = datetime.fromisoformat(t2_str)
                dt = (t2 - t1).total_seconds()
            except ValueError:
                continue

            if dt <= 0:
                continue

            # Extract centroid coordinates
            # prev_entry should have centroid
            c1 = prev_entry.get('centroid')
            c2 = cell.get('centroid')

            if not c1 or not c2:
                continue

            lat1, lon1 = c1
            lat2, lon2 = c2

            # Convert longitudinal difference to km accounting for latitude
            # Approximation: 1 degree lat ~ 111 km, 1 degree lon ~ 111*cos(lat) km
            dx = (lon2 - lon1) * 111 * cos(radians((lat1 + lat2) / 2)) * 1000  # East-West (meters)
            dy = (lat2 - lat1) * 111 * 1000 # North-South (meters)

            # Append motion vectors to CURRENT cell
            cell['dx'] = dx
            cell['dy'] = dy
            cell['dt'] = dt

        return entries

    @staticmethod
    def _build_previous_entry_map(previous_entries):
        if not previous_entries:
            return {}

        previous_by_id = {}
        for entry in previous_entries:
            cell_id = entry.get('id')
            if cell_id is None:
                continue

            try:
                previous_by_id[int(cell_id)] = entry
            except (TypeError, ValueError):
                continue

        return previous_by_id

    @staticmethod
    def _load_previous_entry_from_history(cell_id, before=None):
        try:
            from EdgeWARN.stormprob.database import StormProbRepository
            history = StormProbRepository().legacy_history(cell_id, limit=1, before=before)
            if history:
                return history[-1]
        except FileNotFoundError:
            pass
        history_path = fs.CELL_DIR / f"{cell_id}.json"
        if not history_path.exists():
            return None

        try:
            with open(history_path, 'r') as f:
                history = json.load(f)
        except Exception:
            return None

        if not history:
            return None

        return history[-1]
