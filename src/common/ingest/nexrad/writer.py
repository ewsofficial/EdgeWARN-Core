import json
import re
import shutil
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import util.file as fs
from common.ingest.nexrad import config as nexrad_config
from common.ingest.nexrad.parser import DREF_BLOCK, filter_msg31_blocks, iter_metadata_records, iter_sweep_records
from common.ingest.nexrad.s3_chunks import extract_volume_timestamp, format_nexrad_timestamp, parse_nexrad_timestamp, required_volume_chunks
from common.ingest.nexrad.models import ElevationArtifact, ElevationGroup
from util.atomic import atomic_write_text, atomic_output_path

IMPORTANT_DATA_VARS = None
SCAN_TIMESTAMP_RE = re.compile(r"^\d{8}-\d{6}$")


def _write_text_if_changed(path: Path, content: str) -> Path:
    """Publish manifest text through a temp file + atomic replace.

    The final filename is observable by the render/API side, so a crash mid
    write must never leave a truncated manifest visible under its real name.
    """
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return path
        except Exception:
            pass
    return atomic_write_text(path, content)


def _filename_timestamp(timestamp: str | None) -> str:
    if not timestamp:
        return "unknown"
    parsed = parse_nexrad_timestamp(timestamp)
    if parsed is not None:
        formatted = format_nexrad_timestamp(parsed)
        if formatted is not None:
            return formatted
    return str(timestamp).replace(":", "-")


def _utc_now_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NexradLocalChunkStore:
    def scan_output_dir(self, site: str, volume_id: str, chunks) -> Path:
        timestamp = extract_volume_timestamp(volume_id, chunks)
        return fs.NEXRAD_LEVEL2_DIR / site.upper() / timestamp

    def chunk_output_dir(self, site: str, volume_id: str, chunks) -> Path:
        return self.scan_output_dir(site, volume_id, chunks) / "chunks"

    def volume_output_path(self, site: str, volume_id: str, chunks) -> Path:
        scan_dir = self.scan_output_dir(site, volume_id, chunks)
        return scan_dir / f"{str(site).upper()}_{scan_dir.name}_{volume_id}.ar2v"

    def local_volume_file_complete(self, site: str, volume_id: str, chunks) -> bool:
        needed_chunks = required_volume_chunks(chunks)
        if not needed_chunks:
            return False

        volume_path = self.volume_output_path(site, volume_id, chunks)
        return volume_path.exists()

    def prune_station_scan_dirs(self, site: str, keep_timestamp: str):
        site_dir = fs.NEXRAD_LEVEL2_DIR / str(site).upper()
        if not site_dir.exists():
            return

        timestamp_dirs = sorted(
            (
                child for child in site_dir.iterdir()
                if child.is_dir() and SCAN_TIMESTAMP_RE.fullmatch(child.name)
            ),
            key=lambda child: child.name,
            reverse=True,
        )
        keep_dirs = {child.name for child in timestamp_dirs[:nexrad_config.scan_dirs_to_keep()]}
        keep_dirs.add(keep_timestamp)

        for child in timestamp_dirs:
            if child.name in keep_dirs:
                continue
            shutil.rmtree(child, ignore_errors=True)


