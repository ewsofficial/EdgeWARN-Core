"""Committed GLM inputs require usable payloads and scan alignment."""
from datetime import datetime, timezone

import pytest
import xarray as xr

from common.ingest.mrms.config import GoesIngestSpec
from util.runtime import goes
import util.file as fs

DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize('case', ['valid', 'corrupt', 'missing-variable', 'stale'])
def test_glm_completion(tmp_path, monkeypatch, case):
    monkeypatch.setattr(fs, 'BASE_DIR', tmp_path)
    spec = GoesIngestSpec('GLM-L2-LCFA', tmp_path / 'glm')
    monkeypatch.setattr(goes, 'get_goes_modifiers', lambda: [spec])
    def download(selected, dt, *, cleanup):
        assert cleanup is False
        selected.outdir.mkdir(parents=True, exist_ok=True)
        name = 'OR_GLM-L2-LCFA_merged_20260101-010000.nc' if case == 'stale' else 'OR_GLM-L2-LCFA_merged_20260101-000000.nc'
        path = selected.outdir / name
        if case == 'corrupt':
            path.write_bytes(b'partial')
        else:
            variables = {'flash_lat': ('flash', [35.]), 'flash_lon': ('flash', [-97.])}
            if case != 'missing-variable': variables['flash_energy'] = ('flash', [1.])
            xr.Dataset(variables).to_netcdf(path)
        return [path]
    monkeypatch.setattr(goes, 'download_goes_product', download)
    if case == 'valid':
        first = goes.acquire_glm_inputs_for_scan(DT)[0]
        assert first.record.analysis_time == DT
        assert first.record.validated
        assert first.sha256
        second = goes.acquire_glm_inputs_for_scan(DT)[0]
        assert second.input_id == first.input_id and second.reused
    else:
        with pytest.raises((ValueError, OSError)):
            goes.acquire_glm_inputs_for_scan(DT)
        assert not list(spec.outdir.glob("*.nc"))
