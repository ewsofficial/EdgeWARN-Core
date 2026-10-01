"""Phase 4 fixture acquisition; no live upstream services."""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from common.ingest.mrms import acquisition as a
from common.ingest.mrms.registry import build_registry

DT = datetime(2026, 1, 1, tzinfo=timezone.utc)
NAME = 'MRMS_NewProduct_01.25_20260101-000000.grib2.gz'


def grib():
    fixture = Path(__file__).parents[3] / 'fixtures/weather/rap.grib2.b64'
    data = base64.b64decode(fixture.read_text())
    return data[:int.from_bytes(data[8:16], 'big')]


def framed_but_undecodable():
    sections = b''.join((5).to_bytes(4, 'big') + bytes([n]) for n in (1, 3, 4, 5, 6, 7))
    size = 20 + len(sections)
    return b'GRIB\0\0\0\2' + size.to_bytes(8, 'big') + sections + b'7777'


@pytest.fixture
def registry(tmp_path):
    return build_registry(dict(products=['MRMS_NewProduct_01.25'],
        downloads=dict(max_concurrency=2, optional_timeout_seconds=1),
        ncep_https=dict(sync_timeout_seconds=1, match_window_seconds=120, download_chunk_size_bytes=32),
        decompress_chunk_size_bytes=32), tmp_path)


class Body:
    def __init__(self, data): self.data = data
    # aiobotocore returns its raw aiohttp response from __aenter__, while
    # iter_chunks belongs to the StreamingBody wrapper.
    async def __aenter__(self): return object()
    async def __aexit__(self, *args): pass
    async def iter_chunks(self, **kwargs):
        yield self.data
    def close(self): pass


class S3:
    def get_paginator(self, *args): return None
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def get_object(self, **kwargs):
        data = gzip.compress(grib())
        return {'Body': Body(data), 'ContentLength': len(data)}


@pytest.fixture
def transport(monkeypatch):
    async def lookup(*args, **kwargs): return [(NAME, DT), (NAME, DT)]
    monkeypatch.setattr(a.AsyncFileFinder, 'async_lookup_files', lookup)
    monkeypatch.setattr(a.aioboto3, 'Session', lambda: type('Session', (), {'client': lambda *args, **kwargs: S3()})())
    async def https(*args): return []
    monkeypatch.setattr(a.HttpsFileFinder, 'find_files', https)


def test_new_product_async_and_validated_reuse(registry, transport):
    batch = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert batch.successful
    assert len(batch.downloaded) == 1
    path = Path(batch.downloaded[0].path)
    assert path.parent == registry.path_for('NewProduct_01.25')
    assert path.read_bytes() == grib()
    again = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert again.downloaded[0].source == 'local'
    assert all(r.status == 'not_requested' for r in again.product_results if r.product != 'NewProduct_01.25')
    assert not list((registry.base_dir / 'state/mrms/staging').iterdir())


def test_malformed_and_truncated_never_publish(registry, transport, monkeypatch):
    async def bad(self, **kwargs): return {'Body': Body(b'bad'), 'ContentLength': 9}
    monkeypatch.setattr(S3, 'get_object', bad)
    batch = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert batch.failed == ('NewProduct_01.25',)
    assert not registry.path_for('NewProduct_01.25').exists()
    assert len(list((registry.base_dir / 'state/mrms/quarantine').glob('*.json'))) == 1


def test_invalid_existing_is_rejected(registry, transport):
    spec = registry.require('NewProduct_01.25')
    spec.directory.mkdir(parents=True)
    path = spec.directory / NAME[:-3]
    path.write_bytes(b'bad')
    batch = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert batch.failed
    assert path.read_bytes() == b'bad'


def test_conflicting_publication_preserves_original(registry, tmp_path):
    spec = registry.require('NewProduct_01.25')
    path = tmp_path / NAME[:-3]
    path.write_bytes(grib())
    digest = a.validate_payload(path, spec)
    destination, _ = a.publish(registry, spec, path, digest)
    # Different valid message sequence under the same encoded identity.
    path.unlink()
    path.write_bytes(grib() * 2)
    with pytest.raises(ValueError, match='Conflicting'):
        a.publish(registry, spec, path, a.validate_payload(path, spec))
    assert destination.read_bytes() == grib()


