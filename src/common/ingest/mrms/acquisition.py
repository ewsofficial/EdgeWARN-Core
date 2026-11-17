"""Registry-driven acquisition with bounded work and atomic validated publication.

Staging and quarantine live under state, outside raw-product consumer globs.
Sync callers use bounded joined workers with synchronous network transports.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

import aioboto3
import aiohttp
import eccodes
from botocore import UNSIGNED
from botocore.client import Config

from common.config.mrms_products import parse_product_id
from common.ingest.aws_async_compat import ensure_aiobotocore_endpoint_compat
from common.ingest.manifest import parse_file_analysis_time, staged_input_from_path
from common.ingest.mrms.source import source_for
from common.ingest.objects import CommittedInput
from common.ingest.mrms.s3_async import AsyncFileFinder
from common.ingest.mrms.https_client import HttpsFileFinder


@dataclass(frozen=True)
class ProductResult:
    product: str
    status: str
    requested_time: str
    registry_fingerprint: str
    source: str | None = None
    analysis_time: str | None = None
    path: str | None = None
    reason: str | None = None
    elapsed_seconds: float = 0.0
    sha256: str | None = None
    source_locator: str | None = None
    remote_version: str | None = None


class InputIdentityConflict(ValueError):
    """Published bytes disagree with an upstream revision; never try a mirror."""


class WorkBudget:
    """Process-wide budget shared by event loops, phases, S3 and HTTPS.

    Optional work leaves a slot for protected work when capacity allows it.
    A queued protected request takes precedence even with capacity one.
    """
    def __init__(self, limit):
        self.limit = limit
        self.active = self.optional = self.waiting = self.protected_waiting = 0
        self.peak_active = self.peak_queued = 0
        self.lock = threading.Lock()

    @asynccontextmanager
    async def slot(self, protected):
        acquired = False
        with self.lock:
            self.waiting += 1
            self.protected_waiting += int(protected)
            self.peak_queued = max(self.peak_queued, self.waiting)
        try:
            while not acquired:
                with self.lock:
                    optional_limit = max(1, self.limit - 1)
                    if self.active < self.limit and (protected or (
                        self.protected_waiting == 0 and self.optional < optional_limit
                    )):
                        self.active += 1
                        self.optional += int(not protected)
                        self.waiting -= 1
                        self.protected_waiting -= int(protected)
                        self.peak_active = max(self.peak_active, self.active)
                        acquired = True
                if not acquired:
                    await asyncio.sleep(0.01)
            yield
        finally:
            with self.lock:
                if acquired:
                    self.active -= 1
                    self.optional -= int(not protected)
                else:
                    self.waiting -= 1
                    self.protected_waiting -= int(protected)


_BUDGETS = {}
_BUDGET_LOCK = threading.Lock()
_PUBLISH_LOCK = threading.Lock()
_DOWNLOAD_LOCKS = {}


@asynccontextmanager
async def _product_lock(registry, spec, observation_time):
    from util.file import verify_mrms_containment
    verify_mrms_containment(registry)
    key = (str(registry.base_dir), spec.product_id, observation_time.isoformat())
    with _BUDGET_LOCK:
        lock = _DOWNLOAD_LOCKS.setdefault(key, threading.Lock())
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        yield
    finally:
        lock.release()


def budget_for(registry):
    settings = json.loads(registry.normalized_config_json)
    key = str(registry.base_dir)
    limit = settings['downloads']['max_concurrency']
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(key)
        if budget is None:
            budget = _BUDGETS[key] = WorkBudget(limit)
        elif budget.limit != limit:
            if budget.active or budget.waiting:
                raise RuntimeError('Cannot change MRMS budget while downloads are active')
            budget = _BUDGETS[key] = WorkBudget(limit)
        return budget


def _contained_state(registry, name):
    base = registry.base_dir.resolve()
    path = base / 'state' / 'mrms' / name
    if not path.resolve().is_relative_to(base):
        raise ValueError(f'MRMS state directory escapes base directory: {path}')
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_payload(path, spec):
    """Validate JSON shape or every GRIB2 message through value decoding."""
    adapter = spec if isinstance(spec, str) else spec.adapter
    if adapter == 'probsevere_json':
        with path.open() as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
            raise ValueError('ProbSevere payload must be a GeoJSON FeatureCollection')
        if any(not isinstance(f, dict) or f.get('type') != 'Feature' or not isinstance(f.get('properties'), dict) for f in data['features']):
            raise ValueError('Malformed ProbSevere feature')
    else:
        size = path.stat().st_size
        offset = 0
        # ecCodes tracks its own stream position; use a separate handle from
        # the one used for random-access framing checks.
        with path.open('rb') as handle, path.open('rb') as decoder:
            while offset < size:
                head = handle.read(16)
                if len(head) != 16 or head[:4] != b'GRIB' or head[7] != 2:
                    raise ValueError('Invalid GRIB2 header')
                length = int.from_bytes(head[8:16], 'big')
                if length < 20 or offset + length > size:
                    raise ValueError('Truncated GRIB2 message')
                # Validate section framing as well as the message envelope.
                end = offset + length - 4
                section = offset + 16
                seen = set()
                while section < end:
                    handle.seek(section)
                    header = handle.read(5)
                    width = int.from_bytes(header[:4], 'big')
                    if len(header) != 5 or width < 5 or section + width > end or not 1 <= header[4] <= 7:
                        raise ValueError('Invalid GRIB2 section')
                    seen.add(header[4])
                    section += width
                if section != end or not {1, 3, 4, 5, 6, 7}.issubset(seen):
                    raise ValueError('Incomplete GRIB2 sections')
                handle.seek(end)
                if handle.read(4) != b'7777':
                    raise ValueError('Missing GRIB2 terminator')
                gid = None
                try:
                    gid = eccodes.codes_grib_new_from_file(decoder)
                    if gid is None or decoder.tell() != offset + length:
                        raise ValueError('GRIB2 message could not be decoded')
                    if eccodes.codes_get_long(gid, 'numberOfValues') < 1:
                        raise ValueError('GRIB2 message has no values')
                    # This computed key unpacks the data section without
                    # materializing the full grid as a Python array.
                    eccodes.codes_get_double(gid, 'min')
                except Exception as exc:
                    raise ValueError(f'Undecodable GRIB2 message at byte {offset}: {exc}') from exc
                finally:
                    if gid is not None:
                        eccodes.codes_release(gid)
                offset += length
            if offset == 0:
                raise ValueError('Empty GRIB2 payload')
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def quarantine(registry, path, spec, source, requested, reason):
    """Bound retained diagnostics to 16 entries and 64 MiB per process tree."""
    root = _contained_state(registry, 'quarantine')
    with _PUBLISH_LOCK:
        token = uuid.uuid4().hex
        metadata = dict(product=spec.product_id, source=source, requested_time=requested.isoformat(), reason=str(reason))
        if path is not None and path.is_file() and path.stat().st_size <= 64 * 1024 * 1024:
            shutil.copyfile(path, root / (token + '.payload'))
        (root / (token + '.json')).write_text(json.dumps(metadata, sort_keys=True))
        entries = sorted(root.glob('*.json'), key=lambda p: (p.stat().st_mtime_ns, p.name))
        total = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
        while entries and (len(entries) > 16 or total > 64 * 1024 * 1024):
            old = entries.pop(0)
            for item in (old, old.with_suffix('.payload')):
                if item.exists():
                    total -= item.stat().st_size
                    item.unlink()


def publish(registry, spec, staged, digest):
    from util.file import verify_mrms_containment
    verify_mrms_containment(registry)
    spec.directory.mkdir(parents=True, exist_ok=True)
    destination = spec.directory / staged.name
    if destination.is_symlink():
        raise ValueError('Refusing symlink at MRMS publication target')
    with _PUBLISH_LOCK:
        if destination.exists():
            existing_digest = validate_payload(destination, spec)
            if existing_digest != digest:
                raise InputIdentityConflict('Conflicting content for an already published observation')
            return destination, 'local'
        # Hard-link publication is atomic and never overwrites another producer.
        try:
            os.link(staged, destination)
        except FileExistsError:
            if validate_payload(destination, spec) != digest:
                raise ValueError('Conflicting content from concurrent publication')
            return destination, 'local'
    return destination, None


async def _decode(path, chunk_size):
    if path.suffix != '.gz':
        return path
    target = path.with_suffix('')
    with gzip.open(path, 'rb') as incoming, target.open('wb') as outgoing:
        while chunk := incoming.read(chunk_size):
            outgoing.write(chunk)
            await asyncio.sleep(0)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return target


def _select(candidates, spec, dt, window):
    expected = 'MRMS_PROBSEVERE_' if spec.adapter == 'probsevere_json' else f'MRMS_{spec.product_id}_'
    choices = []
    for candidate in set(candidates):
        name = candidate.rsplit('/', 1)[-1]
        suffixes = ('.json', '.json.gz') if spec.adapter == 'probsevere_json' else ('.grib2', '.grib2.gz')
        if not name.startswith(expected) or not name.endswith(suffixes) or '\\' in name:
            continue
        stamp = parse_file_analysis_time(name)
        if stamp is not None and abs((stamp - dt).total_seconds()) <= window:
            choices.append((abs((stamp - dt).total_seconds()), -stamp.timestamp(), candidate))
    return min(choices)[2] if choices else None


async def _fetch(s3, session, source, locator, path, chunk_size, deadline, remote_version=None, remote_version_kind="etag"):
    written = 0
    if source == 's3':
        from common.ingest.mrms.source import MRMS_BUCKET
        response = await s3.get_object(Bucket=MRMS_BUCKET, Key=locator,
                                       **({('VersionId' if remote_version_kind == 'version_id' else 'IfMatch'): remote_version}
                                          if remote_version else {}))
        body = response['Body']
        # aiobotocore's context manager returns the underlying aiohttp
        # response; keep the StreamingBody wrapper for iter_chunks().
        async with body:
            with path.open('wb') as handle:
                async for chunk in body.iter_chunks(chunk_size=chunk_size):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('optional deadline expired')
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        expected = response.get('ContentLength')
    else:
        async with session.get(locator) as response:
            response.raise_for_status()
            expected = response.content_length
            with path.open('wb') as handle:
                async for chunk in response.content.iter_chunked(chunk_size):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('optional deadline expired')
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    if written == 0 or (expected is not None and written != int(expected)):
        raise ValueError(f'Truncated {source} response: expected {expected}, got {written}')


async def _acquire(registry, spec, dt, max_entries, s3, session, settings, deadline, io, discovered=None):
    async with _product_lock(registry, spec, dt):
        return await _acquire_unlocked(registry, spec, dt, max_entries, s3, session, settings, deadline, io, discovered)


async def _acquire_unlocked(registry, spec, dt, max_entries, s3, session, settings, deadline, io, discovered=None):
    started = time.monotonic()
    stage = _contained_state(registry, 'staging') / uuid.uuid4().hex
    stage.mkdir()
    reason = None
    selected_source = None
    had_candidate = False
    try:
        source = source_for(parse_product_id(spec.configured_id))
        for transport in ((discovered.source, 'https' if discovered.source == 's3' else 's3')
                          if discovered else ('s3', 'https')):
            selected_source = transport
            path = None
            try:
                if discovered is not None:
                    candidate = discovered if transport == discovered.source else discovered.mirror(transport)
                    candidates = [candidate.locator]
                elif transport == 's3':
                    if s3 is None:
                        raise RuntimeError('S3 client unavailable')
                    prefix, marker = source.listing_bounds(dt)
                    finder = AsyncFileFinder(dt, source.bucket, max_entries, io, s3_client=s3, raise_errors=True)
                    candidates = [key for key, _ in await finder.async_lookup_files(prefix, start_after=marker)]
                else:
                    candidates = await HttpsFileFinder(dt, io, raise_errors=True, source=source, timeout_seconds=settings['ncep_https']['sync_timeout_seconds']).find_files(spec.region, spec.source_modifier)
                locator = _select(candidates, spec, dt, 0 if discovered else settings['ncep_https']['match_window_seconds'])
                if locator is None:
                    continue
                had_candidate = True
                name = locator.rsplit('/', 1)[-1]
                final_name = name[:-3] if name.endswith('.gz') else name
                existing = spec.directory / final_name
                if (existing.is_file() and not existing.is_symlink()
                        and not (discovered and discovered.remote_version)):
                    try:
                        digest = validate_payload(existing, spec)
                    except Exception as exc:
                        quarantine(registry, existing, spec, 'local', dt, exc)
                        # Preserve a possibly pinned invalid file; never overwrite it.
                        raise ValueError(f'Invalid existing observation: {exc}') from exc
                    return _ready(registry, spec, dt, existing, 'local', digest, started, locator=locator)
                path = stage / name
                await _fetch(s3, session, transport, locator, path,
                             settings['ncep_https']['download_chunk_size_bytes'], deadline,
                             **({'remote_version': discovered.remote_version,
                                 'remote_version_kind': discovered.remote_version_kind}
                                if discovered and transport == discovered.source and discovered.remote_version else {}))
                decoded = await _decode(path, settings['decompress_chunk_size_bytes'])
                digest = validate_payload(decoded, spec)
                if time.monotonic() >= deadline:
                    raise TimeoutError('optional deadline expired before publication')
                destination, reuse = publish(registry, spec, decoded, digest)
                return _ready(registry, spec, dt, destination, reuse or transport, digest, started,
                              locator=locator, remote_version=(discovered.remote_version
                                  if discovered and transport == discovered.source else None))
            except (asyncio.CancelledError, InputIdentityConflict):
                raise
            except Exception as exc:
                reason = f'{transport}: {type(exc).__name__}: {exc}'
                io.write_warning(f'MRMS {spec.product_id}: {reason}')
                if path is not None:
                    quarantine(registry, path, spec, transport, dt, reason)
        return ProductResult(spec.product_id, 'failed' if reason or had_candidate else 'unavailable',
                             dt.isoformat(), registry.fingerprint, source=selected_source,
                             reason=reason or 'No matching upstream observation', elapsed_seconds=time.monotonic()-started), None
    finally:
        shutil.rmtree(stage)


def _ready(registry, spec, dt, path, source, digest, started, *, locator=None, remote_version=None):
    record = staged_input_from_path(spec.product_id, path, source=source, family='mrms')
    return ProductResult(spec.product_id, 'ready', dt.isoformat(), registry.fingerprint,
                         source, record.analysis_time.isoformat(), str(path),
                         elapsed_seconds=time.monotonic()-started, sha256=digest,
                         source_locator=locator, remote_version=remote_version), record


@asynccontextmanager
async def _async_s3(config, io):
    async with AsyncExitStack() as stack:
        try:
            ensure_aiobotocore_endpoint_compat()
            client = await stack.enter_async_context(aioboto3.Session().client('s3', config=config))
        except Exception as exc:
            io.write_warning(f'MRMS S3 client unavailable; using HTTPS: {exc}')
            client = None
        yield client


@contextmanager
def _sync_s3(config, io):
    import boto3
    try:
        client = boto3.client('s3', config=config)
    except Exception as exc:
        io.write_warning(f'MRMS S3 client unavailable; using HTTPS: {exc}')
        client = None
    try:
        yield client
    finally:
        if client is not None:
            client.close()


async def acquire_batch(registry, dt, max_entries, target_modifiers, io, *, on_committed=None):
    """Historical batch adapter over the shared per-product acquisition path.

    No deletion occurs here. Historical main.py wrappers own their isolated
    cleanup; realtime object callers defer it to inventory maintenance. An
    optional completion hook runs immediately per committed file, before the
    batch barrier, and must not delete or invalidate already published bytes.
    """
    from common.ingest.mrms.downloader import DownloadBatchResult
    settings = json.loads(registry.normalized_config_json)
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    dt = dt.replace(second=0, microsecond=0)
    specs = sorted((p for p in registry.products if target_modifiers is None or p.source_modifier in target_modifiers or p.product_id in target_modifiers), key=lambda p: (not p.protected, p.product_id))
    if target_modifiers is not None:
        known = {p.source_modifier for p in registry.products} | {p.product_id for p in registry.products}
        unknown = set(target_modifiers) - known
        if unknown:
            raise ValueError(f'Requested MRMS products are not enabled: {unknown}')
    results = {p.product_id: ProductResult(p.product_id, 'not_requested', dt.isoformat(), registry.fingerprint) for p in registry.products}
    if not specs:
        return DownloadBatchResult((), (), (), tuple(results.values()))
    budget = budget_for(registry)
    started = time.monotonic()
    deadline = started + settings['downloads']['optional_timeout_seconds']
    timeout = settings['ncep_https']['sync_timeout_seconds']
    records = []
    iterator = iter(specs)
    config = Config(signature_version=UNSIGNED, connect_timeout=timeout, read_timeout=timeout, retries={'max_attempts': 1}, max_pool_connections=budget.limit)
    async with _async_s3(config, io) as s3, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async def worker():
            for spec in iterator:
                remaining = timeout * 4 if spec.protected else max(0, deadline-time.monotonic())
                try:
                    async with asyncio.timeout(remaining):
                        async with budget.slot(spec.protected):
                            result, record = await _acquire(registry, spec, dt, max_entries, s3, session, settings,
                                                            time.monotonic() + timeout * 4 if spec.protected else deadline, io)
                    results[spec.product_id] = result
                    if record is not None:
                        records.append(record)
                except TimeoutError:
                    results[spec.product_id] = ProductResult(spec.product_id, 'failed', dt.isoformat(), registry.fingerprint,
                                                            reason='protected network deadline expired' if spec.protected else 'optional deadline expired', elapsed_seconds=time.monotonic()-started)
                except Exception as exc:
                    results[spec.product_id] = ProductResult(spec.product_id, 'failed', dt.isoformat(), registry.fingerprint,
                                                            reason=f'{type(exc).__name__}: {exc}', elapsed_seconds=time.monotonic()-started)
                if results[spec.product_id].status == 'ready' and on_committed is not None:
                    import inspect
                    notification = on_committed(_completion(result, record))
                    if inspect.isawaitable(notification):
                        await notification
        async with asyncio.TaskGroup() as group:
            for _ in range(min(len(specs), budget.limit)):
                group.create_task(worker())
    metrics = dict(peak_active=budget.peak_active, peak_queued=budget.peak_queued,
                   active=budget.active, queued=budget.waiting, elapsed_seconds=time.monotonic()-started)
    io.write_info(f'MRMS acquisition: {metrics}')
    return DownloadBatchResult(tuple(p.product_id for p in specs), tuple(records),
                               tuple(p.product_id for p in specs if results[p.product_id].status != 'ready'),
                               tuple(results.values()), tuple(metrics.items()))


async def _acquire_sync_transport(registry, spec, dt, max_entries, s3, settings, deadline, io, discovered=None):
    async with _product_lock(registry, spec, dt):
        return await _acquire_sync_unlocked(registry, spec, dt, max_entries, s3, settings, deadline, io, discovered)


async def _acquire_sync_unlocked(registry, spec, dt, max_entries, s3, settings, deadline, io, discovered=None):
    """Synchronous network operations, owned by one bounded batch worker.

    This coroutine uses the common chunked decoder without launching any tasks
    or executor jobs. Network reads have socket timeouts and deadline checks.
    """
    import requests
    from common.ingest.mrms.s3_sync import FileFinder
    started = time.monotonic()
    stage = _contained_state(registry, 'staging') / uuid.uuid4().hex
    stage.mkdir()
    reason = None
    transport = None
    try:
        source = source_for(parse_product_id(spec.configured_id))
        for transport in ((discovered.source, 'https' if discovered.source == 's3' else 's3')
                          if discovered else ('s3', 'https')):
            path = None
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError('optional deadline expired')
                if discovered is not None:
                    candidate = discovered if transport == discovered.source else discovered.mirror(transport)
                    candidates = [candidate.locator]
                elif transport == 's3':
                    if s3 is None:
                        raise RuntimeError('S3 client unavailable')
                    prefix, marker = source.listing_bounds(dt)
                    candidates = [key for key, _ in FileFinder(dt, source.bucket, max_entries, io, client=s3, raise_errors=True).lookup_files(prefix, start_after=marker)]
                else:
                    candidates = HttpsFileFinder(dt, io, raise_errors=True, source=source, timeout_seconds=settings['ncep_https']['sync_timeout_seconds']).find_files_sync(spec.region, spec.source_modifier)
                locator = _select(candidates, spec, dt, 0 if discovered else settings['ncep_https']['match_window_seconds'])
                if locator is None:
                    continue
                name = locator.rsplit('/', 1)[-1]
                existing = spec.directory / (name[:-3] if name.endswith('.gz') else name)
                if (existing.is_file() and not existing.is_symlink()
                        and not (discovered and discovered.remote_version)):
                    try:
                        digest = validate_payload(existing, spec)
                    except Exception as exc:
                        quarantine(registry, existing, spec, 'local', dt, exc)
                        raise ValueError(f'Invalid existing observation: {exc}') from exc
                    return _ready(registry, spec, dt, existing, 'local', digest, started, locator=locator)
                path = stage / name
                chunk_size = settings['ncep_https']['download_chunk_size_bytes']
                written = 0
                if transport == 's3':
                    response = s3.get_object(Bucket=source.bucket, Key=locator,
                                            **({('VersionId' if discovered.remote_version_kind == 'version_id' else 'IfMatch'): discovered.remote_version}
                                               if discovered and transport == discovered.source and discovered.remote_version else {}))
                    body = response['Body']
                    expected = response.get('ContentLength')
                    chunks = body.iter_chunks(chunk_size=chunk_size)
                else:
                    body = requests.get(locator, stream=True, timeout=settings['ncep_https']['sync_timeout_seconds'])
                    body.raise_for_status()
                    expected = body.headers.get('Content-Length')
                    chunks = body.iter_content(chunk_size=chunk_size)
                try:
                    with path.open('wb') as handle:
                        for chunk in chunks:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('optional deadline expired')
                            handle.write(chunk)
                            written += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    body.close()
                if written == 0 or (expected is not None and written != int(expected)):
                    raise ValueError(f'Truncated {transport} response: expected {expected}, got {written}')
                decoded = await _decode(path, settings['decompress_chunk_size_bytes'])
                digest = validate_payload(decoded, spec)
                if time.monotonic() >= deadline:
                    raise TimeoutError('optional deadline expired before publication')
                destination, reuse = publish(registry, spec, decoded, digest)
                return _ready(registry, spec, dt, destination, reuse or transport, digest, started,
                              locator=locator, remote_version=(discovered.remote_version
                                  if discovered and transport == discovered.source else None))
            except InputIdentityConflict:
                raise
            except Exception as exc:
                reason = f'{transport}: {type(exc).__name__}: {exc}'
                io.write_warning(f'MRMS {spec.product_id}: {reason}')
                if path is not None:
                    quarantine(registry, path, spec, transport, dt, reason)
        return ProductResult(spec.product_id, 'failed' if reason else 'unavailable', dt.isoformat(), registry.fingerprint,
                             source=transport, reason=reason or 'No matching upstream observation', elapsed_seconds=time.monotonic()-started), None
    finally:
        shutil.rmtree(stage)


def acquire_batch_sync(registry, dt, max_entries, target_modifiers, io, *, on_committed=None):
    """Bounded synchronous fallback; joins every worker before returning."""
    from concurrent.futures import ThreadPoolExecutor
    from common.ingest.mrms.downloader import DownloadBatchResult
    settings = json.loads(registry.normalized_config_json)
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    dt = dt.replace(second=0, microsecond=0)
    specs = sorted((p for p in registry.products if target_modifiers is None or p.source_modifier in target_modifiers or p.product_id in target_modifiers), key=lambda p: (not p.protected, p.product_id))
    if target_modifiers is not None:
        known = {p.source_modifier for p in registry.products} | {p.product_id for p in registry.products}
        if set(target_modifiers) - known:
            raise ValueError('Requested MRMS products are not enabled')
    results = {p.product_id: ProductResult(p.product_id, 'not_requested', dt.isoformat(), registry.fingerprint) for p in registry.products}
    if not specs:
        return DownloadBatchResult((), (), (), tuple(results.values()))
    budget = budget_for(registry)
    started = time.monotonic()
    deadline = started + settings['downloads']['optional_timeout_seconds']
    timeout = settings['ncep_https']['sync_timeout_seconds']
    records = []
    iterator = iter(specs)
    lock = threading.Lock()
    config = Config(signature_version=UNSIGNED, connect_timeout=timeout, read_timeout=timeout, retries={'max_attempts': 1}, max_pool_connections=budget.limit)
    with _sync_s3(config, io) as s3:
        def worker():
            while True:
                with lock:
                    spec = next(iterator, None)
                if spec is None:
                    return
                async def run():
                    # Timeout bounds the slot wait; network calls also check the
                    # deadline and remain joined even if their socket times out.
                    remaining = timeout * 4 if spec.protected else max(0, deadline-time.monotonic())
                    async with asyncio.timeout(remaining):
                        async with budget.slot(spec.protected):
                            return await _acquire_sync_transport(registry, spec, dt, max_entries, s3, settings,
                                                                  time.monotonic() + timeout * 4 if spec.protected else deadline, io)
                try:
                    result, record = asyncio.run(run())
                except Exception as exc:
                    result = ProductResult(spec.product_id, 'failed', dt.isoformat(), registry.fingerprint,
                                           reason=f'{type(exc).__name__}: {exc}', elapsed_seconds=time.monotonic()-started)
                    record = None
                with lock:
                    results[spec.product_id] = result
                    if record is not None:
                        records.append(record)
                if record is not None and on_committed is not None:
                    on_committed(_completion(result, record))
        with ThreadPoolExecutor(max_workers=budget.limit, thread_name_prefix='mrms') as pool:
            futures = [pool.submit(worker) for _ in range(min(len(specs), budget.limit))]
            for future in futures:
                future.result()
    metrics = dict(peak_active=budget.peak_active, peak_queued=budget.peak_queued,
                   active=budget.active, queued=budget.waiting, elapsed_seconds=time.monotonic()-started)
    io.write_info(f'MRMS acquisition: {metrics}')
    return DownloadBatchResult(tuple(p.product_id for p in specs), tuple(records),
                               tuple(p.product_id for p in specs if results[p.product_id].status != 'ready'),
                               tuple(results.values()), tuple(metrics.items()))


def _completion(result, record):
    if record is None or result.status != 'ready':
        raise RuntimeError(result.reason or f"MRMS acquisition {result.status}")
    return CommittedInput(record, result.sha256,
                          result.source_locator or record.path,
                          result.remote_version,
                          reused=result.source == 'local')


async def acquire_object(registry, discovered, io):
    """Acquire one exact object, with mirror fallback and no source cleanup.

    The caller owns inventory retention and notification delivery. No sibling
    task or batch must finish before this immutable completion is returned.
    """
    spec = registry.require(discovered.product_id)
    settings = json.loads(registry.normalized_config_json)
    timeout = settings['ncep_https']['sync_timeout_seconds']
    budget = budget_for(registry)
    duration = timeout * 4 if spec.protected else settings['downloads']['optional_timeout_seconds']
    deadline = time.monotonic() + duration
    config = Config(signature_version=UNSIGNED, connect_timeout=timeout, read_timeout=timeout,
                    retries={'max_attempts': 1}, max_pool_connections=budget.limit)
    async with asyncio.timeout(duration):
        async with budget.slot(spec.protected):
            async with _async_s3(config, io) as s3, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                result, record = await _acquire(registry, spec, discovered.observation_time,
                    1, s3, session, settings, deadline, io, discovered)
    return _completion(result, record)


def acquire_object_sync(registry, discovered, io):
    """Synchronous transport fallback; all work remains owned by the caller."""
    spec = registry.require(discovered.product_id)
    settings = json.loads(registry.normalized_config_json)
    timeout = settings['ncep_https']['sync_timeout_seconds']
    budget = budget_for(registry)
    duration = timeout * 4 if spec.protected else settings['downloads']['optional_timeout_seconds']
    deadline = time.monotonic() + duration
    config = Config(signature_version=UNSIGNED, connect_timeout=timeout, read_timeout=timeout,
                    retries={'max_attempts': 1}, max_pool_connections=budget.limit)
    with _sync_s3(config, io) as s3:
        async def run():
            async with asyncio.timeout(duration):
                async with budget.slot(spec.protected):
                    return await _acquire_sync_transport(registry, spec, discovered.observation_time,
                        1, s3, settings, deadline, io, discovered)
        result, record = asyncio.run(run())
    return _completion(result, record)
