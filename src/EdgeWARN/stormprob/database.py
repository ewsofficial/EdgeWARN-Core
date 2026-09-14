"""SQLite source of truth for StormProb inputs and cycle publication.

Only the EdgeWARN publication process opens a writer. Readers use SQLite's
read-only URI and get a consistent WAL snapshot for each query. JSON cell
snapshots are retained solely as compatibility projections for legacy clients.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import struct
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import util.file as fs

from .features import HISTORY_STEPS, N_CURRENT, feature_order_checksum
from .geometry import N_RAYS
from .records import SCHEMA_VERSION, build_observation_record

DB_VERSION = 1
LEADS = (15, 30, 45, 60)
PENDING_MODEL_VERSION = "stormprob-pending/v1"


def database_path(base_dir: Path | str | None = None) -> Path:
    return Path(base_dir if base_dir is not None else fs.BASE_DIR) / "data" / "stormprob" / "stormprob.sqlite3"


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True, default=str)


def clean_projection(value: Any) -> Any:
    """Keep legacy projections JSON-safe without changing model input vectors."""
    if isinstance(value, dict):
        return {str(key): clean_projection(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_projection(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def clean_public_projection(value: Any) -> Any:
    """Apply the operational public contract to derived cell projections."""
    value = clean_projection(value)
    if not isinstance(value, dict):
        return value
    modules = value.get("modules")
    stormprob = modules.get("StormProb") if isinstance(modules, dict) else None
    if isinstance(stormprob, dict):
        public = {
            "status": stormprob.get("status"),
            "analysis_time": stormprob.get("analysis_time"),
            "leads": [],
        }
        for lead in stormprob.get("leads", []):
            if not isinstance(lead, dict):
                continue
            item = {
                "lead_minutes": lead.get("lead_minutes"),
                "valid_time": lead.get("valid_time"),
                "status": lead.get("status"),
            }
            if lead.get("status") == "ok":
                for key in ("east_km", "north_km", "predicted_centroid", "polygon"):
                    item[key] = lead.get(key)
            else:
                item["reason"] = lead.get("reason")
            public["leads"].append(item)
        modules["StormProb"] = public
    return value


def _time(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _finite_vector(values: Any, count: int) -> bytes:
    if not isinstance(values, (list, tuple)) or len(values) != count:
        raise ValueError(f"expected {count} vector elements")
    floats = [float(value) for value in values]
    if not all(math.isfinite(value) for value in floats):
        raise ValueError("non-finite model vector")
    return struct.pack(f"<{count}f", *floats)


def _unpack(data: bytes, count: int) -> list[float]:
    if len(data) != 4 * count:
        raise ValueError("corrupt float32 vector length")
    return list(struct.unpack(f"<{count}f", data))


class StormProbRepository:
    def __init__(self, base_dir: Path | str | None = None):
        self.path = database_path(base_dir)

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            self._migrate(db)
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > DB_VERSION:
            raise RuntimeError(f"StormProb database version {version} is newer than this reader")
        if version == DB_VERSION:
            return
        db.executescript("""
            CREATE TABLE IF NOT EXISTS cycles (
                cycle_id TEXT PRIMARY KEY, analysis_time TEXT NOT NULL,
                source_manifest_json TEXT NOT NULL, state TEXT NOT NULL,
                projection_hash TEXT, projection_json TEXT, projection_path TEXT,
                projection_state TEXT NOT NULL DEFAULT 'none',
                committed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cell_observations (
                cell_id TEXT NOT NULL, analysis_time TEXT NOT NULL,
                feature_schema_version TEXT NOT NULL, cycle_id TEXT NOT NULL,
                lineage_json TEXT NOT NULL, centroid_json TEXT NOT NULL,
                polygon_json TEXT NOT NULL, geometry_status TEXT NOT NULL,
                inference_ready INTEGER NOT NULL, reasons_json TEXT NOT NULL,
                legacy_projection_json TEXT NOT NULL,
                PRIMARY KEY(cell_id,analysis_time,feature_schema_version),
                FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id)
            );
            CREATE INDEX IF NOT EXISTS observation_history
                ON cell_observations(cell_id,analysis_time DESC);
            CREATE TABLE IF NOT EXISTS feature_values (
                cell_id TEXT NOT NULL, analysis_time TEXT NOT NULL,
                feature_schema_version TEXT NOT NULL, order_checksum TEXT NOT NULL,
                values_f32 BLOB NOT NULL, raw_values_json TEXT NOT NULL,
                units_json TEXT NOT NULL, quality_json TEXT NOT NULL,
                source_times_json TEXT NOT NULL,
                PRIMARY KEY(cell_id,analysis_time,feature_schema_version),
                FOREIGN KEY(cell_id,analysis_time,feature_schema_version)
                    REFERENCES cell_observations(cell_id,analysis_time,feature_schema_version)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS radial_profiles (
                cell_id TEXT NOT NULL, analysis_time TEXT NOT NULL,
                feature_schema_version TEXT NOT NULL, radii_f32 BLOB NOT NULL,
                log_area REAL, area_km2 REAL NOT NULL,
                PRIMARY KEY(cell_id,analysis_time,feature_schema_version),
                FOREIGN KEY(cell_id,analysis_time,feature_schema_version)
                    REFERENCES cell_observations(cell_id,analysis_time,feature_schema_version)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS forecasts (
                cell_id TEXT NOT NULL, analysis_time TEXT NOT NULL,
                lead_minutes INTEGER NOT NULL, model_version TEXT NOT NULL,
                radial_checkpoint_id TEXT, motion_checkpoint_id TEXT,
                cycle_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
                east_km REAL, north_km REAL, centroid_json TEXT,
                polygon_json TEXT, probability_threshold REAL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY(cell_id,analysis_time,lead_minutes,model_version),
                FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id),
                CHECK(lead_minutes IN (15,30,45,60))
            );
            CREATE TABLE IF NOT EXISTS legacy_sources (
                path TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                entry_count INTEGER NOT NULL, imported_count INTEGER NOT NULL,
                imported_at TEXT NOT NULL
            );
        """)
        db.execute(f"PRAGMA user_version={DB_VERSION}")
        db.commit()

    def integrity_check(self) -> None:
        with self.reader() as db:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"StormProb integrity check: {result}")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("StormProb foreign key violation")

    def backup(self, target: Path | str) -> Path:
        """Make a consistent SQLite backup; caller controls retention of files."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            with self.reader() as source, sqlite3.connect(temporary) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("StormProb backup integrity check failed")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def prune_backups(self, directory: Path | str, keep: int = 7) -> list[Path]:
        """Only remove managed backup files in the supplied backup directory."""
        if keep < 1:
            raise ValueError("keep must be positive")
        folder = Path(directory)
        if folder.resolve() != (self.path.parent / "backups").resolve():
            raise ValueError("backup retention is restricted to stormprob/backups")
        files = sorted(folder.glob("stormprob-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[keep:]:
            path.unlink()
        return files[keep:]

    def backup_if_due(self, *, now: datetime | None = None) -> Path | None:
        """Create at most one daily online backup and retain seven copies."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        folder = self.path.parent / "backups"
        target = folder / f"stormprob-{moment.astimezone(timezone.utc):%Y%m%d}.sqlite3"
        if target.exists():
            return None
        self.backup(target)
        self.prune_backups(folder)
        return target

    def commit_cycle(self, cycle_id: str, analysis_time: Any, cells: list[dict],
                     source_manifest: Any = None, projection_hash: str | None = None,
                     projection_cells: list[dict] | None = None,
                     projection_path: Path | str | None = None,
                     forecast_policy: str = "invalidate",
                     forecasts: list[dict] | None = None) -> int:
        """Atomically upsert one cycle and all its current input/status rows."""
        if forecast_policy not in ("invalidate", "preserve"):
            raise ValueError("forecast_policy must be invalidate or preserve")
        moment = _time(analysis_time)
        now = datetime.now(timezone.utc).isoformat()
        with self.writer() as db:
            db.execute("""INSERT INTO cycles VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cycle_id) DO UPDATE SET analysis_time=excluded.analysis_time,
                source_manifest_json=excluded.source_manifest_json,state=excluded.state,
                projection_hash=excluded.projection_hash,projection_json=excluded.projection_json,
                projection_path=excluded.projection_path,
                projection_state=excluded.projection_state,
                committed_at=excluded.committed_at""",
                (str(cycle_id), moment, _json(source_manifest), "inputs-committed",
                 projection_hash, _json(clean_projection(projection_cells)) if projection_cells is not None else None,
                 str(projection_path) if projection_path is not None else None,
                 "pending" if projection_path is not None else "none", now))
            count = 0
            for cell in cells:
                if not isinstance(cell, dict) or cell.get("id") is None or not cell.get("timestamp"):
                    continue
                record = (cell.get("stormprob") or {}).get("observation")
                if not isinstance(record, dict):
                    record = build_observation_record(cell, analysis_time=cell["timestamp"])
                if forecast_policy == "invalidate":
                    db.execute("""DELETE FROM forecasts WHERE cell_id=? AND analysis_time=?
                        AND model_version<>?""", (str(cell["id"]), _time(record["analysis_time"]),
                                                 PENDING_MODEL_VERSION))
                self._upsert_observation(db, cell, record, str(cycle_id))
                count += 1
            if forecasts:
                expected = {(str(cell["id"]), _time(cell["timestamp"])) for cell in cells
                            if isinstance(cell, dict) and cell.get("id") is not None
                            and cell.get("timestamp")}
                groups: dict[tuple[str, str, str], set[int]] = {}
                for forecast in forecasts:
                    key = (str(forecast["cell_id"]), _time(forecast["analysis_time"]))
                    if key not in expected:
                        raise ValueError("forecast does not belong to committed cycle")
                    group = (*key, str(forecast["model_version"]))
                    leads = groups.setdefault(group, set())
                    lead = int(forecast["lead_minutes"])
                    if lead in leads:
                        raise ValueError("duplicate forecast lead")
                    leads.add(lead)
                if any(leads != set(LEADS) for leads in groups.values()):
                    raise ValueError("model forecast must include all four leads")
                for forecast in forecasts:
                    self._upsert_forecast(db, str(cycle_id), forecast)
            return count

    @staticmethod
    def _upsert_forecast(db: sqlite3.Connection, cycle_id: str, forecast: dict) -> None:
        lead = int(forecast["lead_minutes"])
        if lead not in LEADS:
            raise ValueError("unsupported forecast lead")
        model = str(forecast["model_version"])
        if not model or model == PENDING_MODEL_VERSION:
            raise ValueError("forecast needs a deployed model version")
        status = str(forecast["status"])
        if status not in ("ok", "no-polygon", "skipped", "error"):
            raise ValueError("invalid forecast status")
        east = forecast.get("east_km")
        north = forecast.get("north_km")
        threshold = forecast.get("probability_threshold")
        for value in (east, north, threshold):
            if value is not None and not math.isfinite(float(value)):
                raise ValueError("non-finite forecast number")
        if status == "ok" and (east is None or north is None or forecast.get("polygon") is None):
            raise ValueError("successful forecast requires displacement and polygon")
        db.execute("""INSERT INTO forecasts
            (cell_id,analysis_time,lead_minutes,model_version,radial_checkpoint_id,
             motion_checkpoint_id,cycle_id,status,reason,
             east_km,north_km,centroid_json,polygon_json,probability_threshold,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cell_id,analysis_time,lead_minutes,model_version) DO UPDATE SET
            radial_checkpoint_id=excluded.radial_checkpoint_id,
            motion_checkpoint_id=excluded.motion_checkpoint_id,
            cycle_id=excluded.cycle_id,status=excluded.status,reason=excluded.reason,
            east_km=excluded.east_km,north_km=excluded.north_km,
            centroid_json=excluded.centroid_json,polygon_json=excluded.polygon_json,
            probability_threshold=excluded.probability_threshold,
            metadata_json=excluded.metadata_json""",
            (str(forecast["cell_id"]), _time(forecast["analysis_time"]), lead, model,
             forecast.get("radial_checkpoint_id"), forecast.get("motion_checkpoint_id"),
             cycle_id, status, forecast.get("reason"), east, north,
             _json(forecast.get("predicted_centroid")), _json(forecast.get("polygon")),
             threshold, _json(forecast.get("metadata", {}))))

    @staticmethod
    def _upsert_observation(db: sqlite3.Connection, cell: dict, record: dict, cycle_id: str) -> None:
        cell_id = str(cell["id"])
        moment = _time(record["analysis_time"])
        schema = record["schema_version"]
        values = _finite_vector(record["current_features_raw"], N_CURRENT)
        radii = _finite_vector(record["radial_profile"], N_RAYS)
        area = float(record.get("radial_area_km2") or 0)
        log_area = record.get("radial_log_area")
        if log_area is not None and not math.isfinite(float(log_area)):
            raise ValueError("non-finite radial log area")
        if not math.isfinite(area):
            raise ValueError("non-finite radial area")
        projection = dict(cell)
        projection.pop("stormprob", None)
        projection = clean_projection(projection)
        key = (cell_id, moment, schema)
        db.execute("""INSERT INTO cell_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cell_id,analysis_time,feature_schema_version) DO UPDATE SET
            cycle_id=excluded.cycle_id,lineage_json=excluded.lineage_json,
            centroid_json=excluded.centroid_json,polygon_json=excluded.polygon_json,
            geometry_status=excluded.geometry_status,inference_ready=excluded.inference_ready,
            reasons_json=excluded.reasons_json,legacy_projection_json=excluded.legacy_projection_json""",
            (*key, cycle_id, _json(record.get("lineage", {})), _json(record.get("centroid")),
             _json(record.get("polygon")), record.get("geometry_status", "skipped"),
             int(bool(record.get("inference_ready"))), _json(record.get("reasons", [])),
             _json(projection)))
        db.execute("""INSERT INTO feature_values VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cell_id,analysis_time,feature_schema_version) DO UPDATE SET
            order_checksum=excluded.order_checksum,values_f32=excluded.values_f32,
            raw_values_json=excluded.raw_values_json,units_json=excluded.units_json,
            quality_json=excluded.quality_json,source_times_json=excluded.source_times_json""",
            (*key, record.get("feature_order_checksum", feature_order_checksum()), values,
             _json(record.get("raw_values", {})), _json(record.get("units", {})),
             _json(record.get("quality", {})), _json(record.get("source_times", {}))))
        db.execute("""INSERT INTO radial_profiles VALUES(?,?,?,?,?,?)
            ON CONFLICT(cell_id,analysis_time,feature_schema_version) DO UPDATE SET
            radii_f32=excluded.radii_f32,log_area=excluded.log_area,area_km2=excluded.area_km2""",
            (*key, radii, log_area, area))
        # The actual models arrive in Phase 3/4. Record explicit status for all
        # four leads, so no committed input cycle appears to contain predictions.
        for lead in LEADS:
            db.execute("""INSERT INTO forecasts
                (cell_id,analysis_time,lead_minutes,model_version,cycle_id,status,reason,metadata_json)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cell_id,analysis_time,lead_minutes,model_version)
                DO UPDATE SET cycle_id=excluded.cycle_id,status=excluded.status,
                reason=excluded.reason,metadata_json=excluded.metadata_json""",
                (cell_id, moment, lead, PENDING_MODEL_VERSION, cycle_id,
                 "not-computed" if record.get("inference_ready") else "skipped",
                 "model-not-deployed" if record.get("inference_ready") else "input-not-ready",
                 _json({"phase": 2})))

    def feature_history(self, cell_id: Any, limit: int | None = None,
                        before: Any | None = None,
                        through: Any | None = None) -> list[dict]:
        limit = HISTORY_STEPS if limit is None else limit
        if limit < 1:
            return []
        if before is not None and through is not None:
            raise ValueError("choose before or through")
        clause = "AND o.analysis_time < ?" if before is not None else (
            "AND o.analysis_time <= ?" if through is not None else "")
        params = [str(cell_id), SCHEMA_VERSION]
        if before is not None or through is not None:
            params.append(_time(before if before is not None else through))
        params.append(limit)
        with self.reader() as db:
            rows = db.execute(f"""SELECT o.*,f.*,r.radii_f32,r.log_area,r.area_km2
                FROM cell_observations o JOIN feature_values f USING(cell_id,analysis_time,feature_schema_version)
                JOIN radial_profiles r USING(cell_id,analysis_time,feature_schema_version)
                JOIN cycles c ON c.cycle_id=o.cycle_id
                WHERE o.cell_id=? AND o.feature_schema_version=?
                AND c.state IN ('inputs-committed','legacy-imported')
                {clause} ORDER BY o.analysis_time DESC LIMIT ?""", params).fetchall()
        if any(row["order_checksum"] != feature_order_checksum() for row in rows):
            raise RuntimeError("StormProb feature order checksum mismatch")
        return [{"cell_id": row["cell_id"], "analysis_time": row["analysis_time"],
                 "centroid": json.loads(row["centroid_json"]),
                 "polygon": json.loads(row["polygon_json"]),
                 "lineage": json.loads(row["lineage_json"]),
                 "geometry_status": row["geometry_status"],
                 "inference_ready": bool(row["inference_ready"]),
                 "feature_schema_version": row["feature_schema_version"],
                 "order_checksum": row["order_checksum"],
                 "features": _unpack(row["values_f32"], N_CURRENT),
                 "raw_values": json.loads(row["raw_values_json"]),
                 "units": json.loads(row["units_json"]),
                 "quality": json.loads(row["quality_json"]),
                 "source_times": json.loads(row["source_times_json"]),
                 "radial_profile": _unpack(row["radii_f32"], N_RAYS),
                 "radial_log_area": row["log_area"]} for row in rows]

    def model_inputs(self, cell_id: Any, *, through: Any | None = None) -> dict:
        """Build 30-step tensors only from committed database feature rows."""
        from .features import HISTORY_STEPS, N_TRAJECTORY
        from .tracks import build_trajectory_sequence

        # Derive trajectory/age from the complete committed track, then retain
        # the last 30 rows. Truncating first would reset storm age at row 31.
        history = list(reversed(self.feature_history(cell_id, limit=1_000_000,
                                                      through=through)))
        if not history:
            raise ValueError(f"no committed features for cell {cell_id}")
        centroids = [row["centroid"] for row in history]
        times = [datetime.fromisoformat(row["analysis_time"]).timestamp() for row in history]
        trajectory, _ = build_trajectory_sequence(centroids, times, HISTORY_STEPS)
        vectors = []
        for index, row in enumerate(history):
            vector = list(row["features"])
            vector[-2] = times[index] - times[0]
            vector[-1] = float(min(index + 1, HISTORY_STEPS))
            vectors.append(vector)
        history = history[-HISTORY_STEPS:]
        vectors = vectors[-HISTORY_STEPS:]
        trajectory = trajectory[-HISTORY_STEPS:]
        pad = HISTORY_STEPS - len(history)
        valid_radial = [row["radial_log_area"] is not None and row["geometry_status"] == "ok"
                        for row in history]
        return {
            "current": vectors[-1],
            "history_sequence": [[0.0] * N_CURRENT for _ in range(pad)] + vectors,
            "history_mask": [False] * pad + [True] * len(history),
            "trajectory_sequence": [[0.0] * N_TRAJECTORY for _ in range(pad)] + trajectory,
            "trajectory_mask": [False] * pad + [True] * len(history),
            "radial_history": [[0.0] * N_RAYS for _ in range(pad)] +
                              [row["radial_profile"] if valid else [0.0] * N_RAYS
                               for row, valid in zip(history, valid_radial)],
            "radial_statistics_history": [[0.0] for _ in range(pad)] +
                                         [[row["radial_log_area"]] if valid else [0.0]
                                          for row, valid in zip(history, valid_radial)],
            "radial_history_mask": [False] * pad + valid_radial,
        }

    def legacy_history(self, cell_id: Any, limit: int | None = None,
                       before: Any | None = None,
                       through: Any | None = None) -> list[dict]:
        """Read-only legacy-shaped projection, oldest first."""
        if before is not None and through is not None:
            raise ValueError("choose before or through")
        clause = "AND o.analysis_time < ?" if before is not None else (
            "AND o.analysis_time <= ?" if through is not None else "")
        params: list[Any] = [str(cell_id)]
        if before is not None or through is not None:
            params.append(_time(before if before is not None else through))
        params.append(limit if limit is not None else 1000000)
        with self.reader() as db:
            rows = db.execute(f"""SELECT o.legacy_projection_json FROM cell_observations o
                JOIN cycles c ON c.cycle_id=o.cycle_id
                WHERE o.cell_id=? AND c.state IN ('inputs-committed','legacy-imported') {clause}
                ORDER BY o.analysis_time DESC LIMIT ?""", params).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def latest_cycle_before(self, analysis_time: Any) -> list[dict]:
        with self.reader() as db:
            row = db.execute("""SELECT cycle_id,projection_json FROM cycles WHERE analysis_time < ?
                AND state='inputs-committed' AND projection_state='published'
                ORDER BY analysis_time DESC LIMIT 1""",
                (_time(analysis_time),)).fetchone()
            if row is None:
                return []
            if row["projection_json"] is not None:
                return json.loads(row["projection_json"])
            rows = db.execute("""SELECT legacy_projection_json FROM cell_observations
                WHERE cycle_id=? ORDER BY cell_id""", (row[0],)).fetchall()
        return [json.loads(item[0]) for item in rows]

    def cycle_projection_hash(self, cycle_id: str) -> str | None:
        with self.reader() as db:
            row = db.execute("SELECT projection_hash FROM cycles WHERE cycle_id=?", (str(cycle_id),)).fetchone()
        return row[0] if row else None

    def index_projection(self) -> tuple[list[str], dict[str, float]]:
        """Committed paths/IDs for the derived legacy API indexes."""
        with self.reader() as db:
            cycles = db.execute("""SELECT projection_path FROM cycles WHERE
                state='inputs-committed' AND projection_state='published'
                AND projection_path IS NOT NULL""").fetchall()
            cells = db.execute("""SELECT o.cell_id,MAX(c.committed_at) AS committed_at
                FROM cell_observations o JOIN cycles c ON c.cycle_id=o.cycle_id
                WHERE c.state='inputs-committed' AND c.projection_state='published'
                GROUP BY o.cell_id""").fetchall()
        timestamps = []
        for row in cycles:
            path = Path(row[0])
            if path.exists() and path.stem.startswith("stormcells_"):
                timestamps.append(path.stem.removeprefix("stormcells_"))
        cell_dir = self.path.parent.parent / "cells"
        ids = {row["cell_id"]: datetime.fromisoformat(row["committed_at"]).timestamp()
               for row in cells if (cell_dir / f"{row['cell_id']}.json").exists()}
        return sorted(set(timestamps)), ids

    def recover_projections(self) -> list[Path]:
        """Republish missing compatibility files from committed cycle rows.

        This handles a crash after the SQLite commit but before the CTAM JSON
        journal was prepared. Existing JSON is left for journal recovery.
        """
        if not self.path.exists():
            return []
        from EdgeWARN.ctam.publication import CTAMPublicationCoordinator
        from EdgeWARN.process.detect.tools.save import CellDataSaver
        from util.atomic import atomic_write_json

        journal = self.path.parent.parent / "ctam" / "transactions"
        recovered_journals = CTAMPublicationCoordinator(journal).recover()
        with self.reader() as db:
            rows = db.execute("""SELECT cycle_id,projection_json,projection_path FROM cycles
                WHERE state='inputs-committed' AND projection_state='pending'
                AND projection_json IS NOT NULL
                AND projection_path IS NOT NULL ORDER BY analysis_time""").fetchall()
        restored: list[Path] = list(recovered_journals)
        touched_ids: set[str] = set()
        stormcell_dir = self.path.parent.parent / "stormcells"
        cell_dir = self.path.parent.parent / "cells"
        saver = CellDataSaver(None, None, None, None, None, None)
        for row in rows:
            target = Path(row["projection_path"])
            if not target.resolve().is_relative_to(stormcell_dir.resolve()):
                raise ValueError(f"projection path escapes runtime stormcell directory: {target}")
            cells = json.loads(row["projection_json"])
            if not target.exists():
                atomic_write_json(target, saver.create_json_structure(row["cycle_id"], cells))
                restored.append(target)
            for cell in cells:
                if isinstance(cell, dict) and cell.get("id") is not None and cell.get("timestamp"):
                    touched_ids.add(str(cell["id"]))
        for cell_id in touched_ids:
            target = cell_dir / f"{cell_id}.json"
            if not target.exists():
                atomic_write_json(target, self.legacy_history(cell_id))
                restored.append(target)
        return restored

    def pending_projection_cycles(self) -> list[str]:
        if not self.path.exists():
            return []
        with self.reader() as db:
            rows = db.execute("""SELECT cycle_id FROM cycles WHERE
                state='inputs-committed' AND projection_state='pending'""").fetchall()
        return [row[0] for row in rows]

    def mark_projection_published(self, cycle_id: str) -> None:
        with self.writer() as db:
            row = db.execute("SELECT projection_path FROM cycles WHERE cycle_id=?",
                             (str(cycle_id),)).fetchone()
            if row is None or row[0] is None or not Path(row[0]).exists():
                raise RuntimeError("cannot mark missing projection published")
            db.execute("UPDATE cycles SET projection_state='published' WHERE cycle_id=?",
                       (str(cycle_id),))

    def import_legacy_file(self, path: Path | str) -> dict:
        """Resumable one-file migration with hash verification and no overwrites.

        Reimporting identical bytes is a no-op. Changed source bytes are an
        error requiring operator review, not a silent rewrite of old features.
        """
        path = Path(path)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        payload = json.loads(content)
        entries = payload.get("features", []) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ValueError(f"invalid legacy array: {path}")
        with self.writer() as db:
            prior = db.execute("SELECT sha256,entry_count,imported_count FROM legacy_sources WHERE path=?",
                               (str(path),)).fetchone()
            if prior:
                if prior[0] != digest or prior[1] != len(entries):
                    raise ValueError(f"legacy source changed after import: {path}")
                return {"path": str(path), "entries": prior[1], "imported": prior[2], "sha256": digest, "skipped": True}
            valid = [cell for cell in entries if isinstance(cell, dict) and cell.get("id") is not None
                     and (cell.get("timestamp") or (cell.get("properties") or {}).get("timestamp"))]
            invalid = len(entries) - len(valid)
            if invalid:
                raise ValueError(f"{path}: {invalid} entries lack identity/timestamp")
            cycle_id = "legacy:" + digest
            fallback_time = valid[0].get("timestamp") if valid else "1970-01-01T00:00:00Z"
            db.execute("INSERT INTO cycles VALUES(?,?,?,?,?,?,?,?,?)",
                       (cycle_id, _time(fallback_time), _json({"legacy_path": str(path), "sha256": digest}),
                        "legacy-imported", None, None, None, "none",
                        datetime.now(timezone.utc).isoformat()))
            imported = 0
            for cell in valid:
                item = dict(cell)
                item["timestamp"] = item.get("timestamp") or item["properties"]["timestamp"]
                record = build_observation_record(item, analysis_time=item["timestamp"])
                # Missing backfill fields remain marked missing by the Phase 1
                # extractor; no synthetic weather values are introduced.
                self._upsert_observation(db, item, record, cycle_id)
                imported += 1
            db.execute("INSERT INTO legacy_sources VALUES(?,?,?,?,?)",
                       (str(path), digest, len(entries), imported, datetime.now(timezone.utc).isoformat()))
            return {"path": str(path), "entries": len(entries), "imported": imported,
                    "sha256": digest, "skipped": False}


__all__ = ["StormProbRepository", "database_path", "LEADS", "PENDING_MODEL_VERSION",
           "clean_projection", "clean_public_projection"]
