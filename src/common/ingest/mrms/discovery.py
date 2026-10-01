"""Bounded complete-window discovery, independent of Core's processing cursor.

Overflow raises explicitly: callers must retain/backpressure the window rather
than advance a cursor from an incomplete listing. Clients own request timeouts.
"""
from datetime import timedelta

from common.config.mrms_products import parse_product_id
from common.ingest.manifest import parse_file_analysis_time
from common.ingest.mrms.source import DiscoveredObject, source_for, _utc
from common.ingest.mrms.https_client import HttpsFileFinder


class ListingLimitExceeded(RuntimeError):
    pass


def _days(start, end):
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        yield day
        day += timedelta(days=1)


def _bounds(spec, start, end, max_objects, max_pages, page_size):
    start, end = _utc(start), _utc(end)
    if start > end or end - start > timedelta(days=7):
        raise ValueError("Discovery window must be ordered and at most seven days")
    if max_objects < 1 or max_pages < 1 or not 1 <= page_size <= 1000:
        raise ValueError("Invalid listing bounds")
    return source_for(parse_product_id(spec.configured_id)), start, end


def _add(found, spec, locator, transport, version, start, end, limit, version_kind="etag"):
    stamp = parse_file_analysis_time(locator)
    if stamp is None or not start <= stamp <= end:
        return
    try:
        obj = DiscoveredObject(spec.product_id, stamp, transport, locator, version, version_kind)
    except ValueError:
        return  # Foreign products, malformed names and index navigation entries.
    found[obj.acquisition_identity] = obj
    if len(found) > limit:
        raise ListingLimitExceeded(f"{spec.product_id}: eligible object limit {limit} exceeded")


def _kwargs(source, day, start, page_size):
    prefix = source.s3_prefix(day)
    return dict(Bucket=source.bucket, Prefix=prefix,
                StartAfter=prefix + source.filename_start_after(max(day, start)),
                PaginationConfig={"PageSize": page_size})


async def discover_objects(spec, start, end, *, s3, io, max_objects, max_pages,
                           page_size, timeout_seconds):
    import asyncio
    source, start, end = _bounds(spec, start, end, max_objects, max_pages, page_size)
    found = {}
    async with asyncio.timeout(timeout_seconds):
        try:
            if s3 is None:
                raise RuntimeError("S3 unavailable")
            pages = 0
            for day in _days(start, end):
                async for page in s3.get_paginator("list_objects_v2").paginate(**_kwargs(source, day, start, page_size)):
                    pages += 1
                    if pages > max_pages:
                        raise ListingLimitExceeded(f"{spec.product_id}: page limit exceeded")
                    for obj in page.get("Contents", ()):
                        _add(found, spec, obj["Key"], "s3", obj.get("VersionId") or obj.get("ETag"), start, end, max_objects,
                             "version_id" if obj.get("VersionId") else "etag")
            if found:
                return tuple(sorted(found.values(), key=lambda x: (x.observation_time, x.locator)))
        except ListingLimitExceeded:
            raise
        except Exception as exc:
            io.write_warning(f"MRMS window listing falling back to HTTPS: {exc}")
        for day in _days(start, end):
            finder = HttpsFileFinder(day, io, raise_errors=True, source=source, timeout_seconds=timeout_seconds)
            for locator in await finder.find_files(spec.region, spec.source_modifier):
                _add(found, spec, locator, "https", None, start, end, max_objects)
    return tuple(sorted(found.values(), key=lambda x: (x.observation_time, x.locator)))


def discover_objects_sync(spec, start, end, *, s3, io, max_objects, max_pages,
                          page_size, timeout_seconds):
    import time
    source, start, end = _bounds(spec, start, end, max_objects, max_pages, page_size)
    found = {}
    deadline = time.monotonic() + timeout_seconds
    def check_deadline():
        if time.monotonic() >= deadline:
            raise TimeoutError("MRMS listing deadline expired")
    try:
        if s3 is None:
            raise RuntimeError("S3 unavailable")
        pages = 0
        for day in _days(start, end):
            check_deadline()
            for page in s3.get_paginator("list_objects_v2").paginate(**_kwargs(source, day, start, page_size)):
                check_deadline()
                pages += 1
                if pages > max_pages:
                    raise ListingLimitExceeded(f"{spec.product_id}: page limit exceeded")
                for obj in page.get("Contents", ()):
                    _add(found, spec, obj["Key"], "s3", obj.get("VersionId") or obj.get("ETag"), start, end, max_objects,
                             "version_id" if obj.get("VersionId") else "etag")
        if found:
            return tuple(sorted(found.values(), key=lambda x: (x.observation_time, x.locator)))
    except (ListingLimitExceeded, TimeoutError):
        raise
    except Exception as exc:
        io.write_warning(f"MRMS window listing falling back to HTTPS: {exc}")
    for day in _days(start, end):
        check_deadline()
        finder = HttpsFileFinder(day, io, raise_errors=True, source=source,
                                 timeout_seconds=max(0.001, deadline-time.monotonic()))
        for locator in finder.find_files_sync(spec.region, spec.source_modifier):
            _add(found, spec, locator, "https", None, start, end, max_objects)
    check_deadline()
    return tuple(sorted(found.values(), key=lambda x: (x.observation_time, x.locator)))