class NexradElevationStore:
    """Manages per-elevation public outputs and internal scratch paths."""

    def elevation_dir(self, site: str, elevation: str) -> Path:
        return fs.NEXRAD_LEVEL2_DIR / str(site).upper() / str(elevation)

    def elevation_netcdf_path(
        self, site: str, elevation: str, elevation_timestamp: str
    ) -> Path:
        ts = _filename_timestamp(elevation_timestamp)
        stem = f"{str(site).upper()}_{elevation}_{ts}"
        return self.elevation_dir(site, elevation) / f"{stem}.nc"

    def elevation_ar2v_path(
        self, site: str, elevation: str, elevation_timestamp: str
    ) -> Path:
        ts = _filename_timestamp(elevation_timestamp)
        stem = f"{str(site).upper()}_{elevation}_{ts}"
        return self.elevation_dir(site, elevation) / f"{stem}.ar2v"

    def elevation_manifest_path(
        self, site: str, elevation: str, elevation_timestamp: str
    ) -> Path:
        ts = _filename_timestamp(elevation_timestamp)
        stem = f"{str(site).upper()}_{elevation}_{ts}"
        return self.elevation_dir(site, elevation) / f"{stem}.json"

    def runtime_dir(self, site: str) -> Path:
        return fs.NEXRAD_LEVEL2_DIR / ".runtime" / str(site).upper()

    def runtime_scan_path(self, site: str, volume_id: str) -> Path:
        runtime = self.runtime_dir(site)
        runtime.mkdir(parents=True, exist_ok=True)
        return runtime / f"{str(site).upper()}_{volume_id}.ar2v"

    def prune_runtime_volumes(self, site: str, keep_volume_id: str, *, assume_locked=False) -> None:
        runtime = self.runtime_dir(site)
        if not runtime.exists():
            return

        if not assume_locked:
            from util.runtime.handoff import _AdvisoryFileLock
            lock = _AdvisoryFileLock(runtime / ".locks" / "site.lock")
            try:
                lock.acquire()
            except OSError:
                return
            try:
                return self.prune_runtime_volumes(site, keep_volume_id, assume_locked=True)
            finally:
                lock.release()
        keep_prefix = f"{str(site).upper()}_{keep_volume_id}"
        for child in runtime.iterdir():
            if child.name.startswith(keep_prefix) or child.name == ".locks" or ".snapshot-" in child.name:
                continue
            if child.suffix in {".ar2v", ".json", ".part"}:
                child.unlink(missing_ok=True)

    def site_manifest_path(self, site: str) -> Path:
        return fs.NEXRAD_LEVEL2_DIR / str(site).upper() / "manifest.json"

    def prune_elevation_artifacts(self, site: str, elevation: str):
        elev_dir = self.elevation_dir(site, elevation)
        if not elev_dir.exists():
            return

        nc_files = sorted(
            (f for f in elev_dir.iterdir() if f.suffix == ".nc"),
            key=lambda f: f.name,
            reverse=True,
        )
        ar2v_files = sorted(
            (f for f in elev_dir.iterdir() if f.suffix == ".ar2v"),
            key=lambda f: f.name,
            reverse=True,
        )
        dirs_to_keep = nexrad_config.elevation_dirs_to_keep()
        keep = {f.name for f in nc_files[:dirs_to_keep]}
        keep.update(f.name for f in ar2v_files[:dirs_to_keep])

        for f in [*nc_files, *ar2v_files]:
            if f.name in keep:
                continue
            f.unlink(missing_ok=True)
            json_path = f.with_suffix(".json")
            json_path.unlink(missing_ok=True)


def _elevation_store() -> NexradElevationStore:
    return NexradElevationStore()


def elevation_dir(site: str, elevation: str) -> Path:
    return _elevation_store().elevation_dir(site, elevation)


def elevation_netcdf_path(site: str, elevation: str, elevation_timestamp: str) -> Path:
    return _elevation_store().elevation_netcdf_path(site, elevation, elevation_timestamp)


def elevation_ar2v_path(site: str, elevation: str, elevation_timestamp: str) -> Path:
    return _elevation_store().elevation_ar2v_path(site, elevation, elevation_timestamp)


def elevation_manifest_path(site: str, elevation: str, elevation_timestamp: str) -> Path:
    return _elevation_store().elevation_manifest_path(site, elevation, elevation_timestamp)


def runtime_dir(site: str) -> Path:
    return _elevation_store().runtime_dir(site)


def runtime_scan_path(site: str, volume_id: str) -> Path:
    return _elevation_store().runtime_scan_path(site, volume_id)


def prune_runtime_volumes(site: str, keep_volume_id: str, *, assume_locked=False):
    _elevation_store().prune_runtime_volumes(site, keep_volume_id, assume_locked=assume_locked)


def site_manifest_path(site: str) -> Path:
    return _elevation_store().site_manifest_path(site)


def local_elevation_complete(site: str, elevation: str, elevation_timestamp: str) -> bool:
    nc_path = elevation_netcdf_path(site, elevation, elevation_timestamp)
    ar2v_path = elevation_ar2v_path(site, elevation, elevation_timestamp)
    return nc_path.exists() or ar2v_path.exists()


