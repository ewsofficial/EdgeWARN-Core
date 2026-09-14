import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from util.io import IOManager
import util.file as fs
from util.atomic import atomic_write_json
from EdgeWARN.api_integration.config import (
    inactive_cell_max_age_minutes,
    remove_old_cells_realtime,
)


class APIIndexManager:
    """Manages index files for the API to track available resources."""

    def __init__(self, io_manager: IOManager, remove_old_cells=None):
        self.io_manager = io_manager
        self.stormcell_index_path = fs.STORMCELL_DIR / "stormcell_index.json"
        self.cell_index_path = fs.CELL_DIR / "cell_index.json"
        # The realtime value is the default; historical callers pass their own.
        self.remove_old_cells = (
            remove_old_cells_realtime() if remove_old_cells is None else remove_old_cells
        )
        self.cell_timestamps = {}
        self.stormcell_timestamps = set()
        self._initial_scan_done = False
        self._stormcell_initial_scan_done = False
        
    def initialize_indexes(self):
        """
        Scan existing files and create initial index files.
        Called at server/pipeline startup.
        """
        self.io_manager.write_info("Initializing API indexes...")
        
        # Initialize stormcell index
        self._initialize_stormcell_index()
        
        # Initialize cell index
        self._initial_scan_cell_index()
        
        self.io_manager.write_info("API indexes initialized successfully")
    
    def _initialize_stormcell_index(self):
        """Scan STORMCELL_DIR and create/update stormcell_index.json"""
        if not fs.STORMCELL_DIR.exists():
            fs.STORMCELL_DIR.mkdir(parents=True, exist_ok=True)
        
        timestamps = []
        database_present = False
        try:
            from EdgeWARN.stormprob.database import StormProbRepository
            repository = StormProbRepository()
            database_present = repository.path.exists()
            if database_present:
                timestamps, _ = repository.index_projection()
        except FileNotFoundError:
            pass
        if not database_present:
            timestamps = [file.stem.removeprefix("stormcells_") for file in
                          sorted(fs.STORMCELL_DIR.glob("stormcells_*.json"))]

        self.stormcell_timestamps = set(timestamps)
        self._stormcell_initial_scan_done = True
        
        # Create index
        index_data = {
            "timestamps": sorted(timestamps),
            "lastUpdated": datetime.now(timezone.utc).isoformat()
        }
        
        # Write index
        atomic_write_json(self.stormcell_index_path, index_data, indent=2)
            
    def _initial_scan_cell_index(self):
        """Scan CELL_DIR once on startup to populate our internal state."""
        if not fs.CELL_DIR.exists():
            fs.CELL_DIR.mkdir(parents=True, exist_ok=True)
            
        current_time = datetime.now(timezone.utc).timestamp()
        
        self.cell_timestamps.clear()
        database_present = False
        try:
            from EdgeWARN.stormprob.database import StormProbRepository
            repository = StormProbRepository()
            database_present = repository.path.exists()
            if database_present:
                _, self.cell_timestamps = repository.index_projection()
        except FileNotFoundError:
            pass
        if not database_present:
            for file in fs.CELL_DIR.glob("*.json"):
                if file.stem == "cell_index":
                    continue
                try:
                    self.cell_timestamps[file.stem] = file.stat().st_mtime
                except Exception:
                    pass
                
        self._initial_scan_done = True
        self._write_cell_index()

    def _write_cell_index(self):
        """Write the current state to the index file."""
        # cellIds should be int if possible according to the old logic
        cell_ids = []
        for cid in self.cell_timestamps.keys():
            try:
                cell_ids.append(int(cid))
            except ValueError:
                self.io_manager.write_warning(f"Skipping non-numeric cell file: {cid}")
                
        index_data = {
            "cellIds": sorted(cell_ids),
            "lastUpdated": datetime.now(timezone.utc).isoformat()
        }
        
        atomic_write_json(self.cell_index_path, index_data, indent=2)
            
    def update_stormcell_index(self, timestamp: str):
        """
        Update stormcell_index.json incrementally, or resync if no timestamp.

        No periodic resync counter, and no config key for one. An interval only
        means something if the counter outlives a single update, and it cannot:
        the only path here is detection, and src/util/runtime/cycle.py starts
        edgewarn_cycle_worker in a fresh multiprocessing.Process per cycle, so
        any per-instance counter is zeroed before every update regardless of the
        scope the manager is hoisted into. Adding an interval requires first
        moving the index commit somewhere that survives a cycle -- the long-lived
        loop in src/run.py, or a counter persisted into stormcell_index.json.

        Args:
            timestamp: Timestamp of the latest stormcell output.
        """
        if not self._stormcell_initial_scan_done:
            self._initialize_stormcell_index()

        timestamp_str = str(timestamp) if timestamp is not None else ""
        stormcell_file = fs.STORMCELL_DIR / f"stormcells_{timestamp_str}.json"

        if timestamp and stormcell_file.exists():
            self.stormcell_timestamps.add(timestamp_str)
            index_data = {
                "timestamps": sorted(self.stormcell_timestamps),
                "lastUpdated": datetime.now(timezone.utc).isoformat()
            }
            atomic_write_json(self.stormcell_index_path, index_data, indent=2)
            return

        self._initialize_stormcell_index()

    def update_cell_index(self, cell_ids: list):
        """
        Update cell_index.json incrementally without a full directory scan!
        
        Args:
            cell_ids: List of active cell IDs just processed.
        """
        if not self._initial_scan_done:
            self._initial_scan_cell_index()
            
        current_time = datetime.now(timezone.utc).timestamp()
        
        for cid in cell_ids:
            self.cell_timestamps[str(cid)] = current_time
            
        self._write_cell_index()
    
    def cleanup_inactive_cells(self):
        """
        Expire cells past their age budget using our tracked state, then update index.
        Doesn't glob the directory.
        """
        if not self._initial_scan_done:
            self._initial_scan_cell_index()

        if self.remove_old_cells:
            current_time = datetime.now(timezone.utc).timestamp()
            cutoff_time = current_time - (inactive_cell_max_age_minutes() * 60)
            
            expired_cells = []
            for cell_id, timestamp in self.cell_timestamps.items():
                if timestamp < cutoff_time:
                    expired_cells.append(cell_id)
            
            for cell_id in expired_cells:
                # Remove from disk
                file_path = fs.CELL_DIR / f"{cell_id}.json"
                try:
                    if file_path.exists():
                        file_path.unlink()
                except Exception as e:
                    self.io_manager.write_error(f"Failed to delete old cell file {cell_id}.json: {e}")
                    
                # Remove from tracking
                del self.cell_timestamps[cell_id]
                
        # Update index to match reality
        self._write_cell_index()