def test_deadline_cancels_and_cleans_staging(registry, transport, monkeypatch):
    async def slow(*args, **kwargs): await asyncio.sleep(10)
    monkeypatch.setattr(a, '_fetch', slow)
    batch = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert 'deadline' in next(r.reason for r in batch.product_results if r.product == 'NewProduct_01.25')
    assert a.budget_for(registry).active == 0
    assert not list((registry.base_dir / 'state/mrms/staging').iterdir())


def test_cancel_joins_owned_tasks(registry, transport, monkeypatch):
    entered = asyncio.Event()
    async def slow(*args, **kwargs):
        entered.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(a, '_fetch', slow)
    async def run():
        task = asyncio.create_task(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    asyncio.run(run())
    assert a.budget_for(registry).active == 0
    assert not list((registry.base_dir / 'state/mrms/staging').iterdir())


def test_selection_is_deduplicated_and_order_independent(registry):
    spec = registry.require('NewProduct_01.25')
    older = NAME.replace('000000', '000100')
    wrong = NAME.replace('NewProduct_01.25', 'NewProduct_00.50')
    assert a._select([older, NAME, NAME, wrong], spec, DT, 120) == NAME
    assert a._select([wrong, NAME, older], spec, DT, 120) == NAME


def test_quarantine_is_bounded(registry):
    spec = registry.require('NewProduct_01.25')
    for _ in range(20): a.quarantine(registry, None, spec, 's3', DT, 'bad')
    assert len(list((registry.base_dir / 'state/mrms/quarantine').glob('*.json'))) == 16


def test_payload_validation(registry, tmp_path):
    path = tmp_path / 'payload'
    spec = registry.require('NewProduct_01.25')
    for data in (b'', b'GRIB', grib()[:-1], grib() + b'junk',
                 framed_but_undecodable(), grib() + framed_but_undecodable()):
        path.write_bytes(data)
        with pytest.raises(ValueError): a.validate_payload(path, spec)
    path.write_text('{"type":"FeatureCollection","features":[]}')
    assert a.validate_payload(path, registry.require('ProbSevere'))
    path.write_text('{}')
    with pytest.raises(ValueError): a.validate_payload(path, registry.require('ProbSevere'))


def test_undecodable_s3_payload_falls_back_to_https(registry, transport, monkeypatch):
    bad = gzip.compress(framed_but_undecodable())
    good = gzip.compress(grib())

    async def bad_s3(self, **kwargs):
        return {'Body': Body(bad), 'ContentLength': len(bad)}

    class Response:
        content_length = len(good)
        def __init__(self): self.content = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def iter_chunked(self, *args): yield good

    class Http:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args): return Response()

    async def https(*args):
        return ['https://mrms.ncep.noaa.gov/data/2D/NewProduct/' + NAME]

    monkeypatch.setattr(S3, 'get_object', bad_s3)
    monkeypatch.setattr(a.HttpsFileFinder, 'find_files', https)
    monkeypatch.setattr(a.aiohttp, 'ClientSession', Http)
    batch = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert batch.successful
    assert batch.downloaded[0].source == 'https'
    assert Path(batch.downloaded[0].path).read_bytes() == grib()
    assert len(list((registry.base_dir / 'state/mrms/quarantine').glob('*.json'))) == 1


def test_new_product_sync(registry, monkeypatch):
    import boto3
    from common.ingest.mrms.s3_sync import FileFinder
    payload = gzip.compress(grib())
    class SyncBody:
        def iter_chunks(self, **kwargs): yield payload
        def close(self): pass
    class SyncS3:
        def close(self): pass
        def get_paginator(self, *args): return None
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_object(self, **kwargs): return {'Body': SyncBody(), 'ContentLength': len(payload)}
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs: SyncS3())
    monkeypatch.setattr(FileFinder, 'lookup_files', lambda *args, **kwargs: [(NAME, DT)])
    batch = a.acquire_batch_sync(registry, DT, 10, ['NewProduct_01.25'], MagicMock())
    assert batch.successful
    assert Path(batch.downloaded[0].path).read_bytes() == grib()
    assert a.budget_for(registry).active == 0


