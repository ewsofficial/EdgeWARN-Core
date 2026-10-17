"""
NWS Alert Ingest Module

Downloads active NWS alerts from the National Weather Service API,
filters them by event type, applies GeoMapper for zone-to-polygon mapping,
and stores them in a deduplicated registry.

Architecture:
    - Downloads from nws.yaml's active_alerts_url at the nws_seconds cadence in runtime.yaml (background_intervals)
    - Filters out non-severe event types (nws.yaml dropped_events blocklist)
    - Applies GeoMapper to map UGC zone codes to actual polygons
    - Stores unique alerts in alerts_registry.json with deduplication
    - Reconciles saved alerts to the latest active upstream ID set each cycle
    - TTL/expiration cleanup remains as a secondary safety net
"""

import json
import ijson
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
import util.file as fs
from util.io import IOManager
from util.release import weather_api_headers
from .config import (
    active_alerts_url,
    download_chunk_size_bytes,
    dropped_events,
    registry_ttl_hours,
)
import aiohttp
import asyncio
import tempfile
import os
from typing import Dict, Any, List, Optional, Set, Tuple

# Import GeoMapper logic
from .geomapper import process_warning

# Import AlertRegistry for deduplication
from .registry import AlertRegistry, get_registry, reset_registry

# Custom JSON encoder to handle Decimal types from ijson
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

io_manager = IOManager("[NWS Ingest]")

def _get_registry() -> AlertRegistry:
    """Get or initialize the AlertRegistry singleton."""
    return get_registry(fs.MRMS_NWS_DIR, ttl_hours=registry_ttl_hours())


def _prepare(dt: datetime):
    """Create the output directory, load the registry and prune old snapshots."""
    fs.MRMS_NWS_DIR.mkdir(parents=True, exist_ok=True)
    registry = _get_registry()
    current_time = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    registry.cleanup_old_timestamps(current_time)
    return registry, current_time


def _apply_download(temp_path: str, registry: AlertRegistry, current_time: datetime, label: str) -> None:
    """Fold a downloaded feed into the registry, reconcile and persist it."""
    new_count, updated_count, seen_ids, complete = _process_nws_file_with_registry(
        temp_path, registry, current_time)

    # An empty ID set from a fully parsed feed means no active alerts upstream,
    # so reconcile to empty. Only features whose IDs could not be recovered make
    # the set untrustworthy, and then the registry is left to the TTL.
    reconciled_removed_count = (
        registry.reconcile_with_active_ids(seen_ids, current_time) if complete else 0
    )
    removed_count = registry.cleanup_expired(current_time)
    registry.save()

    io_manager.write_info(
        f"Registry updated{label}: "
        f"new={new_count}, updated={updated_count}, "
        f"reconciled={reconciled_removed_count}, expired_removed={removed_count}, "
        f"total_active={registry.alert_count}"
    )


def _remove_quietly(path: Optional[str]) -> None:
    if path is not None:
        try:
            os.remove(path)
        except OSError:
            pass


def download_alerts(dt: datetime):
    """
    Download active NWS alerts, filter them by event type, apply GeoMapper,
    and update the alerts registry with deduplication.

    Args:
        dt: Current datetime (used for timestamp tracking)
    """
    registry, current_time = _prepare(dt)
    io_manager.write_info("Downloading active alerts for registry update...")

    req = urllib.request.Request(active_alerts_url(), headers=weather_api_headers())
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp()
        with os.fdopen(fd, 'wb') as tmp, urllib.request.urlopen(req) as response:
            tmp.write(response.read())
        _apply_download(temp_path, registry, current_time, "")
    except Exception as e:
        io_manager.write_error(f"Failed to download/process NWS alerts: {e}")
        raise
    finally:
        _remove_quietly(temp_path)


async def download_alerts_async(dt: datetime):
    """
    Async version of download_alerts.
    Downloads active NWS alerts using aiohttp, processes with deduplication,
    and updates the alerts registry.

    Args:
        dt: Current datetime (used for timestamp tracking)
    """
    registry, current_time = _prepare(dt)
    io_manager.write_info("Downloading active alerts (async) for registry update...")

    chunk_size = download_chunk_size_bytes()

    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp()
        os.close(fd)

        async with aiohttp.ClientSession(headers=weather_api_headers()) as session:
            async with session.get(active_alerts_url()) as response:
                response.raise_for_status()
                with open(temp_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        f.write(chunk)

        # Parsing and registry work are blocking, so keep them off the loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, _apply_download, temp_path, registry, current_time, " (async)")
    except Exception as e:
        io_manager.write_error(f"Failed to download/process NWS alerts (async): {e}")
        raise
    finally:
        _remove_quietly(temp_path)


def _process_nws_file_with_registry(
    input_path: str, 
    registry: AlertRegistry, 
    current_time: datetime
) -> Tuple[int, int, Set[str], bool]:
    """
    Process the raw NWS JSON file and update the registry.
    
    Filters events, applies GeoMapper logic, and adds/updates alerts in registry.
    
    Args:
        input_path: Path to the downloaded NWS JSON file
        registry: AlertRegistry instance to update
        current_time: Current timestamp for tracking
        
    Returns:
        Tuple of (new_count, updated_count, seen_ids, complete); ``complete`` is
        False when a malformed feature had no recoverable ID
    """
    new_count = 0
    updated_count = 0
    seen_ids: Set[str] = set()
    complete = True
    # Read once per file rather than per feature: the blocklist cannot change
    # mid-file, and a feed carries tens of thousands of features.
    dropped = dropped_events()

    try:
        with open(input_path, 'r', encoding='utf-8') as infile:
            # Validate the entire stream before mutating the singleton.
            for _ in ijson.parse(infile):
                pass
            infile.seek(0)
            features = ijson.items(infile, 'features.item')

            for feature in features:
                if not isinstance(feature, dict) or not isinstance(feature.get('properties'), dict):
                    io_manager.write_warning("Skipping malformed NWS feature")
                    if isinstance(feature, dict) and isinstance(feature.get('id'), str):
                        seen_ids.add(feature['id'])
                    else:
                        complete = False
                    continue
                props = feature['properties']
                if props.get('event') in dropped:
                    continue
                try:
                    processed_feature = process_warning(feature)
                    is_new, alert_id = registry.process_alert(processed_feature, current_time)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    io_manager.write_warning(f"Skipping malformed NWS feature: {exc}")
                    alert_id = props.get('id') or feature.get('id')
                    if isinstance(alert_id, str):
                        seen_ids.add(alert_id)
                    else:
                        complete = False
                    continue
                if alert_id:
                    seen_ids.add(alert_id)
                    if is_new:
                        new_count += 1
                    else:
                        updated_count += 1

        return new_count, updated_count, seen_ids, complete
    except Exception as e:
        raise e