def local_scan_elevations_complete(
    site: str,
    required_elevations: list[tuple[str, str]],
) -> bool:
    for elevation, elevation_timestamp in required_elevations:
        if not local_elevation_complete(site, elevation, elevation_timestamp):
            return False
    return True


def prune_elevation_artifacts(site: str, elevation: str):
    _elevation_store().prune_elevation_artifacts(site, elevation)


def _parse_manifest_timestamp(timestamp: str | None):
    if not timestamp:
        return None
    return parse_nexrad_timestamp(timestamp)


def _volume_sort_key(volume_timestamp: str | None, volume_id: str):
    parsed = _parse_manifest_timestamp(volume_timestamp)
    return (parsed is not None, parsed or volume_timestamp or "", str(volume_id))


def _elevation_sort_key(elevation: str):
    try:
        return (0, float(elevation))
    except (TypeError, ValueError):
        return (1, str(elevation))


def _site_manifest_candidate_dirs(site_dir: Path):
    for child in site_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name == ".runtime":
            continue
        if SCAN_TIMESTAMP_RE.fullmatch(child.name):
            continue
        try:
            float(child.name)
        except (TypeError, ValueError):
            continue
        yield child


def prune_stale_site_manifests(base_dir: Path | None = None, *, max_age_hours: float | None = None) -> int:
    # Resolved in the body, not as a default expression: a default binds at
    # import time, and this module is imported before --config-dir is known.
    if max_age_hours is None:
        max_age_hours = nexrad_config.stale_manifest_max_age_hours()
    if base_dir:
        fs.initialize_filesystem(base_dir)
    root = fs.NEXRAD_LEVEL2_DIR
    now = datetime.now(UTC)
    removed = 0
    runtime_root = root / '.runtime'
    if not root.exists():
        return 0
    for site_dir in root.iterdir():
        if not site_dir.is_dir() or not site_dir.name.startswith('K'):
            continue
        manifest = site_dir / 'manifest.json'
        if not manifest.exists():
            continue
        runtime_dir = runtime_root / site_dir.name
        if runtime_dir.exists() and any(runtime_dir.glob('*.json')):
            continue
        try:
            payload = json.loads(manifest.read_text(encoding='utf-8'))
        except Exception:
            continue
        volumes = payload.get('volumes') or []
        if not volumes:
            continue
        ts = volumes[0].get('volume_timestamp') or volumes[0].get('scan_timestamp')
        parsed = parse_nexrad_timestamp(ts)
        if parsed is None:
            continue
        age_hours = (now - parsed).total_seconds() / 3600
        if age_hours < max_age_hours:
            continue
        manifest.unlink(missing_ok=True)
        removed += 1
    return removed


def _load_elevation_manifest_payload(manifest_path: Path) -> dict | None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _normalize_sidecar_payload(
    payload: dict,
    *,
    fallback_volume_id: str | None = None,
    fallback_volume_timestamp: str | None = None,
    fallback_download_started_at: str | None = None,
) -> dict | None:
    volume_id = payload.get("volume_id") or fallback_volume_id
    if not volume_id:
        return None

    volume_timestamp = payload.get("volume_timestamp") or payload.get("scan_timestamp")
    if fallback_volume_id is not None and str(volume_id) == str(fallback_volume_id) and fallback_volume_timestamp:
        volume_timestamp = volume_timestamp or fallback_volume_timestamp

    elevation = payload.get("elevation")
    return {
        "site": str(payload.get("site") or "").upper(),
        "volume_id": str(volume_id),
        "volume_timestamp": volume_timestamp,
        "scan_timestamp": payload.get("scan_timestamp") or volume_timestamp,
        "download_started_at": payload.get("download_started_at") or fallback_download_started_at,
        "elevation": str(elevation) if elevation is not None else None,
        "elevation_timestamp": payload.get("elevation_timestamp"),
        "first_sweep_index": payload.get("first_sweep_index"),
        "last_sweep_index": payload.get("last_sweep_index"),
        "first_sweep_timestamp": payload.get("first_sweep_timestamp"),
        "last_sweep_timestamp": payload.get("last_sweep_timestamp"),
        "member_group_names": list(payload.get("member_group_names") or []),
        "member_sweeps": list(payload.get("member_sweeps") or []),
        "waveforms_present": list(payload.get("waveforms_present") or []),
        "supplemental": bool(payload.get("supplemental", False)),
        "file_written_at": payload.get("file_written_at"),
        "netcdf_path": payload.get("netcdf_path"),
        "ar2v_path": payload.get("ar2v_path"),
    }


