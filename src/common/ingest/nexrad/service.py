import asyncio
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import subprocess
import time
from pathlib import Path
import shutil
import uuid

import util.file as fs
from common.ingest.nexrad import config as nexrad_config
from common.ingest.nexrad.config import allowed_vcps, chunks_bucket, format_perf_ms
from common.ingest.nexrad.models import (
    ElevationArtifact,
    NexradIngestResult,
    ScanStreamState,
    WorkerParseResult,
)
from common.ingest.nexrad.s3_async import (
    async_get_chunk_bytes,
    async_list_recent_volume_ids,
    async_list_volume_chunks,
    get_unsigned_s3_client_async,
)
from common.ingest.nexrad.s3_chunks import (
    extract_volume_timestamp,
    format_nexrad_timestamp,
    get_chunk_bytes,
    get_unsigned_s3_client,
    list_recent_volume_ids,
    list_volume_chunks,
    parse_nexrad_timestamp,
)
from common.ingest.nexrad.stream import (
    MAX_MAGIC_OVERLAP,
    detect_next_volume_offset,
    split_at_boundary,
)
from common.ingest.nexrad.grouping import ingest_readiness_elevation_ids
from common.ingest.nexrad.parser import normalize_chunk_payload
from common.ingest.nexrad.vcp_probe import probe_volume_vcp
from common.ingest.nexrad.weather_api import fetch_radar_station_vcps
from common.ingest.nexrad.worker_pool import get_nexrad_pool, record_volume_and_maybe_recycle, shutdown_nexrad_pool
from common.ingest.nexrad.writer import (
    NexradLocalChunkStore,
    NexradElevationStore,
    prune_runtime_volumes,
    prune_stale_site_manifests,
    runtime_scan_path,
    local_scan_elevations_complete,
    write_site_manifest,
)
from util.atomic import atomic_write_text
from util.io import IOManager

io_manager = IOManager("[NEXRAD]")


def _new_volume_perf_totals() -> dict[str, object]:
    return {
        "parse_runs": 0,
        "saved_sweeps": 0,
        "saved_elevations": 0,
        "max_visible_sweeps": 0,
        "max_child_rss_kb": None,
        "queued_ms": 0.0,
        "execute_ms": 0.0,
        "buffer_trimmed": False,
        "first_useful_export": None,
        "chunk_list_ms": None,
        "chunk_count": 0,
    }


def _record_worker_parse_totals(
    totals: dict[str, object] | None,
    *,
    result: WorkerParseResult,
    queued_elapsed_ms: float,
    execute_elapsed_ms: float,
    first_elevation_timestamp: str | None,
    had_first_elevation: bool,
) -> None:
    if totals is None or not result.saved_sweep_count:
        return

    totals["parse_runs"] = int(totals["parse_runs"]) + 1
    totals["saved_sweeps"] = int(totals["saved_sweeps"]) + int(result.saved_sweep_count)
    totals["saved_elevations"] = int(totals["saved_elevations"]) + len(result.saved_elevations)
    totals["max_visible_sweeps"] = max(int(totals["max_visible_sweeps"]), int(result.visible_sweeps))
    totals["queued_ms"] = float(totals["queued_ms"]) + float(queued_elapsed_ms)
    totals["execute_ms"] = float(totals["execute_ms"]) + float(execute_elapsed_ms)
    totals["buffer_trimmed"] = bool(totals["buffer_trimmed"]) or bool(result.buffer_trimmed)

    if result.child_rss_kb is not None:
        current_rss = totals["max_child_rss_kb"]
        totals["max_child_rss_kb"] = (
            float(result.child_rss_kb)
            if current_rss is None
            else max(float(current_rss), float(result.child_rss_kb))
        )

    if not had_first_elevation and first_elevation_timestamp is not None and totals["first_useful_export"] is None:
        totals["first_useful_export"] = first_elevation_timestamp


def _emit_volume_perf_summary(
    site: str,
    volume_id: str,
    *,
    totals: dict[str, object],
    total_elapsed_ms: float | None = None,
    complete: bool | None = None,
    chunks_downloaded: int | None = None,
) -> None:
    saved_sweeps = int(totals["saved_sweeps"])
    if saved_sweeps:
        rss_kb = totals["max_child_rss_kb"]
        rss_text = "n/a" if rss_kb is None else f"{float(rss_kb):.0f}"
        io_manager.write_info(
            f"[VOL {site}/{volume_id}] exported {saved_sweeps} sweeps across "
            f"{int(totals['saved_elevations'])} elevations "
            f"(parse_runs={int(totals['parse_runs'])}, visible={int(totals['max_visible_sweeps'])}, rss_kb={rss_text})"
        )

    parts = []
    if totals["chunk_list_ms"] is not None:
        parts.append(
            f"chunk_list={float(totals['chunk_list_ms']):.2f}ms (listed={int(totals['chunk_count'])})"
        )
    if int(totals["parse_runs"]):
        parts.append(
            f"worker_parse=queued:{float(totals['queued_ms']):.2f}ms "
            f"execute:{float(totals['execute_ms']):.2f}ms"
        )
        parts.append(f"trimmed={bool(totals['buffer_trimmed'])}")
    if totals["first_useful_export"] is not None:
        parts.append(f"first_useful_export={totals['first_useful_export']}")
    if total_elapsed_ms is not None:
        parts.append(
            f"total_async_ingest={float(total_elapsed_ms):.2f}ms "
            f"(accepted=True, complete={bool(complete)}, chunks_downloaded={int(chunks_downloaded or 0)})"
        )
    if parts:
        io_manager.write_perf(f"[VOL {site}/{volume_id}] " + " | ".join(parts))


def _write_text_if_changed(path: Path, content: str) -> None:
    """Persist scan state through a temp file + atomic replace.

    The state file survives process restarts and staleness checks, so a crash
    mid write must never expose truncated JSON under the real name.
    """
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except Exception:
            pass
    atomic_write_text(path, content)


def _utc_now_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nexrad_worker_timeout_seconds() -> float:
    # The clamp guards a hand-set environment variable; the schema pins the
    # configured value at 1 or more, so the catalog cannot be silently rewritten.
    return max(1.0, float(nexrad_config.worker_timeout_seconds()))

def _artifact_group_key(artifact: ElevationArtifact) -> str:
    return f"{artifact.elevation}:{','.join(artifact.member_group_names)}"


def _normalize_seen_elevation_exports(value: object, legacy_keys: object = None) -> dict[str, str | None]:
    normalized: dict[str, str | None] = {}
    if isinstance(value, dict):
        for key, timestamp in value.items():
            normalized[str(key)] = str(timestamp) if timestamp else None
        return normalized
    if isinstance(value, list):
        for key in value:
            normalized[str(key)] = None
        return normalized
    if isinstance(legacy_keys, list):
        for key in legacy_keys:
            normalized[str(key)] = None
    return normalized