@pytest.mark.parametrize('sync', [False, True])
def test_https_fallback_publishes_generated_directory(registry, monkeypatch, sync):
    import boto3
    import requests
    from common.ingest.mrms.s3_sync import FileFinder
    payload = gzip.compress(grib())
    url = 'https://mrms.ncep.noaa.gov/data/2D/NewProduct/' + NAME
    async def no_s3(*args, **kwargs): return []
    async def https(*args, **kwargs): return [url, url]
    class Response:
        content_length = len(payload)
        headers = {'Content-Length': str(len(payload))}
        content = None
        def __init__(self): self.content = self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def iter_chunked(self, *args): yield payload
        def iter_content(self, **kwargs): yield payload
        def close(self): pass
    class Http:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args): return Response()
    class SyncS3:
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_paginator(self, *args): return None
    monkeypatch.setattr(a.AsyncFileFinder, 'async_lookup_files', no_s3)
    monkeypatch.setattr(a.aioboto3, 'Session', lambda: type('Session', (), {'client': lambda *args, **kwargs: S3()})())
    monkeypatch.setattr(a.HttpsFileFinder, 'find_files', https)
    monkeypatch.setattr(a.aiohttp, 'ClientSession', Http)
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs: SyncS3())
    monkeypatch.setattr(FileFinder, 'lookup_files', lambda *args, **kwargs: [])
    monkeypatch.setattr(a.HttpsFileFinder, 'find_files_sync', lambda *args, **kwargs: [url])
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: Response())
    if sync:
        batch = a.acquire_batch_sync(registry, DT, 10, ['NewProduct_01.25'], MagicMock())
    else:
        batch = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert batch.successful
    assert batch.downloaded[0].source == 'https'
    assert Path(batch.downloaded[0].path).read_bytes() == grib()


def test_budget_reserves_protected_capacity():
    async def run():
        budget = a.WorkBudget(2)
        optional_started = asyncio.Event()
        release = asyncio.Event()
        order = []
        async def optional():
            async with budget.slot(False):
                optional_started.set()
                await release.wait()
        async def second_optional():
            async with budget.slot(False): order.append('optional')
        async def protected():
            async with budget.slot(True): order.append('protected')
        first = asyncio.create_task(optional())
        await optional_started.wait()
        second = asyncio.create_task(second_optional())
        core = asyncio.create_task(protected())
        await asyncio.wait_for(core, 1)
        assert order == ['protected']
        release.set()
        await asyncio.gather(first, second)
        assert budget.peak_active == 2
        assert budget.active == budget.waiting == 0
    asyncio.run(run())


def test_restart_ignores_abandoned_staging(registry, transport):
    abandoned = registry.base_dir / 'state/mrms/staging/abandoned'
    abandoned.mkdir(parents=True)
    (abandoned / NAME).write_bytes(b'partial')
    batch = asyncio.run(a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock()))
    assert batch.successful
    assert Path(batch.downloaded[0].path).read_bytes() == grib()
    assert (abandoned / NAME).read_bytes() == b'partial'


def test_empty_optional_selection_has_no_transport(registry, monkeypatch):
    monkeypatch.setattr(a.aioboto3, 'Session', lambda: pytest.fail('empty selection opened network'))
    batch = asyncio.run(a.acquire_batch(registry, DT, 10, [], MagicMock()))
    assert not batch.successful
    assert not batch.attempted
    assert all(r.status == 'not_requested' for r in batch.product_results)


def test_concurrent_batches_reuse_one_observation(registry, transport, monkeypatch):
    calls = []
    original = S3.get_object
    async def counted(self, **kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.02)
        return await original(self, **kwargs)
    monkeypatch.setattr(S3, 'get_object', counted)
    async def run():
        return await asyncio.gather(*(
            a.acquire_batch(registry, DT, 10, ['NewProduct_01.25'], MagicMock())
            for _ in range(2)
        ))
    results = asyncio.run(run())
    assert all(r.successful for r in results)
    assert len(calls) == 1
    assert a.budget_for(registry).active == 0