def _raw_sweep_sort_key(sweep: dict):
    sweep_index = sweep.get("sweep_index")
    try:
        normalized_index = int(sweep_index)
    except (TypeError, ValueError):
        normalized_index = float("inf")
    return (
        normalized_index,
        sweep.get("timestamp") or "",
        sweep.get("group_name") or "",
    )


def _raw_sweep_identity(sweep: dict):
    return (
        sweep.get("group_name"),
        sweep.get("sweep_index"),
        sweep.get("timestamp"),
    )


def _raw_sweep_elevation(member_sweep: dict, grouped_elevation: str | None):
    if member_sweep.get("fixed_angle") is not None:
        return member_sweep.get("fixed_angle")
    return grouped_elevation


def build_site_manifest(
    site: str,
    *,
    current_volume_id: str | None = None,
    current_volume_timestamp: str | None = None,
    current_download_started_at: str | None = None,
) -> dict:
    site_upper = str(site).upper()
    site_dir = fs.NEXRAD_LEVEL2_DIR / site_upper
    volumes: dict[str, dict] = {}

    if site_dir.exists():
        for elev_dir in _site_manifest_candidate_dirs(site_dir):
            for manifest_file in sorted(elev_dir.glob("*.json"), reverse=True):
                payload = _load_elevation_manifest_payload(manifest_file)
                if payload is None:
                    continue
                normalized = _normalize_sidecar_payload(
                    payload,
                    fallback_volume_id=current_volume_id,
                    fallback_volume_timestamp=current_volume_timestamp,
                    fallback_download_started_at=current_download_started_at,
                )
                if normalized is None:
                    continue

                volume_id = normalized["volume_id"]
                volume = volumes.setdefault(
                    volume_id,
                    {
                        "site": site_upper,
                        "volume_id": volume_id,
                        "volume_timestamp": normalized["volume_timestamp"],
                        "scan_timestamp": normalized["scan_timestamp"],
                        "download_started_at": normalized["download_started_at"],
                        "sweeps": [],
                        "_seen_sweeps": set(),
                    },
                )

                volume_timestamp = normalized["volume_timestamp"]
                if _volume_sort_key(volume_timestamp, volume_id) > _volume_sort_key(volume.get("volume_timestamp"), volume_id):
                    volume["volume_timestamp"] = volume_timestamp
                if volume.get("scan_timestamp") is None and normalized["scan_timestamp"] is not None:
                    volume["scan_timestamp"] = normalized["scan_timestamp"]
                if volume.get("download_started_at") is None and normalized["download_started_at"] is not None:
                    volume["download_started_at"] = normalized["download_started_at"]

                if normalized["elevation"] is None:
                    continue

                for member_sweep in normalized.get("member_sweeps") or []:
                    if not isinstance(member_sweep, dict):
                        continue
                    raw_sweep = {
                        "sweep_index": member_sweep.get("sweep_index"),
                        "group_name": member_sweep.get("group_name"),
                        "elevation": _raw_sweep_elevation(member_sweep, normalized["elevation"]),
                        "timestamp": member_sweep.get("timestamp"),
                        "waveform": member_sweep.get("waveform"),
                    }
                    identity = _raw_sweep_identity(raw_sweep)
                    if identity in volume["_seen_sweeps"]:
                        continue
                    volume["_seen_sweeps"].add(identity)
                    volume["sweeps"].append(raw_sweep)

    ordered_volumes = sorted(
        volumes.values(),
        key=lambda volume: _volume_sort_key(volume.get("volume_timestamp"), volume.get("volume_id", "")),
        reverse=True,
    )[:nexrad_config.elevation_dirs_to_keep()]

    for volume in ordered_volumes:
        volume["sweeps"].sort(key=_raw_sweep_sort_key)
        volume.pop("_seen_sweeps", None)

    return {
        "site": site_upper,
        "volumes": ordered_volumes,
    }