def _normalize_elevation_timestamps(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for elevation, timestamp in value.items():
        if timestamp:
            normalized[str(elevation)] = str(timestamp)
    return normalized


def _timestamp_sort_key(value: str | None) -> tuple[int, str]:
    if not value:
        return (0, "")
    parsed = parse_nexrad_timestamp(value)
    if parsed is not None:
        normalized = format_nexrad_timestamp(parsed)
        if normalized:
            return (1, normalized)
    return (0, str(value))


def _timestamp_is_newer(candidate: str | None, current: str | None) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    return _timestamp_sort_key(candidate) > _timestamp_sort_key(current)


def _required_elevation_paths_complete(site: str, elevation_timestamps: dict[str, str]) -> bool:
    required_elevations: list[tuple[str, str]] = []
    for elevation in ingest_readiness_elevation_ids():
        timestamp = elevation_timestamps.get(elevation)
        if not timestamp:
            return False
        required_elevations.append((elevation, timestamp))
    return local_scan_elevations_complete(site, required_elevations)


class NexradIngestService:
    def __init__(
        self,
        *,
        chunk_lister=None,
        chunk_fetcher=None,
        volume_lister=None,
        station_fetcher=None,
        volume_prober=None,
        async_chunk_lister=None,
        async_chunk_fetcher=None,
        async_volume_lister=None,
        max_site_tasks=None,
        max_chunk_downloads=None,
        parse_checkpoint_chunk_interval=None,
        in_volume_prefetch=None,
    ):
        if max_site_tasks is None:
            max_site_tasks = nexrad_config.max_site_tasks()
        if max_chunk_downloads is None:
            max_chunk_downloads = nexrad_config.max_chunk_downloads()
        if parse_checkpoint_chunk_interval is None:
            parse_checkpoint_chunk_interval = nexrad_config.parse_checkpoint_chunk_interval()
        if in_volume_prefetch is None:
            in_volume_prefetch = nexrad_config.in_volume_prefetch()
        self.chunk_lister = chunk_lister or list_volume_chunks
        self.chunk_fetcher = chunk_fetcher or get_chunk_bytes
        self.volume_lister = volume_lister or list_recent_volume_ids
        self.station_fetcher = station_fetcher or fetch_radar_station_vcps
        self.volume_prober = volume_prober or probe_volume_vcp
        self.async_chunk_lister = async_chunk_lister or async_list_volume_chunks
        self.async_chunk_fetcher = async_chunk_fetcher or async_get_chunk_bytes
        self.async_volume_lister = async_volume_lister or async_list_recent_volume_ids
        self.max_site_tasks = max_site_tasks
        self.max_chunk_downloads = max_chunk_downloads
        self.parse_checkpoint_chunk_interval = max(1, int(parse_checkpoint_chunk_interval))
        self.in_volume_prefetch = max(1, int(in_volume_prefetch))
        self._stream_chunk_downloads = async_chunk_fetcher is None
        self._shared_chunk_download_semaphore = None
        self.local_chunk_store = NexradLocalChunkStore()
        self.elevation_store = NexradElevationStore()

    def _runtime_state_path(self, site: str, volume_id: str) -> Path:
        runtime_dir = self.elevation_store.runtime_dir(site)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir / f"{str(site).upper()}_{volume_id}.json"

    @staticmethod
    def _chunk_identity(chunk) -> str:
        return f"{chunk.chunk_number:03d}-{chunk.chunk_type}:{chunk.key}"

    def _load_runtime_state(self, site: str, volume_id: str, runtime_path: Path) -> dict:
        if not runtime_path.exists():
            return {}

        state_path = self._runtime_state_path(site, volume_id)
        if not state_path.exists():
            runtime_path.write_bytes(b"")
            return {}

        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            runtime_path.write_bytes(b"")
            return {}

        if not isinstance(data, dict):
            data = {}
        committed_size = data.get("bytes_written", 0)
        if not isinstance(committed_size, int) or not 0 <= committed_size <= runtime_path.stat().st_size:
            data, committed_size = {}, 0
        if "bytes_written" not in data:
            data = {}
        # Roll back an incomplete pass before replaying its uncommitted chunks.
        with runtime_path.open("r+b") as handle:
            handle.truncate(committed_size)
        return data

    def _save_runtime_state(
        self,
        site: str,
        volume_id: str,
        *,
        downloaded_chunk_keys: set[str],
        seen_elevation_exports: dict[str, str | None],
        elevation_timestamps_by_id: dict[str, str],
        first_elevation_timestamp: str | None,
        scan_timestamp: str | None,
        download_started_at: str | None,
        bytes_written: int = 0,
    ) -> None:
        state_path = self._runtime_state_path(site, volume_id)
        payload = {
            "site": str(site).upper(),
            "volume_id": str(volume_id),
            "scan_timestamp": scan_timestamp,
            "download_started_at": download_started_at,
            "downloaded_chunk_keys": sorted(downloaded_chunk_keys),
            "bytes_written": bytes_written,
            "seen_elevation_exports": seen_elevation_exports,
            "seen_elevation_keys": sorted(seen_elevation_exports),
            "elevation_timestamps_by_id": elevation_timestamps_by_id,
            "first_elevation_timestamp": first_elevation_timestamp,
        }
        _write_text_if_changed(state_path, json.dumps(payload, separators=(",", ":")))

    def _clear_runtime_state(self, site: str, volume_id: str) -> None:
        self._runtime_state_path(site, volume_id).unlink(missing_ok=True)

    @staticmethod
    def _read_runtime_tail(runtime_path: Path) -> bytes:
        if not runtime_path.exists():
            return b""
        with runtime_path.open("rb") as handle:
            handle.seek(max(0, runtime_path.stat().st_size - MAX_MAGIC_OVERLAP))
            return handle.read()

    @staticmethod
    def _normalize_runtime_chunk(payload: bytes, *, first_chunk_of_volume: bool) -> bytes:
        if not payload:
            return b""
        return normalize_chunk_payload(payload, first_chunk_of_volume=first_chunk_of_volume)

    @staticmethod
    def _volume_timestamp(volume_id: str, chunks) -> str:
        return extract_volume_timestamp(volume_id, chunks)

    def _chunk_output_dir(self, site: str, volume_id: str, chunks) -> Path:
        return self.local_chunk_store.chunk_output_dir(site, volume_id, chunks)

    def _volume_output_path(self, site: str, volume_id: str, chunks) -> Path:
        return self.local_chunk_store.volume_output_path(site, volume_id, chunks)

    @staticmethod
    def _remove_chunk_dir(outdir: Path):
        if outdir.exists():
            shutil.rmtree(outdir, ignore_errors=True)

    def _download_chunks_to_site_dir(self, site: str, volume_id: str, chunks, *, s3_client):
        outdir = self._chunk_output_dir(site, volume_id, chunks)
        volume_path = self._volume_output_path(site, volume_id, chunks)
        if self.local_chunk_store.local_volume_file_complete(site, volume_id, chunks):
            self._remove_chunk_dir(outdir)
            return

        self._remove_chunk_dir(outdir)
        volume_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = volume_path.with_suffix(f"{volume_path.suffix}.part")
        if temp_path.exists():
            temp_path.unlink()

        with temp_path.open("wb") as volume_file:
            for chunk in chunks:
                volume_file.write(self.chunk_fetcher(chunk, s3_client=s3_client))

        temp_path.replace(volume_path)
        self.local_chunk_store.prune_station_scan_dirs(site, outdir.parent.name)

    async def _download_chunks_to_site_dir_async(self, site: str, volume_id: str, chunks, *, s3_client, chunk_download_semaphore=None):
        import aiofiles

        started_at = time.perf_counter()
        outdir = self._chunk_output_dir(site, volume_id, chunks)
        volume_path = self._volume_output_path(site, volume_id, chunks)
        if self.local_chunk_store.local_volume_file_complete(site, volume_id, chunks):
            self._remove_chunk_dir(outdir)
            return

        self._remove_chunk_dir(outdir)
        volume_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = volume_path.with_suffix(f"{volume_path.suffix}.part")
        if temp_path.exists():
            temp_path.unlink()
        semaphore = chunk_download_semaphore or asyncio.Semaphore(self.max_chunk_downloads)

        async with aiofiles.open(temp_path, "wb") as file_obj:
            for chunk in chunks:
                async with semaphore:
                    if self._stream_chunk_downloads:
                        response = await s3_client.get_object(Bucket=chunks_bucket(), Key=chunk.key)
                        body = response["Body"]
                        async for data in body.iter_chunks():
                            await file_obj.write(data)
                        if hasattr(body, "close"):
                            maybe_close = body.close()
                            if asyncio.iscoroutine(maybe_close):
                                await maybe_close
                    else:
                        payload = await self.async_chunk_fetcher(chunk, s3_client=s3_client)
                        if isinstance(payload, (bytes, bytearray)):
                            await file_obj.write(payload)
                        else:
                            body = payload["Body"] if isinstance(payload, dict) else payload
                            if hasattr(body, "iter_chunks"):
                                async for data in body.iter_chunks():
                                    await file_obj.write(data)
                                if hasattr(body, "close"):
                                    maybe_close = body.close()
                                    if asyncio.iscoroutine(maybe_close):
                                        await maybe_close
                            else:
                                data = await body.read()
                                await file_obj.write(data)

        temp_path.replace(volume_path)
        elapsed_ms = format_perf_ms(started_at)
        io_manager.write_perf(
            f"[VOL {str(site).upper()}/{volume_id}] async_chunk_download: {elapsed_ms:.2f}ms "
            f"(chunks={len(chunks)})"
        )

    def _stream_ingest_volume(self, site, volume_id, chunks, **kwargs):
        from util.runtime.handoff import _AdvisoryFileLock
        from util.runtime.config import section
        lock = _AdvisoryFileLock(self.elevation_store.runtime_dir(site) / ".locks" / "site.lock")
        lock.acquire(timeout=section("handoff")["input_lock_timeout_seconds"])
        try:
            return self._stream_ingest_volume_locked(site, volume_id, chunks, **kwargs)
        finally:
            lock.release()

    def _stream_ingest_volume_locked(
        self,
        site: str,
        volume_id: str,
        chunks,
        *,
        s3_client,
        base_dir=None,
    ):
        """Overlap-safe stream ingest: fetches chunks, detects boundaries, runs worker parse."""
        if base_dir:
            fs.initialize_filesystem(base_dir)

        scan_timestamp = self._volume_timestamp(volume_id, chunks)
        site_upper = str(site).upper()
        sorted_chunks = sorted(chunks, key=lambda c: (c.chunk_number, c.chunk_type))

        runtime_path = runtime_scan_path(site_upper, volume_id)
        prune_runtime_volumes(site_upper, volume_id, assume_locked=True)
        persisted_state = self._load_runtime_state(site_upper, volume_id, runtime_path)
        downloaded_chunk_keys = set(persisted_state.get("downloaded_chunk_keys", []))
        download_started_at = persisted_state.get("download_started_at") or _utc_now_timestamp()
        pending_chunks = [
            chunk for chunk in sorted_chunks
            if self._chunk_identity(chunk) not in downloaded_chunk_keys
        ]
        existing_size = runtime_path.stat().st_size if runtime_path.exists() else 0
        current_state = ScanStreamState(
            index=0,
            volume_id=volume_id,
            scan_timestamp=scan_timestamp,
            file_path=str(runtime_path),
            bytes_written=existing_size,
        )

        previous_tail = self._read_runtime_tail(runtime_path)
        stream_has_started = existing_size > 0
        chunks_since_parse = 0
        parsed_since_last_write = True
        seen_elevation_exports = _normalize_seen_elevation_exports(
            persisted_state.get("seen_elevation_exports"),
            persisted_state.get("seen_elevation_keys"),
        )
        elevation_timestamps_by_id = _normalize_elevation_timestamps(persisted_state.get("elevation_timestamps_by_id"))
        first_elevation_timestamp: str | None = persisted_state.get("first_elevation_timestamp")
        perf_totals = _new_volume_perf_totals()

        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        with open(runtime_path, "ab" if existing_size > 0 else "wb") as scan_file:
            for chunk in pending_chunks:
                payload = self.chunk_fetcher(chunk, s3_client=s3_client)
                if not payload:
                    continue

                downloaded_chunk_keys.add(self._chunk_identity(chunk))

                found_boundary, boundary_offset = detect_next_volume_offset(
                    previous_tail, payload, stream_has_started, chunk_type=chunk.chunk_type,
                )

                if found_boundary and boundary_offset >= 0:
                    before, after = split_at_boundary(payload, boundary_offset)
                    del payload
                    if before:
                        normalized_before = self._normalize_runtime_chunk(
                            before,
                            first_chunk_of_volume=current_state.bytes_written == 0,
                        )
                        scan_file.write(normalized_before)
                        current_state.bytes_written += len(normalized_before)
                        parsed_since_last_write = False
                        del normalized_before
                    del before

                    scan_file.flush()

                    current_state.finalized = True
                    prior_bytes_written = current_state.bytes_written
                    first_elevation_timestamp = self._run_worker_parse(
                        current_state,
                        site_upper,
                        volume_id,
                        scan_timestamp,
                        seen_elevation_exports,
                        first_elevation_timestamp,
                        download_started_at=download_started_at,
                        elevation_timestamps_by_id=elevation_timestamps_by_id,
                        perf_totals=perf_totals,
                        base_dir=base_dir,
                    )
                    seen_elevation_exports = {}
                    elevation_timestamps_by_id = {}
                    first_elevation_timestamp = None
                    parsed_since_last_write = True
                    chunks_since_parse = 0

                    current_state = ScanStreamState(
                        index=current_state.index + 1,
                        volume_id=volume_id,
                        scan_timestamp=scan_timestamp,
                        file_path=str(runtime_path),
                    )
                    scan_file.seek(0)
                    scan_file.truncate()

                    if after:
                        normalized_after = self._normalize_runtime_chunk(
                            after,
                            first_chunk_of_volume=True,
                        )
                        scan_file.write(normalized_after)
                        current_state.bytes_written = len(normalized_after)
                        parsed_since_last_write = False
                        scan_file.flush()
                        prior_bytes_written = current_state.bytes_written
                        first_elevation_timestamp = self._run_worker_parse(
                            current_state,
                            site_upper,
                            volume_id,
                            scan_timestamp,
                            seen_elevation_exports,
                            first_elevation_timestamp,
                            download_started_at=download_started_at,
                            elevation_timestamps_by_id=elevation_timestamps_by_id,
                            perf_totals=perf_totals,
                            base_dir=base_dir,
                        )
                        stream_has_started = True
                        if current_state.bytes_written != prior_bytes_written:
                            previous_tail = self._read_runtime_tail(runtime_path)
                            scan_file.seek(current_state.bytes_written)
                        else:
                            previous_tail = after[-MAX_MAGIC_OVERLAP:] if len(after) >= MAX_MAGIC_OVERLAP else after
                        parsed_since_last_write = True
                        del normalized_after
                        del after
                    else:
                        stream_has_started = True
                        previous_tail = previous_tail[-MAX_MAGIC_OVERLAP:]
                    
                    continue

                normalized_payload = self._normalize_runtime_chunk(
                    payload,
                    first_chunk_of_volume=current_state.bytes_written == 0,
                )
                scan_file.write(normalized_payload)
                current_state.bytes_written += len(normalized_payload)
                stream_has_started = True
                previous_tail = (previous_tail + payload)[-MAX_MAGIC_OVERLAP:]
                parsed_since_last_write = False
                chunks_since_parse += 1

                if chunks_since_parse >= self.parse_checkpoint_chunk_interval and current_state.bytes_written > 0:
                    scan_file.flush()
                    prior_bytes_written = current_state.bytes_written
                    first_elevation_timestamp = self._run_worker_parse(
                        current_state,
                        site_upper,
                        volume_id,
                        scan_timestamp,
                        seen_elevation_exports,
                        first_elevation_timestamp,
                        download_started_at=download_started_at,
                        elevation_timestamps_by_id=elevation_timestamps_by_id,
                        perf_totals=perf_totals,
                        base_dir=base_dir,
                    )
                    if current_state.bytes_written != prior_bytes_written:
                        previous_tail = self._read_runtime_tail(runtime_path)
                        scan_file.seek(current_state.bytes_written)
                    parsed_since_last_write = True
                    chunks_since_parse = 0
                del normalized_payload
                del payload

            if not parsed_since_last_write and current_state.bytes_written > 0:
                scan_file.flush()
                first_elevation_timestamp = self._run_worker_parse(
                    current_state,
                    site_upper,
                    volume_id,
                    scan_timestamp,
                    seen_elevation_exports,
                    first_elevation_timestamp,
                    download_started_at=download_started_at,
                    elevation_timestamps_by_id=elevation_timestamps_by_id,
                    perf_totals=perf_totals,
                    base_dir=base_dir,
                )

        self.local_chunk_store.prune_station_scan_dirs(site_upper, scan_timestamp)

        complete = _required_elevation_paths_complete(site_upper, elevation_timestamps_by_id)

        if complete:
            write_site_manifest(
                site_upper,
                current_volume_id=volume_id,
                current_volume_timestamp=scan_timestamp,
                current_download_started_at=download_started_at,
            )
            if runtime_path.exists():
                runtime_path.unlink(missing_ok=True)
            self._clear_runtime_state(site_upper, volume_id)
        else:
            self._save_runtime_state(
                site_upper,
                volume_id,
                downloaded_chunk_keys=downloaded_chunk_keys,
                bytes_written=current_state.bytes_written,
                seen_elevation_exports=seen_elevation_exports,
                elevation_timestamps_by_id=elevation_timestamps_by_id,
                first_elevation_timestamp=first_elevation_timestamp,
                scan_timestamp=scan_timestamp,
                download_started_at=download_started_at,
            )

        _emit_volume_perf_summary(site_upper, volume_id, totals=perf_totals)

        return NexradIngestResult(
            site=site_upper,
            volume_id=volume_id,
            vcp=0,
            dynamic_scan_type=None,
            volume_path=None,
            scan_timestamp=scan_timestamp,
            low_path=None,
            high_path=None,
            manifest_path=None,
            chunks_downloaded=len(pending_chunks),
            complete=complete,
        )

    def _run_worker_parse(
        self,
        state: ScanStreamState,
        site: str,
        volume_id: str,
        scan_timestamp: str | None,
        seen_elevation_exports: dict[str, str | None],
        first_elevation_timestamp: str | None,
        *,
        download_started_at: str | None = None,
        elevation_timestamps_by_id: dict[str, str] | None = None,
        perf_totals: dict[str, object] | None = None,
        base_dir=None,
    ) -> str | None:
        """Run the worker parse/export via the shared process pool.

        Returns the first elevation timestamp seen, or the existing value.
        """
        if not state.file_path or not Path(state.file_path).exists():
            return first_elevation_timestamp

        if not isinstance(elevation_timestamps_by_id, dict):
            if first_elevation_timestamp is None:
                first_elevation_timestamp = elevation_timestamps_by_id
            elevation_timestamps_by_id = {}
        had_first_elevation = first_elevation_timestamp is not None

        output_root = fs.NEXRAD_LEVEL2_DIR
        pool = get_nexrad_pool()
        submit_started_at = time.perf_counter()

        snapshot_path = Path(state.file_path).with_name(
            Path(state.file_path).stem + f".snapshot-{uuid.uuid4().hex}.ar2v")
        shutil.copyfile(state.file_path, snapshot_path)
        try:
            future = pool.submit(
                volume_path=str(snapshot_path),
                output_root=str(output_root),
                site=site,
                volume_id=volume_id,
                scan_timestamp=scan_timestamp,
                download_started_at=download_started_at,
                seen_keys=seen_elevation_exports,
                trim_buffer=True,
            )
            queued_elapsed_ms = format_perf_ms(submit_started_at)
            future_wait_started_at = time.perf_counter()
            payload = future.result(timeout=_nexrad_worker_timeout_seconds())
            execute_elapsed_ms = format_perf_ms(future_wait_started_at)
            result = WorkerParseResult(
                visible_sweeps=payload.visible_sweeps,
                saved_sweep_count=payload.saved_sweep_count,
                saved_elevations=payload.saved_elevations,
                parse_error=payload.parse_error,
                child_rss_kb=payload.child_rss_kb,
                buffer_trimmed=payload.buffer_trimmed,
                runtime_size=payload.runtime_size,
            )

            if result.buffer_trimmed and result.runtime_size is not None:
                state.bytes_written = max(0, int(result.runtime_size))
                with snapshot_path.open("rb") as source, Path(state.file_path).open("r+b") as target:
                    shutil.copyfileobj(source, target)
                    target.truncate(state.bytes_written)

            if result.parse_error:
                state.parse_errors.append(result.parse_error)
                io_manager.write_warning(
                    f"[VOL {site}/{volume_id}] worker parse error: {result.parse_error}"
                )

            for artifact in result.saved_elevations:
                artifact_key = _artifact_group_key(artifact)
                artifact_timestamp = artifact.elevation_timestamp or artifact.scan_timestamp
                previous_export_timestamp = seen_elevation_exports.get(artifact_key)
                if _timestamp_is_newer(artifact_timestamp, previous_export_timestamp):
                    seen_elevation_exports[artifact_key] = artifact_timestamp
                else:
                    seen_elevation_exports.setdefault(artifact_key, artifact_timestamp)
                if artifact_timestamp:
                    current_timestamp = elevation_timestamps_by_id.get(artifact.elevation)
                    if _timestamp_is_newer(artifact_timestamp, current_timestamp):
                        elevation_timestamps_by_id[artifact.elevation] = artifact_timestamp
                    else:
                        elevation_timestamps_by_id.setdefault(artifact.elevation, artifact_timestamp)
                    if first_elevation_timestamp is None:
                        first_elevation_timestamp = artifact_timestamp

            _record_worker_parse_totals(
                perf_totals,
                result=result,
                queued_elapsed_ms=queued_elapsed_ms,
                execute_elapsed_ms=execute_elapsed_ms,
                first_elevation_timestamp=first_elevation_timestamp,
                had_first_elevation=had_first_elevation,
            )
        except FuturesTimeoutError:
            future.cancel()
            shutdown_nexrad_pool(wait=False, expected_pool=pool)
            message = (
                f"worker parse timed out after {_nexrad_worker_timeout_seconds():.0f}s; "
                "recycling NEXRAD worker pool"
            )
            state.parse_errors.append(message)
            io_manager.write_warning(f"[VOL {site}/{volume_id}] {message}")
        except BrokenProcessPool as exc:
            shutdown_nexrad_pool(wait=False, expected_pool=pool)
            state.parse_errors.append(str(exc))
            io_manager.write_warning(
                f"[VOL {site}/{volume_id}] worker pool broke; recycled pool: {exc}"
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            message = stderr or stdout or str(exc)
            state.parse_errors.append(message)
            io_manager.write_warning(
                f"[VOL {site}/{volume_id}] worker parse failed: {message}"
            )
        except Exception as exc:
            if "terminated abruptly" in str(exc).lower():
                shutdown_nexrad_pool(wait=False, expected_pool=pool)
            state.parse_errors.append(str(exc))
            io_manager.write_warning(
                f"[VOL {site}/{volume_id}] worker parse failed: {exc}"
            )

        finally:
            snapshot_path.unlink(missing_ok=True)
        return first_elevation_timestamp

    async def _stream_ingest_volume_async(self, site, volume_id, chunks, **kwargs):
        from util.runtime.handoff import _AdvisoryFileLock, InputLockTimeout, _lock_contention
        from util.runtime.config import section
        lock = _AdvisoryFileLock(self.elevation_store.runtime_dir(site) / ".locks" / "site.lock")
        deadline = time.monotonic() + section("handoff")["input_lock_timeout_seconds"]
        while True:
            try:
                lock.acquire()
                break
            except OSError as exc:
                if not _lock_contention(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise InputLockTimeout(f"NEXRAD site {site} is still processing another volume") from exc
                await asyncio.sleep(section("consumers")["core_readiness_seconds"])
        try:
            return await self._stream_ingest_volume_async_locked(site, volume_id, chunks, **kwargs)
        finally:
            lock.release()

    async def _stream_ingest_volume_async_locked(
        self,
        site: str,
        volume_id: str,
        chunks,
        *,
        s3_client,
        base_dir=None,
        chunk_download_semaphore=None,
        perf_totals: dict[str, object] | None = None,
    ):
        """Async variant: streams chunks directly to disk, parses on boundary."""
        if base_dir:
            fs.initialize_filesystem(base_dir)

        scan_timestamp = self._volume_timestamp(volume_id, chunks)
        site_upper = str(site).upper()
        sorted_chunks = sorted(chunks, key=lambda c: (c.chunk_number, c.chunk_type))

        runtime_path = runtime_scan_path(site_upper, volume_id)
        prune_runtime_volumes(site_upper, volume_id, assume_locked=True)
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        persisted_state = self._load_runtime_state(site_upper, volume_id, runtime_path)
        downloaded_chunk_keys = set(persisted_state.get("downloaded_chunk_keys", []))
        download_started_at = persisted_state.get("download_started_at") or _utc_now_timestamp()
        pending_chunks = [
            chunk for chunk in sorted_chunks
            if self._chunk_identity(chunk) not in downloaded_chunk_keys
        ]
        existing_size = runtime_path.stat().st_size if runtime_path.exists() else 0

        current_state = ScanStreamState(
            index=0,
            volume_id=volume_id,
            scan_timestamp=scan_timestamp,
            file_path=str(runtime_path),
            bytes_written=existing_size,
        )

        previous_tail = self._read_runtime_tail(runtime_path)
        stream_has_started = existing_size > 0
        chunks_since_parse = 0
        parsed_since_last_write = True
        seen_elevation_exports = _normalize_seen_elevation_exports(
            persisted_state.get("seen_elevation_exports"),
            persisted_state.get("seen_elevation_keys"),
        )
        elevation_timestamps_by_id = _normalize_elevation_timestamps(persisted_state.get("elevation_timestamps_by_id"))
        first_elevation_timestamp: str | None = persisted_state.get("first_elevation_timestamp")

        import aiofiles

        async with aiofiles.open(runtime_path, "ab" if existing_size > 0 else "wb") as scan_file:
            async for chunk, payload in self._prefetch_pending_chunks(
                pending_chunks,
                s3_client=s3_client,
                chunk_download_semaphore=chunk_download_semaphore,
            ):
                if not payload:
                    continue

                downloaded_chunk_keys.add(self._chunk_identity(chunk))

                found_boundary, boundary_offset = detect_next_volume_offset(
                    previous_tail, payload, stream_has_started, chunk_type=chunk.chunk_type,
                )

                if found_boundary and boundary_offset >= 0:
                    before, after = split_at_boundary(payload, boundary_offset)

                    if before:
                        normalized_before = self._normalize_runtime_chunk(
                            before,
                            first_chunk_of_volume=current_state.bytes_written == 0,
                        )
                        await scan_file.write(normalized_before)
                        current_state.bytes_written += len(normalized_before)
                        parsed_since_last_write = False
                        del normalized_before

                    await scan_file.flush()

                    current_state.finalized = True
                    prior_bytes_written = current_state.bytes_written
                    first_elevation_timestamp = await asyncio.to_thread(
                        self._run_worker_parse,
                        current_state,
                        site_upper,
                        volume_id,
                        scan_timestamp,
                        seen_elevation_exports,
                        first_elevation_timestamp,
                        download_started_at=download_started_at,
                        elevation_timestamps_by_id=elevation_timestamps_by_id,
                        perf_totals=perf_totals,
                        base_dir=base_dir,
                    )
                    seen_elevation_exports = {}
                    elevation_timestamps_by_id = {}
                    first_elevation_timestamp = None
                    parsed_since_last_write = True
                    chunks_since_parse = 0

                    current_state = ScanStreamState(
                        index=current_state.index + 1,
                        volume_id=volume_id,
                        scan_timestamp=scan_timestamp,
                        file_path=str(runtime_path),
                    )
                    await scan_file.seek(0)
                    await scan_file.truncate()

                    if after:
                        normalized_after = self._normalize_runtime_chunk(
                            after,
                            first_chunk_of_volume=True,
                        )
                        await scan_file.write(normalized_after)
                        current_state.bytes_written = len(normalized_after)
                        parsed_since_last_write = False
                        await scan_file.flush()
                        prior_bytes_written = current_state.bytes_written
                        first_elevation_timestamp = await asyncio.to_thread(
                            self._run_worker_parse,
                            current_state,
                            site_upper,
                            volume_id,
                            scan_timestamp,
                            seen_elevation_exports,
                            first_elevation_timestamp,
                            download_started_at=download_started_at,
                            elevation_timestamps_by_id=elevation_timestamps_by_id,
                            perf_totals=perf_totals,
                            base_dir=base_dir,
                        )
                    stream_has_started = True
                    if current_state.bytes_written != prior_bytes_written:
                        previous_tail = self._read_runtime_tail(runtime_path)
                        await scan_file.seek(current_state.bytes_written)
                    else:
                        previous_tail = after[-MAX_MAGIC_OVERLAP:] if len(after) >= MAX_MAGIC_OVERLAP else after
                    parsed_since_last_write = True
                    del normalized_after
                    continue

                normalized_payload = self._normalize_runtime_chunk(
                    payload,
                    first_chunk_of_volume=current_state.bytes_written == 0,
                )
                await scan_file.write(normalized_payload)
                current_state.bytes_written += len(normalized_payload)
                parsed_since_last_write = False
                stream_has_started = True
                previous_tail = (previous_tail + payload)[-MAX_MAGIC_OVERLAP:]
                chunks_since_parse += 1

                if chunks_since_parse >= self.parse_checkpoint_chunk_interval and current_state.bytes_written > 0:
                    await scan_file.flush()
                    prior_bytes_written = current_state.bytes_written
                    first_elevation_timestamp = await asyncio.to_thread(
                        self._run_worker_parse,
                        current_state,
                        site_upper,
                        volume_id,
                        scan_timestamp,
                        seen_elevation_exports,
                        first_elevation_timestamp,
                        download_started_at=download_started_at,
                        elevation_timestamps_by_id=elevation_timestamps_by_id,
                        perf_totals=perf_totals,
                        base_dir=base_dir,
                    )
                    if current_state.bytes_written != prior_bytes_written:
                        previous_tail = self._read_runtime_tail(runtime_path)
                        await scan_file.seek(current_state.bytes_written)
                    parsed_since_last_write = True
                    chunks_since_parse = 0
                del normalized_payload
                del payload

            if not parsed_since_last_write and current_state.bytes_written > 0:
                await scan_file.flush()
                first_elevation_timestamp = await asyncio.to_thread(
                    self._run_worker_parse,
                    current_state,
                    site_upper,
                    volume_id,
                    scan_timestamp,
                    seen_elevation_exports,
                    first_elevation_timestamp,
                    download_started_at=download_started_at,
                    elevation_timestamps_by_id=elevation_timestamps_by_id,
                    perf_totals=perf_totals,
                    base_dir=base_dir,
                )

        self.local_chunk_store.prune_station_scan_dirs(site_upper, scan_timestamp)

        complete = _required_elevation_paths_complete(site_upper, elevation_timestamps_by_id)

        if complete:
            write_site_manifest(
                site_upper,
                current_volume_id=volume_id,
                current_volume_timestamp=scan_timestamp,
                current_download_started_at=download_started_at,
            )
            if runtime_path.exists():
                runtime_path.unlink(missing_ok=True)
            self._clear_runtime_state(site_upper, volume_id)
        else:
            self._save_runtime_state(
                site_upper,
                volume_id,
                downloaded_chunk_keys=downloaded_chunk_keys,
                bytes_written=current_state.bytes_written,
                seen_elevation_exports=seen_elevation_exports,
                elevation_timestamps_by_id=elevation_timestamps_by_id,
                first_elevation_timestamp=first_elevation_timestamp,
                scan_timestamp=scan_timestamp,
                download_started_at=download_started_at,
            )

        return NexradIngestResult(
            site=site_upper,
            volume_id=volume_id,
            vcp=0,
            dynamic_scan_type=None,
            volume_path=None,
            scan_timestamp=scan_timestamp,
            low_path=None,
            high_path=None,
            manifest_path=None,
            chunks_downloaded=len(pending_chunks),
            complete=complete,
        )

    async def _fetch_chunk_stream(self, chunk, s3_client):
        """Fetch chunk dynamically yielding parts to avoid double-buffering."""
        if self._stream_chunk_downloads:
            response = await s3_client.get_object(Bucket=chunks_bucket(), Key=chunk.key)
            body = response["Body"]
            try:
                async for data in body.iter_chunks():
                    yield data
            finally:
                await self._close_async_body(body)
        else:
            payload = await self.async_chunk_fetcher(chunk, s3_client=s3_client)
            if not isinstance(payload, (bytes, bytearray)):
                body = payload["Body"] if isinstance(payload, dict) else payload
                if hasattr(body, "iter_chunks"):
                    try:
                        async for data in body.iter_chunks():
                            yield data
                    finally:
                        await self._close_async_body(body)
                else:
                    try:
                        yield await body.read()
                    finally:
                        await self._close_async_body(body)
            else:
                yield payload

    @staticmethod
    async def _close_async_body(body):
        if hasattr(body, "close"):
            maybe_close = body.close()
            if asyncio.iscoroutine(maybe_close):
                await maybe_close

    async def _fetch_chunk_bytes(self, chunk, s3_client) -> bytes:
        """Fetch chunk bytes, returning joined bytes for boundary tracking."""
        if self._stream_chunk_downloads:
            response = await s3_client.get_object(Bucket=chunks_bucket(), Key=chunk.key)
            body = response["Body"]
            try:
                parts = []
                async for data in body.iter_chunks():
                    parts.append(data)
                return b"".join(parts)
            finally:
                await self._close_async_body(body)
        else:
            payload = await self.async_chunk_fetcher(chunk, s3_client=s3_client)
            if not isinstance(payload, (bytes, bytearray)):
                body = payload["Body"] if isinstance(payload, dict) else payload
                if hasattr(body, "iter_chunks"):
                    try:
                        parts = []
                        async for data in body.iter_chunks():
                            parts.append(data)
                        payload = b"".join(parts)
                    finally:
                        await self._close_async_body(body)
                else:
                    try:
                        payload = await body.read()
                    finally:
                        await self._close_async_body(body)
            return payload

    async def _prefetch_pending_chunks(self, pending_chunks, *, s3_client, chunk_download_semaphore=None):
        semaphore = chunk_download_semaphore or asyncio.Semaphore(self.max_chunk_downloads)
        prefetch = max(1, min(self.in_volume_prefetch, len(pending_chunks)))

        async def _fetch_one(chunk):
            async with semaphore:
                return await self._fetch_chunk_bytes(chunk, s3_client)

        tasks = {}
        next_submit = 0
        next_yield = 0
        try:
            while next_yield < len(pending_chunks):
                while next_submit < len(pending_chunks) and len(tasks) < prefetch:
                    tasks[next_submit] = asyncio.create_task(_fetch_one(pending_chunks[next_submit]))
                    next_submit += 1
                payload = await tasks.pop(next_yield)
                yield pending_chunks[next_yield], payload
                next_yield += 1
        finally:
            for task in tasks.values():
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)

    def ingest_allowed_vcp_volume(
        self,
        site,
        volume_id,
        *,
        base_dir=None,
        s3_client=None,
        weather_session=None,
        parser=None,
        writer=None,
        station_vcp=None,
    ):
        _ = (parser, writer)
        if base_dir:
            fs.initialize_filesystem(base_dir)

        if station_vcp is None:
            probe = self.volume_prober(
                site,
                volume_id,
                weather_session=weather_session,
            )
            if not probe.accepted:
                return None
            probe_vcp = probe.vcp
            probe_site = probe.site
            probe_volume_id = probe.volume_id
        else:
            if station_vcp.vcp not in allowed_vcps():
                return None
            probe_vcp = station_vcp.vcp
            probe_site = str(site).upper()
            probe_volume_id = str(volume_id)

        chunks = self.chunk_lister(site, volume_id, s3_client=s3_client)
        if not chunks:
            return NexradIngestResult(
                site=probe_site,
                volume_id=probe_volume_id,
                vcp=probe_vcp,
                dynamic_scan_type=None,
                volume_path=None,
                scan_timestamp=None,
                low_path=None,
                high_path=None,
                manifest_path=None,
                chunks_downloaded=0,
                complete=False,
            )

        result = self._stream_ingest_volume(
            probe_site,
            probe_volume_id,
            chunks,
            s3_client=s3_client,
            base_dir=base_dir,
        )
        record_volume_and_maybe_recycle()
        return NexradIngestResult(
            site=result.site,
            volume_id=result.volume_id,
            vcp=probe_vcp,
            dynamic_scan_type=None,
            volume_path=None,
            scan_timestamp=result.scan_timestamp,
            low_path=None,
            high_path=None,
            manifest_path=None,
            chunks_downloaded=result.chunks_downloaded,
            complete=result.complete,
        )

    async def ingest_allowed_vcp_volume_async(
        self,
        site,
        volume_id,
        *,
        base_dir=None,
        s3_client=None,
        weather_session=None,
        parser=None,
        writer=None,
        station_vcp=None,
        chunk_download_semaphore=None,
    ):
        _ = (parser, writer)
        total_started_at = time.perf_counter()
        if base_dir:
            fs.initialize_filesystem(base_dir)

        async with self._async_s3_client(s3_client) as active_s3_client:
            if station_vcp is None:
                probe = await asyncio.to_thread(
                    self.volume_prober,
                    site,
                    volume_id,
                    weather_session=weather_session,
                )
                if not probe.accepted:
                    return None
                probe_vcp = probe.vcp
                probe_site = probe.site
                probe_volume_id = probe.volume_id
            else:
                if station_vcp.vcp not in allowed_vcps():
                    return None
                probe_vcp = station_vcp.vcp
                probe_site = str(site).upper()
                probe_volume_id = str(volume_id)

            list_started_at = time.perf_counter()
            chunks = await self.async_chunk_lister(site, volume_id, s3_client=active_s3_client)
            list_elapsed_ms = format_perf_ms(list_started_at)
            perf_totals = _new_volume_perf_totals()
            perf_totals["chunk_list_ms"] = list_elapsed_ms
            perf_totals["chunk_count"] = len(chunks)
            if not chunks:
                total_elapsed_ms = format_perf_ms(total_started_at)
                _emit_volume_perf_summary(
                    probe_site,
                    probe_volume_id,
                    totals=perf_totals,
                    total_elapsed_ms=total_elapsed_ms,
                    complete=False,
                    chunks_downloaded=0,
                )
                return NexradIngestResult(
                    site=probe_site,
                    volume_id=probe_volume_id,
                    vcp=probe_vcp,
                    dynamic_scan_type=None,
                    volume_path=None,
                    scan_timestamp=None,
                    low_path=None,
                    high_path=None,
                    manifest_path=None,
                    chunks_downloaded=0,
                    complete=False,
                )

            result = await self._stream_ingest_volume_async(
                probe_site,
                probe_volume_id,
                chunks,
                s3_client=active_s3_client,
                base_dir=base_dir,
                chunk_download_semaphore=chunk_download_semaphore or self._shared_chunk_download_semaphore,
                perf_totals=perf_totals,
            )
            await asyncio.to_thread(record_volume_and_maybe_recycle)
            total_elapsed_ms = format_perf_ms(total_started_at)
            _emit_volume_perf_summary(
                probe_site,
                probe_volume_id,
                totals=perf_totals,
                total_elapsed_ms=total_elapsed_ms,
                complete=result.complete,
                chunks_downloaded=result.chunks_downloaded,
            )
            return NexradIngestResult(
                site=result.site,
                volume_id=result.volume_id,
                vcp=probe_vcp,
                dynamic_scan_type=None,
                volume_path=None,
                scan_timestamp=result.scan_timestamp,
                low_path=None,
                high_path=None,
                manifest_path=None,
                chunks_downloaded=result.chunks_downloaded,
                complete=result.complete,
            )

    def ingest_latest_allowed_vcp_scans(
        self,
        sites,
        *,
        max_volumes_per_site=1,
        base_dir=None,
        s3_client=None,
        weather_session=None,
        station_vcps=None,
    ):
        results = []
        s3_client = s3_client or get_unsigned_s3_client()
        for site in sites:
            volume_ids = self.volume_lister(site, limit=max_volumes_per_site, s3_client=s3_client)
            for volume_id in volume_ids:
                station_vcp = None if station_vcps is None else station_vcps.get(str(site).upper())
                result = self.ingest_allowed_vcp_volume(
                    site,
                    volume_id,
                    base_dir=base_dir,
                    s3_client=s3_client,
                    weather_session=weather_session,
                    station_vcp=station_vcp,
                )
                if result is not None:
                    results.append(result)
        return results

    async def ingest_latest_allowed_vcp_scans_async(
        self,
        sites,
        *,
        max_volumes_per_site=1,
        base_dir=None,
        s3_client=None,
        weather_session=None,
        station_vcps=None,
    ):
        total_started_at = time.perf_counter()
        sites = [str(site).upper() for site in sites]
        if base_dir:
            fs.initialize_filesystem(base_dir)

        if station_vcps is None:
            station_started_at = time.perf_counter()
            station_vcps = await asyncio.to_thread(self.station_fetcher, session=weather_session)
            io_manager.write_perf(
                f"[RUN] station_catalog_fetch: {format_perf_ms(station_started_at):.2f}ms "
                f"(stations={len(station_vcps)})"
            )

        filter_started_at = time.perf_counter()
        pruned_stale = prune_stale_site_manifests(base_dir=base_dir)
        filtered_sites = [
            site
            for site in sites
            if site.startswith("K") and station_vcps.get(site) is not None and station_vcps[site].vcp in allowed_vcps()
        ]
        io_manager.write_perf(
            f"[RUN] site_filter: {format_perf_ms(filter_started_at):.2f}ms "
            f"(input={len(sites)}, allowed={len(filtered_sites)}, pruned_stale={pruned_stale})"
        )
        if not filtered_sites:
            return []

        results = []
        site_semaphore = asyncio.Semaphore(self.max_site_tasks)
        chunk_download_semaphore = asyncio.Semaphore(self.max_chunk_downloads)
        self._shared_chunk_download_semaphore = chunk_download_semaphore

        try:
            async with self._async_s3_client(s3_client) as active_s3_client:
                async def _list_site_volumes(site):
                    async with site_semaphore:
                        started_at = time.perf_counter()
                        volume_ids = await self.async_volume_lister(
                            site,
                            limit=max_volumes_per_site,
                            s3_client=active_s3_client,
                        )
                        io_manager.write_perf(
                            f"[SITE {site}] recent_volume_list: {format_perf_ms(started_at):.2f}ms "
                            f"(volumes={len(volume_ids)}, limit={max_volumes_per_site})"
                        )
                        return site, volume_ids

                volume_discovery_started_at = time.perf_counter()
                listed_sites = await asyncio.gather(*(_list_site_volumes(site) for site in filtered_sites), return_exceptions=True)
                io_manager.write_perf(
                    f"[RUN] site_volume_discovery: {format_perf_ms(volume_discovery_started_at):.2f}ms "
                    f"(sites={len(filtered_sites)}, max_site_tasks={self.max_site_tasks})"
                )

                volume_work = []
                for listed_site in listed_sites:
                    if isinstance(listed_site, Exception):
                        io_manager.write_warning(f"Skipping site after async volume-list failure: {listed_site}")
                        continue

                    site, volume_ids = listed_site
                    station_vcp = station_vcps.get(site)
                    for volume_id in volume_ids:
                        volume_work.append((site, volume_id, station_vcp))

                volume_ingest_started_at = time.perf_counter()
                gathered = await asyncio.gather(
                    *(
                        self.ingest_allowed_vcp_volume_async(
                            site,
                            volume_id,
                            base_dir=base_dir,
                            s3_client=active_s3_client,
                            weather_session=weather_session,
                            station_vcp=station_vcp,
                            chunk_download_semaphore=chunk_download_semaphore,
                        )
                        for site, volume_id, station_vcp in volume_work
                    ),
                    return_exceptions=True,
                )
                io_manager.write_perf(
                    f"[RUN] volume_ingest_batch: {format_perf_ms(volume_ingest_started_at):.2f}ms "
                    f"(volumes={len(volume_work)}, shared_chunk_limit={self.max_chunk_downloads})"
                )
        finally:
            self._shared_chunk_download_semaphore = None

        for (site, volume_id, _station_vcp), result in zip(volume_work, gathered):
            if isinstance(result, Exception):
                io_manager.write_warning(f"Skipping {site}/{volume_id} after async ingest failure: {result}")
                continue
            if result is not None:
                results.append(result)

        complete_count = sum(1 for result in results if result.complete)
        downloaded_chunks = sum(result.chunks_downloaded for result in results)
        total_elapsed_ms = format_perf_ms(total_started_at)
        io_manager.write_perf(
            f"[RUN] latest_allowed_vcp_scans_async_total: {total_elapsed_ms:.2f}ms "
            f"(sites={len(filtered_sites)}, volumes={len(volume_work)}, results={len(results)}, "
            f"complete={complete_count}, chunks_downloaded={downloaded_chunks}, max_site_tasks={self.max_site_tasks})"
        )

        return results

    @staticmethod
    @asynccontextmanager
    async def _async_s3_client(s3_client=None):
        if s3_client is not None:
            yield s3_client
            return

        async with get_unsigned_s3_client_async() as client:
            yield client

    def list_allowed_vcp_sites(self, *, weather_session=None, stations=None):
        if stations is None:
            stations = self.station_fetcher(session=weather_session)
        return sorted(
            site
            for site, station in stations.items()
            if station.vcp in allowed_vcps() and str(site).upper().startswith("K")
        )
