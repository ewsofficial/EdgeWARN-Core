"""Offline Phase 8 transport fixture; run with PYTHONPATH=src in EdgeWARN.

Uses real acquisition, validation, atomic publication and coordinator. Only remote
listing/transfer are replaced. Each replay runs in a fresh Python process.
"""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock

DT = datetime(2026, 1, 1, tzinfo=timezone.utc)

def grib2_message():
    """One real decodable GRIB2 message; acquisition decode-validates payloads."""
    fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/weather/rap.grib2.b64'
    data = base64.b64decode(fixture.read_text())
    return data[:int.from_bytes(data[8:16], 'big')]

async def qualify(base, short, replay):
    from common.config.loader import load_config
    from common.ingest.mrms import acquisition as a
    from common.ingest.mrms.registry import build_registry
    from common.pipeline import coordinator as c
    from common.ingest.mrms.core_contract import PROTECTED_IDS
    settings = json.loads(build_registry(load_config('ingest')['mrms'], base).normalized_config_json)
    if short:
        settings['downloads']['optional_timeout_seconds'] = 0.12
    registry = build_registry(settings, base)
    budget = a.budget_for(registry)
    callbacks = {}
    outcomes = []
    started = time.monotonic()
    @asynccontextmanager
    async def s3(*args):
        yield MagicMock()
    async def lookup(self, prefix, *args, **kwargs):
        product = prefix.split('/')[1] if prefix.startswith('CONUS/') else 'ProbSevere'
        name = (f'MRMS_{product}_20260101-000000.grib2.gz' if product != 'ProbSevere'
                else 'MRMS_PROBSEVERE_20260101_000000.json')
        return [(name, DT)]
    async def https(*args, **kwargs):
        return []
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected sync fallback in offline qualification")
    async def fetch(s3, session, transport, locator, path, chunk, deadline):
        protected = any(product in locator for product in PROTECTED_IDS) or 'PROBSEVERE' in locator
        await asyncio.sleep(0.01 if protected else (1 if short else 0.05))
        if 'PROBSEVERE' in locator:
            payload = json.dumps({'type':'FeatureCollection','features':[]}).encode()
        else:
            payload = gzip.compress(grib2_message())
        path.write_bytes(payload)
    async def detection(dt, max_entries, **kwargs):
        batch = await a.acquire_batch(registry,dt,max_entries,list(PROTECTED_IDS),MagicMock())
        if batch.failed: print([r for r in batch.product_results if r.product in PROTECTED_IDS],file=sys.stderr)
        return batch
    async def optional(dt, max_entries, **kwargs):
        batch = await a.acquire_batch(registry,dt,max_entries,[p.product_id for p in registry.products if not p.protected],MagicMock())
        outcomes.extend(r.status for r in batch.product_results if r.product not in PROTECTED_IDS)
        return batch
    def callback(name):
        return lambda state: callbacks.setdefault(name,time.monotonic()-started)
    with patch.object(a,'_async_s3',s3), patch.object(a.AsyncFileFinder,'async_lookup_files',lookup), patch.object(a,'_fetch',fetch), patch.object(a.HttpsFileFinder,'find_files',https), patch.object(c.mrms_ingest,'download_detection_files',forbidden), patch.object(c.mrms_ingest,'get_registry',lambda:registry), patch.object(c.mrms_ingest,'get_detection_modifiers',lambda:[p.source_modifier for p in registry.for_phase('detection')]), patch.object(c.mrms_ingest,'download_detection_files_async',detection), patch.object(c.mrms_ingest,'download_integration_files_async',optional):
        state = await c.run_staged_ingest_cycle(DT,lambda msg:print(msg,file=sys.stderr),include_goes=False,include_rap=False,
            on_detection_ready=callback('detection'),on_ewmrs_mrms_ready=callback('ewmrs'),
            on_edgewarn_integration_ready=callback('integration'))
    elapsed = time.monotonic()-started
    assert outcomes.count("failed" if short else "ready") == 18
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert state.detection_inputs_ready and state.optional_inputs_complete
    assert budget.active == budget.waiting == 0 and not pending
    assert not any(t.name.startswith('mrms') for t in threading.enumerate())
    assert budget.limit == settings["downloads"]["max_concurrency"] == 8
    assert budget.peak_active <= budget.limit and budget.peak_queued <= 2 * budget.limit
    staging = base/'state/mrms/staging'
    assert not list(staging.iterdir())
    if short and not replay:
        assert max(callbacks.values()) < settings['downloads']['optional_timeout_seconds']
        assert elapsed > max(callbacks.values())
    return dict(schema_version=load_config("ingest")["schema_version"],registry_fingerprint=registry.fingerprint,
        callback_seconds=callbacks,optional_drain_seconds=elapsed-max(callbacks.values()),
        total_cycle_seconds=elapsed,peak_active=budget.peak_active,peak_queued=budget.peak_queued,
        active_after=budget.active,queued_after=budget.waiting,pending_tasks_after=len(pending),
        published_files=len(list((base/'data').rglob('*.*'))),
        publication_digests={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (base/'data').rglob('*') if p.is_file()},
        optional_failed=outcomes.count('failed'),replay=replay,
        max_concurrency=budget.limit,optional_timeout_seconds=settings['downloads']['optional_timeout_seconds'])

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child-base',type=Path)
    parser.add_argument('--short',action='store_true')
    parser.add_argument('--replay',action='store_true')
    args=parser.parse_args()
    if args.child_base:
        print(json.dumps(asyncio.run(qualify(args.child_base,args.short,args.replay))))
        return
    results={}
    with tempfile.TemporaryDirectory(prefix='mrms-release-') as temporary:
        for short in (False,True):
            base=Path(temporary)/('short' if short else 'defaults')
            base.mkdir()
            measurements=[]
            for replay in (False,True):
                command=[sys.executable,__file__,'--child-base',str(base)]
                if short: command.append('--short')
                if replay: command.append('--replay')
                result=subprocess.run(command,capture_output=True,text=True,timeout=60)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                measurements.append(json.loads(result.stdout.strip().splitlines()[-1]))
            assert measurements[0]['publication_digests']==measurements[1]['publication_digests']
            for measurement in measurements:
                del measurement['publication_digests']
            results['short_deadline' if short else 'shipped_defaults']=measurements
    print(json.dumps(results,indent=2))

if __name__=='__main__': main()