def write_site_manifest(
    site: str,
    *,
    current_volume_id: str | None = None,
    current_volume_timestamp: str | None = None,
    current_download_started_at: str | None = None,
) -> Path:
    path = site_manifest_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_site_manifest(
        site,
        current_volume_id=current_volume_id,
        current_volume_timestamp=current_volume_timestamp,
        current_download_started_at=current_download_started_at,
    )
    return _write_text_if_changed(path, json.dumps(payload, separators=(",", ":")))


def chunk_output_dir(site: str, volume_id: str, chunks) -> Path:
    return NexradLocalChunkStore().chunk_output_dir(site, volume_id, chunks)


def volume_output_path(site: str, volume_id: str, chunks) -> Path:
    return NexradLocalChunkStore().volume_output_path(site, volume_id, chunks)


def local_volume_file_complete(site: str, volume_id: str, chunks) -> bool:
    return NexradLocalChunkStore().local_volume_file_complete(site, volume_id, chunks)


def prune_station_scan_dirs(site: str, keep_timestamp: str):
    NexradLocalChunkStore().prune_station_scan_dirs(site, keep_timestamp)


def _sanitize_attr_value(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, bytes, int, float)):
        return value
    if hasattr(value, "item"):
        try:
            return _sanitize_attr_value(value.item())
        except Exception:
            pass
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value)
    return str(value)


def _sanitize_attrs(attrs: dict):
    sanitized = {}
    for key, value in (attrs or {}).items():
        sanitized_value = _sanitize_attr_value(value)
        if sanitized_value is not None:
            sanitized[key] = sanitized_value
    return sanitized


def _sanitize_dataset(dataset):
    sanitized = dataset.copy(deep=False)
    sanitized.attrs = _sanitize_attrs(dataset.attrs)
    for variable_name in sanitized.variables:
        sanitized[variable_name].attrs = _sanitize_attrs(sanitized[variable_name].attrs)
    return sanitized


def _slim_dataset(dataset):
    keep_vars = [name for name in dataset.data_vars if name in IMPORTANT_DATA_VARS]
    slim = dataset[keep_vars] if keep_vars else dataset.drop_vars(list(dataset.data_vars))
    slim = slim.copy(deep=False)
    slim.attrs = {}
    for variable_name in slim.variables:
        slim[variable_name].attrs = {}
    return slim


def _slim_dataset_from_node(node):
    dataset_view = node.ds if hasattr(node, "ds") else node.to_dataset()
    if IMPORTANT_DATA_VARS is None:
        slim = dataset_view
    else:
        keep_vars = [name for name in dataset_view.variables if name in IMPORTANT_DATA_VARS]
        slim = dataset_view[keep_vars] if keep_vars else dataset_view.drop_vars(list(dataset_view.data_vars))
    slim = slim.copy(deep=False)
    slim.attrs = {}
    for variable_name in slim.variables:
        slim[variable_name].attrs = {}
    return slim


def _default_fill_value(dtype):
    import numpy as np

    dtype = np.dtype(dtype)
    if dtype.kind == "u":
        return np.iinfo(dtype).max
    if dtype.kind == "i":
        return np.iinfo(dtype).min
    if dtype.kind == "f":
        return np.nan
    return None


def _build_variable_encoding(data_array):
    encoding = {}
    source_encoding = data_array.encoding or {}

    for key in ("dtype", "scale_factor", "add_offset", "_FillValue"):
        if key in source_encoding:
            encoding[key] = source_encoding[key]

    target_dtype = encoding.get("dtype")
    if target_dtype is not None and encoding.get("_FillValue") is None:
        default_fill = _default_fill_value(target_dtype)
        if default_fill is not None:
            encoding["_FillValue"] = default_fill

    return encoding


def _dataset_encoding(dataset):
    return {
        variable_name: _build_variable_encoding(dataset[variable_name])
        for variable_name in dataset.data_vars
    }