def test_registry_https_source_cannot_follow_legacy_endpoint(registry, monkeypatch):
    from common.ingest.mrms import https_client
    source = a.source_for(a.parse_product_id('MRMS_NewProduct_01.25'))
    monkeypatch.setattr(https_client, 'ncep_base_url', lambda: 'https://wrong.invalid')
    finder = https_client.HttpsFileFinder(DT, source=source, timeout_seconds=2)
    assert finder.construct_url('CONUS', 'NewProduct_01.25') == 'https://mrms.ncep.noaa.gov/data/2D/NewProduct'


def test_sync_deadline_joins_workers_without_late_publication(registry, monkeypatch):
    import boto3
    import threading
    import time
    from common.ingest.mrms.s3_sync import FileFinder
    settings = json.loads(registry.normalized_config_json)
    settings['downloads']['optional_timeout_seconds'] = 0.02
    registry = build_registry(settings, registry.base_dir)
    payload = gzip.compress(grib())
    class SyncBody:
        def iter_chunks(self, **kwargs):
            time.sleep(0.04)
            yield payload
        def close(self): pass
    class SyncS3:
        def close(self): pass
        def get_paginator(self, *args): return None
        def get_object(self, **kwargs): return {'Body': SyncBody(), 'ContentLength': len(payload)}
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs: SyncS3())
    monkeypatch.setattr(FileFinder, 'lookup_files', lambda *args, **kwargs: [(NAME, DT)])
    started = time.monotonic()
    batch = a.acquire_batch_sync(registry, DT, 10, ['NewProduct_01.25'], MagicMock())
    assert time.monotonic() - started < 1
    assert batch.failed == ('NewProduct_01.25',)
    assert not registry.path_for('NewProduct_01.25').exists()
    assert not any(t.name.startswith('mrms') for t in threading.enumerate())
    assert a.budget_for(registry).active == 0
    assert not list((registry.base_dir / 'state/mrms/staging').iterdir())


def discovered(registry, name=NAME, transport='s3'):
    from common.ingest.mrms.source import DiscoveredObject
    stamp = a.parse_file_analysis_time(name)
    source = a.source_for(a.parse_product_id('MRMS_NewProduct_01.25'))
    locator = (source.s3_prefix(stamp) if transport == 's3' else source.https_url + '/') + name
    return DiscoveredObject('NewProduct_01.25', stamp, transport, locator)


def test_object_completes_while_sibling_blocked(registry, transport, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = a._fetch
    async def blocked(s3, session, source, locator, *args, **kwargs):
        if '000200' in locator:
            entered.set()
            await release.wait()
        return await original(s3, session, source, locator, *args, **kwargs)
    monkeypatch.setattr(a, '_fetch', blocked)
    async def run():
        # Different products need not wait on the per-product publication lock.
        from common.ingest.mrms.source import DiscoveredObject
        other = DiscoveredObject('PrecipFlag_00.00', DT.replace(minute=2), 's3',
            'CONUS/PrecipFlag_00.00/20260101/MRMS_PrecipFlag_00.00_20260101-000200.grib2.gz')
        slow = asyncio.create_task(a.acquire_object(registry, other, MagicMock()))
        await entered.wait()
        first = await asyncio.wait_for(a.acquire_object(registry, discovered(registry), MagicMock()), 1)
        assert first.record.local_path.is_file() and first.sha256
        assert not slow.done()
        release.set()
        await slow
    asyncio.run(run())


def test_duplicate_mirrors_have_one_committed_identity(registry, transport):
    first = asyncio.run(a.acquire_object(registry, discovered(registry), MagicMock()))
    again = asyncio.run(a.acquire_object(registry, discovered(registry, transport='https'), MagicMock()))
    assert first.input_id == again.input_id
    assert again.reused
    assert discovered(registry).logical_identity == discovered(registry, transport='https').logical_identity


def test_corrupt_object_never_completes(registry, transport, monkeypatch):
    async def corrupt(*args, **kwargs):
        args[4].write_bytes(b'partial')
    monkeypatch.setattr(a, '_fetch', corrupt)
    with pytest.raises(RuntimeError):
        asyncio.run(a.acquire_object(registry, discovered(registry), MagicMock()))
    assert not registry.path_for('NewProduct_01.25').exists()


def test_object_fallback_is_exact_not_nearest(registry, transport, monkeypatch):
    attempted = []
    async def fetch(s3, session, transport, locator, path, *args, **kwargs):
        attempted.append((transport, locator))
        if transport == 's3':
            raise OSError('S3 unavailable')
        path.write_bytes(gzip.compress(grib()))
    monkeypatch.setattr(a, '_fetch', fetch)
    result = asyncio.run(a.acquire_object(registry, discovered(registry), MagicMock()))
    assert result.record.source == 'https'
    assert result.source_locator == attempted[1][1]
    assert [source for source, _ in attempted] == ['s3', 'https']
    assert all(locator.endswith(NAME) for _, locator in attempted)


def test_probsevere_object_json(registry, transport, monkeypatch):
    from common.ingest.mrms.source import DiscoveredObject
    async def fetch(s3, session, transport, locator, path, *args, **kwargs):
        path.write_text('{"type":"FeatureCollection","features":[]}')
    monkeypatch.setattr(a, '_fetch', fetch)
    obj = DiscoveredObject('ProbSevere', DT, 's3',
                           'ProbSevere/20260101/MRMS_PROBSEVERE_20260101_000000.json')
    result = asyncio.run(a.acquire_object(registry, obj, MagicMock()))
    assert result.record.product == 'ProbSevere'
    assert result.record.analysis_time == DT


def test_sync_object_completion(registry, monkeypatch):
    import boto3
    payload = gzip.compress(grib())
    class SyncBody:
        def iter_chunks(self, **kwargs): yield payload
        def close(self): pass
    class Client:
        def get_object(self, **kwargs): return {'Body': SyncBody(), 'ContentLength': len(payload)}
        def close(self): pass
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs: Client())
    result = a.acquire_object_sync(registry, discovered(registry), MagicMock())
    assert result.record.local_path.read_bytes() == grib()


