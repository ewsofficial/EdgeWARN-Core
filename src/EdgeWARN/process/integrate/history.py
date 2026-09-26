import json
from pathlib import Path
from util.io import IOManager
import util.file as fs
from util.atomic import atomic_write_json

class CellHistoryManager:
    def __init__(self, io_manager: IOManager):
        self.io_manager = io_manager
        # Ensure the cell history directory exists
        if not fs.CELL_DIR.exists():
            fs.CELL_DIR.mkdir(parents=True, exist_ok=True)
            self.io_manager.write_info(f"Created cell history directory at {fs.CELL_DIR}")

    def update_cell_histories(self, cells: list, timestamp: str = None):
        """
        Update the persistent history file for each cell in the list.
        Appends the current cell state safely to CELL_DIR/{cell_id}.json.
        """
        if not cells:
            return

        updates = self.prepare_cell_history_updates(cells)
        success_count = 0
        for file_path, history in updates.items():
            try:
                atomic_write_json(file_path, history, default=str)
                success_count += 1
            except Exception as e:
                self.io_manager.write_error(f"Failed to write history for {file_path.stem}: {e}")
        self.io_manager.write_info(f"Updated history for {success_count}/{len(updates)} active cells")

    def prepare_cell_history_updates(self, cells: list) -> dict[Path, list]:
        """Build, but do not publish, active-cell history replacements.

        The publication coordinator calls this so the stormcell snapshot and all
        touched histories share one journal.  It deliberately retains legacy
        semantics: inactive cells stay untouched and a duplicate *last*
        timestamp refreshes rather than appends.
        """
        updates = {}
        active_ids = [cell["id"] for cell in cells if cell.get("id") and "timestamp" in cell]
        try:
            from EdgeWARN.stormprob.database import StormProbRepository
            database_histories = StormProbRepository().legacy_histories(active_ids)
        except FileNotFoundError:
            database_histories = {}
        for cell in cells:
            cell_id = cell.get("id")
            if not cell_id:
                continue

            # Ensure we have a timestamp
            # Check if this cell is active (has a timestamp assigned by detection)
            if "timestamp" not in cell:
                # Unmatched (inactive) cells do not get a timestamp update in detect/track.py
                # Use this to skip history update entirely, preserving file mtime.
                continue

            # Prioritize cell timestamp (which is now guaranteed to be current if present)
            ts = cell.get("timestamp") or cell.get("properties", {}).get("timestamp")
            
            if not ts:
                self.io_manager.write_warning(f"Cell {cell_id} missing timestamp value, skipping history update.")
                continue

            # Move timestamp to top level (sanity check, usually redundant due to main.py logic now)
            if cell.get("timestamp") != ts:
                 cell["timestamp"] = ts

            # Remove from properties if present (legacy cleanup)
            if "properties" in cell and "timestamp" in cell["properties"]:
                cell["properties"].pop("timestamp")

            file_path = fs.CELL_DIR / f"{cell_id}.json"
            unreadable_history = False
            history = []
            
            # Load existing history
            history = list(database_histories.get(str(cell_id), []))
            if not history and file_path.exists():
                try:
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            history = data
                        else:
                            self.io_manager.write_warning(f"History file {file_path} invalid format; preserving it.")
                            unreadable_history = True
                            history = []
                except Exception as e:
                    self.io_manager.write_error(f"Failed to read history for {cell_id}: {e}; preserving it")
                    unreadable_history = True
                    history = []

            if unreadable_history:
                continue

            # If the latest history entry already has this timestamp, replace it
            # with the current cell snapshot so reprocessing can refresh fields
            # like dx/dy/dt instead of silently keeping stale data.
            replace_last_entry = False
            if history:
                last_entry = history[-1]
                # Check top-level first, then properties (legacy support)
                last_ts = last_entry.get("timestamp") or last_entry.get("properties", {}).get("timestamp")
                
                # Compare timestamps
                if last_ts == ts:
                    replace_last_entry = True
            
            if replace_last_entry:
                history[-1] = cell
            else:
                # Append current full cell state
                history.append(cell)

            # Write back
            updates[file_path] = history

        return updates