def _empty_root_dataset(attrs: dict):
    import xarray as xr

    return xr.Dataset(attrs=_sanitize_attrs(attrs))


def _write_grouped_netcdf(path: Path, root_attrs: dict, datatree, group_names: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    _empty_root_dataset(root_attrs).to_netcdf(path)
    for group_name in group_names:
        dataset = _sanitize_dataset(_slim_dataset_from_node(datatree[group_name]))
        dataset.to_netcdf(
            path,
            mode="a",
            group=group_name.lstrip("/"),
            encoding=_dataset_encoding(dataset),
        )


def write_outputs(probe, parsed_volume, classified_sweeps, chunks_downloaded, *, base_dir=None):
    if base_dir:
        fs.initialize_filesystem(base_dir)

    low_groups = [sweep.group_name for sweep in classified_sweeps if sweep.bucket == "low"]
    high_groups = [sweep.group_name for sweep in classified_sweeps if sweep.bucket == "high"]

    stem = f"{probe.site}_{probe.volume_id}"
    low_path = fs.NEXRAD_LEVEL2_LOW_DIR / f"{stem}_low.nc"
    high_path = fs.NEXRAD_LEVEL2_HIGH_DIR / f"{stem}_high.nc"
    manifest_path = fs.NEXRAD_LEVEL2_MANIFEST_DIR / f"{stem}.json"

    root_attrs = {
        "site": probe.site,
        "volume_id": probe.volume_id,
        "scan_name": parsed_volume.scan_name,
        "vcp": probe.vcp,
        "dynamic_scan_type": parsed_volume.dynamic_scan_type,
        "source_bucket": parsed_volume.source_bucket,
        "chunks_downloaded": chunks_downloaded,
    }

    if parsed_volume.datatree is not None:
        if low_groups:
            _write_grouped_netcdf(low_path, root_attrs, parsed_volume.datatree, low_groups)
        if high_groups:
            _write_grouped_netcdf(high_path, root_attrs, parsed_volume.datatree, high_groups)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_payload = {
        "site": probe.site,
        "volume_id": probe.volume_id,
        "vcp": probe.vcp,
        "scan_name": parsed_volume.scan_name,
        "dynamic_scan_type": parsed_volume.dynamic_scan_type,
        "chunks_downloaded": chunks_downloaded,
        "low_path": str(low_path) if low_groups else None,
        "high_path": str(high_path) if high_groups else None,
        "sweeps": [asdict(sweep) for sweep in classified_sweeps],
    }
    _write_text_if_changed(manifest_path, json.dumps(manifest_payload, separators=(",", ":")))
    return low_path if low_groups else None, high_path if high_groups else None, manifest_path


def _write_elevation_netcdf(
    path: Path,
    root_attrs: dict,
    datatree,
    group_names: list[str],
) -> Path:
    """Write a single elevation NetCDF with all member sweep groups."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _empty_root_dataset(root_attrs).to_netcdf(path)
    for group_name in group_names:
        dataset = _sanitize_dataset(_slim_dataset_from_node(datatree[group_name]))
        dataset.to_netcdf(
            path,
            mode="a",
            group=group_name.lstrip("/"),
            encoding=_dataset_encoding(dataset),
        )
    return path


def _write_elevation_manifest(path: Path, artifact: ElevationArtifact) -> Path:
    """Write a per-elevation JSON manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "site": artifact.site,
        "volume_id": artifact.volume_id,
        "volume_timestamp": artifact.volume_timestamp,
        "scan_timestamp": artifact.scan_timestamp,
        "download_started_at": artifact.download_started_at,
        "elevation": artifact.elevation,
        "elevation_timestamp": artifact.elevation_timestamp,
        "first_sweep_index": artifact.first_sweep_index,
        "last_sweep_index": artifact.last_sweep_index,
        "first_sweep_timestamp": artifact.first_sweep_timestamp,
        "last_sweep_timestamp": artifact.last_sweep_timestamp,
        "member_group_names": artifact.member_group_names,
        "member_sweeps": artifact.member_sweeps,
        "waveforms_present": list(artifact.waveforms_present),
        "supplemental": artifact.supplemental,
        "file_written_at": artifact.file_written_at,
        "netcdf_path": artifact.netcdf_path,
        "ar2v_path": artifact.ar2v_path,
    }
    return _write_text_if_changed(path, json.dumps(payload, separators=(",", ":")))