@pytest.mark.parametrize('sync', [False, True])
def test_discovery_follows_pages_across_day_boundary(registry, sync):
    from datetime import timedelta
    from common.ingest.mrms.discovery import discover_objects, discover_objects_sync
    older = discovered(registry, NAME.replace('20260101-000000', '20251231-235800'))
    current = discovered(registry)
    later = discovered(registry, NAME.replace('000000', '000200'))
    pages = [
        {'Contents': [{'Key': older.locator, 'ETag': 'v1'}]},
        {'Contents': [{'Key': current.locator, 'ETag': 'v2'}]},
        {'Contents': [{'Key': later.locator, 'ETag': 'v3'}]},
    ]
    seen = []
    class Paginator:
        def paginate(self, **kwargs):
            seen.append(kwargs)
            subset = [p for p in pages if p['Contents'][0]['Key'].startswith(kwargs['Prefix'])]
            if sync:
                return iter(subset)
            async def iterate():
                for page in subset: yield page
            return iterate()
    client = MagicMock()
    client.get_paginator.return_value = Paginator()
    args = (registry.require('NewProduct_01.25'), DT-timedelta(minutes=3), DT+timedelta(minutes=2))
    kwargs = dict(s3=client, io=MagicMock(), max_objects=10, max_pages=10, page_size=1, timeout_seconds=1)
    result = discover_objects_sync(*args, **kwargs) if sync else asyncio.run(discover_objects(*args, **kwargs))
    assert [o.observation_time for o in result] == [older.observation_time, DT, later.observation_time]
    assert [o.remote_version for o in result] == ['v1', 'v2', 'v3']
    assert len(seen) == 2
    assert all(k['PaginationConfig']['PageSize'] == 1 for k in seen)


@pytest.mark.parametrize('bound', ['max_objects', 'max_pages'])
def test_discovery_overflow_is_explicit(registry, bound):
    from datetime import timedelta
    from common.ingest.mrms.discovery import discover_objects, ListingLimitExceeded
    objects = [discovered(registry), discovered(registry, NAME.replace('000000', '000200'))]
    class Paginator:
        async def paginate(self, **kwargs):
            for obj in objects:
                yield {'Contents': [{'Key': obj.locator}]}
    client = MagicMock()
    client.get_paginator.return_value = Paginator()
    kwargs = dict(s3=client, io=MagicMock(), max_objects=10, max_pages=10, page_size=1, timeout_seconds=1)
    kwargs[bound] = 1
    with pytest.raises(ListingLimitExceeded):
        asyncio.run(discover_objects(registry.require('NewProduct_01.25'), DT, DT+timedelta(minutes=2), **kwargs))


