"""
Alert Manager

Provides centralised publishing and retrieval of AlertPayloads
to/from the filesystem.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import util.file as fs
from util.atomic import atomic_write_json
from util.io import IOManager

from .schema import AlertPayload

io_manager = IOManager("[Alerts]")


class AlertManager:
    """
    Centralised alert publisher.

    Writes alert JSON files to ``fs.EDGEWARN_ALERTS_IDS_DIR`` using the naming convention
    ``{id}.json``, where id safely replaces colons.
    Snapshots are created in ``fs.EDGEWARN_ALERTS_TS_DIR`` per radar scan timestamp.
    """

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @staticmethod
    def publish(alert: AlertPayload) -> bool:
        """
        Persist an alert to disk.

        Args:
            alert: A fully-populated ``AlertPayload``.

        Returns:
            ``True`` on success, ``False`` otherwise.
        """
        if not alert.cell_id or not alert.geometry:
            return False

        try:
            fs.EDGEWARN_ALERTS_IDS_DIR.mkdir(parents=True, exist_ok=True)
            fs.EDGEWARN_ALERTS_TS_DIR.mkdir(parents=True, exist_ok=True)

            # Safe filename corresponding to unique ID
            safe_id = alert.id.replace(":", "_").replace("/", "_") + ".json"
            alert_file = fs.EDGEWARN_ALERTS_IDS_DIR / safe_id

            atomic_write_json(alert_file, alert.to_dict(), indent=4)

            return True

        except Exception as e:
            io_manager.write_error(
                f"Failed to publish alert for cell {alert.cell_id} "
                f"from {alert.source}: {e}"
            )
            return False

    @staticmethod
    def publish_many(alerts: List[AlertPayload]) -> int:
        """
        Publish a batch of alerts.

        Returns:
            The number of alerts successfully written.
        """
        return sum(1 for a in alerts if AlertManager.publish(a))

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @staticmethod
    def _dict_to_payload(data: Dict[str, Any]) -> AlertPayload:
        """Reconstruct an AlertPayload from a JSON-parsed dictionary."""
        def _parse_dt(val: str) -> datetime:
            try:
                return datetime.fromisoformat(val)
            except (ValueError, TypeError):
                return datetime.now()

        return AlertPayload(
            alert_type=data.get("alert_type", "unknown"),
            source=data.get("source", "unknown"),
            cell_id=data.get("cell_id", data.get("id", "")),
            geometry=data.get("geometry", []),
            effective_time=_parse_dt(data.get("effective", "")),
            expiry_time=_parse_dt(data.get("expires", "")),
            severity=data.get("severity", "warning"),
            threats=data.get("threats", {}),
        )

    @staticmethod
    def load_by_id(alert_id: str) -> Optional[AlertPayload]:
        """
        Load a single alert from disk by its unique alert_id.

        Args:
            alert_id: The unique alert identifier.

        Returns:
            The ``AlertPayload`` if the file exists, otherwise ``None``.
        """
        safe_id = alert_id.replace(":", "_").replace("/", "_") + ".json"
        alert_file = fs.EDGEWARN_ALERTS_IDS_DIR / safe_id
        if not alert_file.exists():
            return None

        try:
            with open(alert_file, "r") as f:
                data = json.load(f)
            return AlertManager._dict_to_payload(data)
        except Exception as e:
            io_manager.write_error(
                f"Failed to load alert {alert_file.name}: {e}"
            )
            return None

    @staticmethod
    def load(source: str, cell_id: str) -> Optional[AlertPayload]:
        """
        DEPRECATED: Left for compatibility. Finds the newest alert for a source and cell.
        """
        alerts = AlertManager.load_all(cell_id)
        source_alerts = sorted(
            [a for a in alerts if a.source == source],
            key=lambda a: a.effective_time,
            reverse=True
        )
        return source_alerts[0] if source_alerts else None

    @staticmethod
    def load_latest_for_cells(
        source: str,
        cell_ids,
    ) -> Dict[str, AlertPayload]:
        """Load the newest matching alert per cell with one directory scan.

        StormProb evaluates hundreds of cells per cycle. Calling ``load()`` for
        each cell repeatedly opened every alert file, making alert lookup
        quadratic in cell and retained-alert counts on slow runtime volumes.
        """
        wanted = {str(cell_id) for cell_id in cell_ids}
        if not wanted or not fs.EDGEWARN_ALERTS_IDS_DIR.exists():
            return {}
        latest: Dict[str, AlertPayload] = {}
        for path in fs.EDGEWARN_ALERTS_IDS_DIR.glob("*.json"):
            try:
                with open(path, "r") as handle:
                    data = json.load(handle)
                cell_id = str(data.get("cell_id", ""))
                if data.get("source") != source or cell_id not in wanted:
                    continue
                alert = AlertManager._dict_to_payload(data)
                prior = latest.get(cell_id)
                if prior is None or alert.effective_time > prior.effective_time:
                    latest[cell_id] = alert
            except Exception as exc:
                io_manager.write_error(
                    f"Failed to read alert {path.name} during batch lookup: {exc}"
                )
        return latest

    @staticmethod
    def load_all(cell_id: str) -> List[AlertPayload]:
        """
        Load every alert for a given cell, regardless of source.

        Args:
            cell_id: The storm cell identifier.

        Returns:
            A list of ``AlertPayload`` objects (may be empty).
        """
        if not fs.EDGEWARN_ALERTS_IDS_DIR.exists():
            return []

        results: List[AlertPayload] = []
        for path in fs.EDGEWARN_ALERTS_IDS_DIR.glob("*.json"):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                
                # Check if this alert belongs to the requested cell
                if data.get("cell_id") == cell_id:
                    results.append(AlertManager._dict_to_payload(data))
            except Exception as e:
                io_manager.write_error(
                    f"Failed to load alert {path.name}: {e}"
                )
        return results

    # ------------------------------------------------------------------
    # Snapshots & Cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def create_snapshot(timestamp: str) -> bool:
        """
        Create a snapshot of all currently active alert IDs for the given timestamp.
        Writes `{timestamp}.json` to fs.EDGEWARN_ALERTS_TS_DIR.

        Args:
            timestamp: Can be YYYYMMDD-HHMMSS or ISO 8601 (e.g. from JSON).

        Returns:
            True on success, False otherwise.
        """
        if not fs.EDGEWARN_ALERTS_IDS_DIR.exists():
            return False

        fs.EDGEWARN_ALERTS_TS_DIR.mkdir(parents=True, exist_ok=True)
        
        active_alerts = []
        
        # We need to parse timestamp back to a naive datetime for comparison
        try:
            # Try ISO format first (from load_json)
            scan_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except ValueError:
            try:
                # Try YYYYMMDD-HHMMSS format
                scan_dt = datetime.strptime(timestamp, "%Y%m%d-%H%M%S")
                scan_dt = scan_dt.replace(tzinfo=timezone.utc)
            except Exception as e:
                io_manager.write_error(f"Failed to parse snapshot timestamp {timestamp}: {e}")
                return False

        if scan_dt.tzinfo is None:
            scan_dt = scan_dt.replace(tzinfo=timezone.utc)

        for path in fs.EDGEWARN_ALERTS_IDS_DIR.glob("*.json"):
            if not path.is_file():
                continue

            try:
                with open(path, "r") as f:
                    data = json.load(f)
                
                # Check if it's active at `scan_dt`
                eff_str = data.get("effective")
                exp_str = data.get("expires")
                if eff_str and exp_str:
                    eff_dt = datetime.fromisoformat(eff_str)
                    exp_dt = datetime.fromisoformat(exp_str)
                    
                    if eff_dt.tzinfo is None: eff_dt = eff_dt.replace(tzinfo=timezone.utc)
                    if exp_dt.tzinfo is None: exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    
                    if eff_dt <= scan_dt <= exp_dt:
                        active_alerts.append({
                            "id": data.get("id"),
                            "severity": data.get("severity", "warning")
                        })
            except Exception as e:
                pass
        
        file_ts_str = scan_dt.strftime("%Y%m%d-%H%M%S")
        snapshot_file = fs.EDGEWARN_ALERTS_TS_DIR / f"{file_ts_str}.json"
        
        try:
            atomic_write_json(snapshot_file, {
                "timestamp": scan_dt.isoformat(),
                "count": len(active_alerts),
                "alerts": active_alerts
            }, indent=4)
            return True
        except Exception as e:
            io_manager.write_error(f"Failed to write snapshot {snapshot_file.name}: {e}")
            return False

    @staticmethod
    def cleanup_expired(max_age_minutes: Optional[int] = 120) -> int:
        """
        Delete expired or stale alert files and old snapshots from disk.

        Args:
            max_age_minutes: Maximum age of a file (in minutes) before it is unconditionally
                             deleted as a safety measure.

        Returns:
            The number of files deleted.
        """
        deleted_count = 0
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        
        cutoff_ts = None
        if max_age_minutes is not None:
            cutoff_ts = now_ts - (max_age_minutes * 60)

        # 1. Clean IDs
        if fs.EDGEWARN_ALERTS_IDS_DIR.exists():
            for path in fs.EDGEWARN_ALERTS_IDS_DIR.glob("*.json"):
                if not path.is_file():
                    continue

                should_delete = False

                try:
                    with open(path, "r") as f:
                        data = json.load(f)

                    expires_str = data.get("expires")
                    if expires_str:
                        # Semantic expiry takes precedence over file mtime.
                        expires_dt = datetime.fromisoformat(expires_str)
                        if expires_dt.tzinfo is None:
                            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                        if expires_dt < now:
                            should_delete = True
                    elif cutoff_ts is not None and path.stat().st_mtime < cutoff_ts:
                        # Fallback for files that cannot provide semantic expiry.
                        should_delete = True
                except Exception as e:
                    io_manager.write_warning(f"Failed to read/parse {path.name} during cleanup: {e}")
                    # If unreadable/malformed, fall back to mtime policy.
                    if cutoff_ts is not None and path.stat().st_mtime < cutoff_ts:
                        should_delete = True

                if should_delete:
                    try:
                        path.unlink()
                        deleted_count += 1
                    except Exception:
                        pass

        # 2. Clean Timestamps
        if fs.EDGEWARN_ALERTS_TS_DIR.exists() and cutoff_ts is not None:
            for path in fs.EDGEWARN_ALERTS_TS_DIR.glob("*.json"):
                if not path.is_file():
                    continue
                
                # We can safely delete snapshots older than the cutoff
                if path.stat().st_mtime < cutoff_ts:
                    try:
                        path.unlink()
                        deleted_count += 1
                    except Exception:
                        pass

        if deleted_count > 0:
            io_manager.write_info(f"Cleaned up {deleted_count} stale EdgeWARN alert/snapshot files.")

        return deleted_count