def _write_elevation_ar2v(path: Path, raw_volume, group_names: list[str]) -> Path:
    """Write a grouped elevation as a raw AR2V payload via streaming writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sweeps_by_group = {sweep.group_name: sweep for sweep in getattr(raw_volume, "sweeps", [])}
    doppler = nexrad_config.doppler_waveform()
    with atomic_output_path(path) as temporary:
        with open(temporary, "wb") as f:
            f.write(raw_volume.volume_header)
            for record in iter_metadata_records(raw_volume):
                f.write(record)
            for group_name in group_names:
                sweep = sweeps_by_group.get(group_name)
                if sweep is None:
                    continue
                # Per sweep, not per record: the waveform is a property of the sweep,
                # so testing it inside the record loop only repeated the work.
                is_doppler = str(getattr(sweep, "waveform", "") or "").strip().lower() == doppler
                for record in iter_sweep_records(raw_volume, sweep):
                    f.write(filter_msg31_blocks(record, DREF_BLOCK) if is_doppler else record)
    return path


def write_elevation_artifacts(
    group: ElevationGroup,
    source,
    *,
    site: str,
    volume_id: str,
    scan_timestamp: str | None,
    download_started_at: str | None,
    elevation_label: str,
    elevation_timestamp: str | None,
    output_root: str | Path | None = None,
) -> list[ElevationArtifact]:
    """Write grouped elevation NetCDF and manifest to the public output tree.

    Returns a list of ElevationArtifact records for the emitted files.
    """
    _ = output_root

    ts_for_filename = elevation_timestamp or scan_timestamp or "unknown"
    store = _elevation_store()

    nc_path = None
    ar2v_path = None
    manifest_path = store.elevation_manifest_path(site, elevation_label, ts_for_filename)

    root_attrs = {
        "site": site,
        "volume_id": volume_id,
        "scan_timestamp": scan_timestamp,
        "elevation": elevation_label,
        "elevation_timestamp": elevation_timestamp,
        "first_sweep_index": group.first_sweep_index,
        "last_sweep_index": group.last_sweep_index,
        "supplemental": group.supplemental,
    }

    group_names = [m.group_name for m in group.members]
    member_sweeps = [
        {
            "group_name": member.group_name,
            "sweep_index": member.index,
            "fixed_angle": member.fixed_angle,
            "elevation_number": member.elevation_number,
            "waveform": member.waveform,
            "timestamp": member.timestamp,
        }
        for member in group.members
    ]
    if hasattr(source, "volume_header") and hasattr(source, "record_buffer"):
        ar2v_path = store.elevation_ar2v_path(site, elevation_label, ts_for_filename)
        _write_elevation_ar2v(ar2v_path, source, group_names)
    else:
        nc_path = store.elevation_netcdf_path(site, elevation_label, ts_for_filename)
        _write_elevation_netcdf(nc_path, root_attrs, source, group_names)
    file_written_at = _utc_now_timestamp()

    artifact = ElevationArtifact(
        site=site,
        volume_id=volume_id,
        volume_timestamp=scan_timestamp,
        scan_timestamp=scan_timestamp,
        elevation=elevation_label,
        elevation_timestamp=elevation_timestamp,
        first_sweep_index=group.first_sweep_index,
        last_sweep_index=group.last_sweep_index,
        first_sweep_timestamp=group.first_timestamp,
        last_sweep_timestamp=group.last_timestamp,
        member_group_names=group_names,
        member_sweeps=member_sweeps,
        waveforms_present=group.waveforms_present,
        supplemental=group.supplemental,
        download_started_at=download_started_at,
        file_written_at=file_written_at,
        netcdf_path=str(nc_path) if nc_path is not None else None,
        ar2v_path=str(ar2v_path) if ar2v_path is not None else None,
    )
    _write_elevation_manifest(manifest_path, artifact)

    store.prune_elevation_artifacts(site, elevation_label)

    return [artifact]