def test_discovery_https_window_includes_late_previous_day(registry, monkeypatch):
    from datetime import timedelta
    from common.ingest.mrms.discovery import discover_objects
    older = discovered(registry, NAME.replace('20260101-000000', '20251231-235800'), 'https')
    current = discovered(registry, transport='https')
    async def listing(self, *args):
        return [older.locator, current.locator, current.locator]
    monkeypatch.setattr(a.HttpsFileFinder, 'find_files', listing)
    result = asyncio.run(discover_objects(registry.require('NewProduct_01.25'),
        DT-timedelta(minutes=3), DT, s3=None, io=MagicMock(), max_objects=10,
        max_pages=10, page_size=10, timeout_seconds=1))
    assert [o.logical_identity for o in result] == [older.logical_identity, current.logical_identity]


def test_batch_notifies_before_sibling_finishes(registry, transport, monkeypatch):
    release = asyncio.Event()
    completed = asyncio.Event()
    original = a._acquire
    async def acquire(*args, **kwargs):
        if args[1].product_id == 'PrecipFlag_00.00':
            await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(a, '_acquire', acquire)
    async def run():
        task = asyncio.create_task(a.acquire_batch(registry, DT, 10,
            ['NewProduct_01.25', 'PrecipFlag_00.00'], MagicMock(),
            on_committed=lambda result: completed.set()))
        await asyncio.wait_for(completed.wait(), 1)
        assert not task.done()
        release.set()
        await task
    asyncio.run(run())


def test_upstream_revision_conflict_does_not_fall_back_or_complete(registry, transport, monkeypatch):
    from dataclasses import replace
    first = asyncio.run(a.acquire_object(registry, discovered(registry), MagicMock()))
    calls = []
    async def changed(s3, session, source, locator, path, *args, **kwargs):
        calls.append(source)
        path.write_bytes(gzip.compress(grib() * 2))
    monkeypatch.setattr(a, '_fetch', changed)
    with pytest.raises(a.InputIdentityConflict, match='Conflicting'):
        asyncio.run(a.acquire_object(registry, replace(discovered(registry), remote_version='new-etag'), MagicMock()))
    assert calls == ['s3']
    assert first.record.local_path.read_bytes() == grib()


def test_same_product_observations_complete_independently(registry, transport, monkeypatch):
    from common.ingest.mrms.source import DiscoveredObject
    product = 'PrecipFlag_00.00'
    def obj(minute):
        stamp = DT.replace(minute=minute)
        return DiscoveredObject(product, stamp, 's3',
            'CONUS/PrecipFlag_00.00/20260101/' +
            f'MRMS_PrecipFlag_00.00_20260101-00{minute:02d}00.grib2.gz')
    entered, release = asyncio.Event(), asyncio.Event()
    original = a._fetch
    async def slow(s3, session, source, locator, *args, **kwargs):
        if '000200' in locator:
            entered.set()
            await release.wait()
        return await original(s3, session, source, locator, *args, **kwargs)
    monkeypatch.setattr(a, '_fetch', slow)
    async def run():
        sibling = asyncio.create_task(a.acquire_object(registry, obj(2), MagicMock()))
        await entered.wait()
        first = await asyncio.wait_for(a.acquire_object(registry, obj(0), MagicMock()), 1)
        assert first.record.analysis_time == DT and not sibling.done()
        release.set()
        await sibling
    asyncio.run(run())


@pytest.mark.parametrize('kind,parameter', [('etag', 'IfMatch'), ('version_id', 'VersionId')])
def test_remote_version_uses_correct_s3_constraint(registry, transport, monkeypatch, kind, parameter):
    from dataclasses import replace
    calls = []
    original = S3.get_object
    async def fetch(self, **kwargs):
        calls.append(kwargs)
        return await original(self, **kwargs)
    monkeypatch.setattr(S3, 'get_object', fetch)
    obj = replace(discovered(registry), remote_version='remote-version', remote_version_kind=kind)
    completed = asyncio.run(a.acquire_object(registry, obj, MagicMock()))
    assert calls[0][parameter] == 'remote-version'
    assert completed.remote_version == 'remote-version'
