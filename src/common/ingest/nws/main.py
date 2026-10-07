"""
NWS Alert Ingest Module

Downloads active NWS alerts from the National Weather Service API,
filters them by event type, applies GeoMapper for zone-to-polygon mapping,
and stores them in a deduplicated registry.

Architecture:
    - Downloads from nws.yaml's active_alerts_url every 2 minutes
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


def download_alerts(dt: datetime):
    """
    Download active NWS alerts, filter them by event type, Apply GeoMapper,
    and update the alerts registry with deduplication.
    
    Args:
        dt: Current datetime (used for timestamp tracking)
    """
    url = active_alerts_url()

    # Ensure output directory exists
    if not fs.MRMS_NWS_DIR.exists():
        fs.MRMS_NWS_DIR.mkdir(parents=True, exist_ok=True)

    # Get the registry
    registry = _get_registry()

    io_manager.write_info(f"Downloading active alerts for registry update...")

    # Cleanup old timestamp snapshot files before download
    current_time = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    registry.cleanup_old_timestamps(current_time)

    headers = weather_api_headers()

    req = urllib.request.Request(url, headers=headers)

    temp_path = None
    try:
        with urllib.request.urlopen(req) as response:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(response.read())
                temp_path = tmp.name

        current_time = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        new_count, updated_count, seen_ids = _process_nws_file_with_registry(temp_path, registry, current_time)

        # Empty/entirely malformed pulls must not erase still-valid warnings.
        reconciled_removed_count = registry.reconcile_with_active_ids(seen_ids or registry.get_active_ids(), current_time)
        removed_count = registry.cleanup_expired(current_time)
        registry.save()

        total_active = registry.alert_count
        io_manager.write_info(
            "Registry updated: "
            f"new={new_count}, updated={updated_count}, "
            f"reconciled={reconciled_removed_count}, expired_removed={removed_count}, "
            f"total_active={total_active}"
        )

    except Exception as e:
        io_manager.write_error(f"Failed to download/process NWS alerts: {e}")
        raise e
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except Exception:
                pass


async def download_alerts_async(dt: datetime):
    """
    Async version of download_alerts.
    Downloads active NWS alerts using aiohttp, processes with deduplication,
    and updates the alerts registry.
    
    Args:
        dt: Current datetime (used for timestamp tracking)
    """
    url = active_alerts_url()

    # Ensure output directory exists
    if not fs.MRMS_NWS_DIR.exists():
        fs.MRMS_NWS_DIR.mkdir(parents=True, exist_ok=True)

    # Get the registry
    registry = _get_registry()

    io_manager.write_info(f"Downloading active alerts (async) for registry update...")

    # Cleanup old timestamp snapshot files before download
    current_time = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    registry.cleanup_old_timestamps(current_time)

    headers = weather_api_headers()
    # Resolved once, not per chunk.
    chunk_size = download_chunk_size_bytes()

    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp()
        os.close(fd)

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                with open(temp_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        f.write(chunk)

        current_time = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        loop = asyncio.get_running_loop()
        new_count, updated_count, seen_ids = await loop.run_in_executor(
            None,
            _process_nws_file_with_registry,
            temp_path,
            registry,
            current_time,
        )

        # Empty/entirely malformed pulls must not erase still-valid warnings.
        reconciled_removed_count = registry.reconcile_with_active_ids(seen_ids or registry.get_active_ids(), current_time)
        removed_count = registry.cleanup_expired(current_time)
        registry.save()

        total_active = registry.alert_count
        io_manager.write_info(
            "Registry updated (async): "
            f"new={new_count}, updated={updated_count}, "
            f"reconciled={reconciled_removed_count}, expired_removed={removed_count}, "
            f"total_active={total_active}"
        )

    except Exception as e:
        io_manager.write_error(f"Failed to download/process NWS alerts (async): {e}")
        raise e
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except Exception:
                pass


def _process_nws_file_with_registry(
    input_path: str, 
    registry: AlertRegistry, 
    current_time: datetime
) -> Tuple[int, int, Set[str]]:
    """
    Process the raw NWS JSON file and update the registry.
    
    Filters events, applies GeoMapper logic, and adds/updates alerts in registry.
    
    Args:
        input_path: Path to the downloaded NWS JSON file
        registry: AlertRegistry instance to update
        current_time: Current timestamp for tracking
        
    Returns:
        Tuple of (new_count, updated_count, seen_ids)
    """
    new_count = 0
    updated_count = 0
    seen_ids: Set[str] = set()
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
                    continue
                if alert_id:
                    seen_ids.add(alert_id)
                    if is_new:
                        new_count += 1
                    else:
                        updated_count += 1

        return new_count, updated_count, seen_ids
        
    except Exception as e:
        raise e


if __name__ == "__main__":
    # Test block
    fs.initialize_filesystem()
    import asyncio
    asyncio.run(download_alerts_async(datetime.now(timezone.utc)))
