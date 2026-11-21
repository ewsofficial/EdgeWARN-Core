import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from util.io import IOManager
import util.file as fs
from util.atomic import atomic_write_json
from EdgeWARN.api_integration.config import (
    inactive_cell_max_age_minutes,
    remove_old_cells_realtime,
    stormprob_field_max_age_minutes,
)


class APIIndexManager:
    """Manages index files for the API to track available resources."""

    def __init__(self, io_manager: IOManager, remove_old_cells=None, *, include_pending=False):
        self.io_manager = io_manager
        self.stormcell_index_path = fs.STORMCELL_DIR / "stormcell_index.json"
        self.cell_index_path = fs.CELL_DIR / "cell_index.json"
        self.field_index_path = fs.STORMPROB_FIELDS_DIR / "field_index.json"
        # The realtime value is the default; historical callers pass their own.
        self.remove_old_cells = (
            remove_old_cells_realtime() if remove_old_cells is None else remove_old_cells
        )
        self.cell_timestamps = {}
        self.stormcell_timestamps = set()
        self._initial_scan_done = False
        self._stormcell_initial_scan_done = False
        self._database_projection_loaded = False
        self._database_projection = None
        self.projection_query_seconds = 0.0
        self.projection_reused = False
        self.include_pending = include_pending
        self.cleanup_seconds = 0.0
        self.index_write_seconds = 0.0
        self.field_retention_seconds = 0.0

    @staticmethod
    def _stormcell_file_timestamps() -> set:
        """Timestamps of the stormcell snapshot files currently on disk."""
        return {
            path.stem.removeprefix("stormcells_")
            for path in fs.STORMCELL_DIR.glob("stormcells_*.json")
        }

    def _load_database_projection(self):
        """Return one immutable database snapshot for this index update."""
        if self._database_projection_loaded:
            self.projection_reused = self._database_projection is not None
            return self._database_projection

        self._database_projection_loaded = True
        try:
            from EdgeWARN.stormprob.database import StormProbRepository

            repository = StormProbRepository()
            if not repository.path.exists():
                return None
            started = time.perf_counter()
            if self.include_pending:
                self._database_projection = repository.index_projection(include_pending=True)
            else:
                self._database_projection = repository.index_projection()
            self.projection_query_seconds = time.perf_counter() - started
            return self._database_projection
        except FileNotFoundError:
            return None
        
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
        projection = self._load_database_projection()
        timestamps = self._stormcell_file_timestamps()
        if projection is not None:
            timestamps |= set(projection[0])

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
        projection = self._load_database_projection()
        if projection is not None:
            _, projected_cell_timestamps = projection
            self.cell_timestamps.update(projected_cell_timestamps)
        else:
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
                    
                # A failed unlink must remain listed while its file exists.
                if not file_path.exists():
                    del self.cell_timestamps[cell_id]
                
        # Update index to match reality
        self._write_cell_index()

    def _publish_field_index(self) -> None:
        """Expire old swept-probability fields and rewrite ``field_index.json``.

        Entries are canonical ``YYYYMMDD-HHMMSS`` cycle keys -- the same keys as
        ``stormcell_index.json`` and the artifact file names -- so an index entry
        resolves every artifact of a scan. The artifacts themselves are the
        retention source of truth: each cycle writes or replaces its own file,
        and a scan's field is independent of every other scan. Header-only
        quiet-scan artifacts age out the same way.
        """
        from EdgeWARN.stormprob import field

        directory = fs.STORMPROB_FIELDS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        cutoff = datetime.now(timezone.utc).timestamp() - stormprob_field_max_age_minutes() * 60
        timestamps = []
        for path in sorted(directory.glob("stormprob_field_*.bin.gz")):
            try:
                if self.remove_old_cells and path.stat().st_mtime < cutoff:
                    path.unlink()
                    continue
                timestamps.append(field.cycle_key(field.read_analysis_time(path)))
            except (OSError, ValueError) as exc:
                self.io_manager.write_warning(
                    f"Skipping unreadable StormProb field {path.name}: {exc}"
                )
        atomic_write_json(self.field_index_path, {
            "timestamps": sorted(set(timestamps)),
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        }, indent=2)
        self.field_retention_seconds = time.perf_counter() - started

    def publish_cycle_indexes(self, timestamp: str, cell_ids: list) -> None:
        """Build both final listings, clean expired files, then write each once."""
        fs.STORMCELL_DIR.mkdir(parents=True, exist_ok=True)
        fs.CELL_DIR.mkdir(parents=True, exist_ok=True)
        projection = self._load_database_projection()
        if projection is None:
            self.stormcell_timestamps = self._stormcell_file_timestamps()
            self.cell_timestamps = {
                path.stem: path.stat().st_mtime
                for path in fs.CELL_DIR.glob("*.json") if path.stem != "cell_index"
            }
        else:
            timestamps, cell_timestamps = projection
            # StormProb prunes cycle rows after its own, shorter budget; the
            # snapshot files on disk are the retention source of truth.
            self.stormcell_timestamps = self._stormcell_file_timestamps() | set(timestamps)
            self.cell_timestamps = dict(cell_timestamps)
            # SQLite pruning may already have removed an inactive cell from
            # its projection. Include orphan files in the age-based cleanup.
            for path in fs.CELL_DIR.glob("*.json"):
                if path.stem.isdigit() and path.stem not in self.cell_timestamps:
                    self.cell_timestamps[path.stem] = path.stat().st_mtime

        snapshot = fs.STORMCELL_DIR / f"stormcells_{timestamp}.json"
        if snapshot.exists():
            self.stormcell_timestamps.add(timestamp)
        now = datetime.now(timezone.utc).timestamp()
        for cell_id in cell_ids:
            if (fs.CELL_DIR / f"{cell_id}.json").exists():
                self.cell_timestamps[str(cell_id)] = now

        self._initial_scan_done = True
        cleanup_started = time.perf_counter()
        if self.remove_old_cells:
            cutoff = now - inactive_cell_max_age_minutes() * 60
            for cell_id, touched_at in list(self.cell_timestamps.items()):
                if touched_at >= cutoff:
                    continue
                path = fs.CELL_DIR / f"{cell_id}.json"
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    self.io_manager.write_error(f"Failed to delete old cell file {cell_id}.json: {exc}")
                if not path.exists():
                    del self.cell_timestamps[cell_id]

        self.cleanup_seconds = time.perf_counter() - cleanup_started
        self._publish_field_index()
        write_started = time.perf_counter()
        self._write_cell_index()
        atomic_write_json(self.stormcell_index_path, {
            "timestamps": sorted(self.stormcell_timestamps),
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        }, indent=2)
        self.index_write_seconds = time.perf_counter() - write_started
