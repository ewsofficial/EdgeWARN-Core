"""Offline weather-data spine from decode through durable publication."""

from __future__ import annotations

import base64
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xarray as xr

import util.file as fs
from common.ingest.manifest import CycleInputManifest, StagedInput
from EdgeWARN.process.detect.config import DetectionConfig


detection_main = importlib.import_module("EdgeWARN.process.detect.main")
integration_main = importlib.import_module("EdgeWARN.process.integrate.pipeline")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "weather"


def _read(name):
    return json.loads((FIXTURES / name).read_text())


def _write_grid(spec_name, destination, timestamp, *, value_override=None):
    spec = _read(spec_name)
    lat = spec["coordinates"]["latitude"]
    lon = spec["coordinates"]["longitude"]
    values = np.full(
        (lat["count"], lon["count"]),
        spec["background"] if value_override is None else value_override,
        dtype=np.float32,
    )
    if value_override is None:
        for patch in spec["patches"]:
            values[
                slice(*patch["rows"]), slice(*patch["columns"])
            ] = patch["value"]
    dataset = xr.Dataset(
        {"unknown": (("latitude", "longitude"), values)},
        coords={
            "latitude": np.linspace(lat["start"], lat["stop"], lat["count"]),
            "longitude": np.linspace(lon["start"], lon["stop"], lon["count"]),
        },
    )
    dataset["unknown"].attrs["units"] = spec["units"]
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"MRMS_{spec['product']}_{timestamp}.nc"
    dataset.to_netcdf(path)
    return path


def _write_glm(destination, timestamp):
    fixture = _read("glm.json")
    flashes = fixture["flashes"]
    dataset = xr.Dataset({
        "flash_lat": ("number_of_flashes", [row["latitude"] for row in flashes]),
        "flash_lon": ("number_of_flashes", [row["longitude"] for row in flashes]),
        "flash_energy": ("number_of_flashes", [row["energy"] for row in flashes]),
    })
    dataset["flash_energy"].attrs["units"] = fixture["units"]["flash_energy"]
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"OR_GLM-L2-LCFA_s20260791800000_{timestamp}.nc"
    dataset.to_netcdf(path)
    return path


def _write_probsevere(destination, timestamp, *, probability=None):
    payload = _read("probsevere.json")
    if probability is not None:
        payload["features"][0]["properties"]["ProbSevere"] = str(probability)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"MRMS_ProbSevere_{timestamp}.json"
    path.write_text(json.dumps(payload))
    return path


def _write_rap(destination, timestamp):
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"RAP_{timestamp}.grib2"
    path.write_bytes(base64.b64decode((FIXTURES / "rap.grib2.b64").read_text()))
    return path


def _record(product, path, cycle_time, family="mrms"):
    return StagedInput(
        product=product,
        path=str(path),
        analysis_time=cycle_time,
        source="sanitized-fixture",
        family=family,
    )


def _cycle_inputs(timestamp, cycle_time, *, newer_probability=None):
    radar = _write_grid("mrms_reflectivity.json", fs.MRMS_COMPOSITE_DIR, timestamp)
    precip = _write_grid("mrms_precipitation_type.json", fs.MRMS_PRECIPTYP_DIR, timestamp)
    probsevere = _write_probsevere(fs.MRMS_PROBSEVERE_DIR, timestamp)
    stats = _write_grid(
        "mrms_reflectivity.json", fs.MRMS_ECHOTOP30_DIR, timestamp,
        value_override=10.0,
    )
    glm = _write_glm(fs.GOES_GLM_DIR, timestamp)
    rap = _write_rap(fs.RAP_DIR, timestamp)

    if newer_probability is not None:
        _write_probsevere(fs.MRMS_PROBSEVERE_DIR, "20260317-200400", probability=newer_probability)

    manifest = CycleInputManifest(cycle_time=cycle_time, inputs=(
        _record("ProbSevere", probsevere, cycle_time),
        _record("EchoTop30", stats, cycle_time),
        _record("GLM", glm, cycle_time, "goes"),
        _record("RAP", rap, cycle_time, "rap"),
    ))
    return radar, precip, probsevere, manifest


def test_two_connected_cycles_decode_enrich_publish_and_reopen():
    config = DetectionConfig.from_yaml()
    first_time = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
    radar_1, precip_1, ps_1, manifest_1 = _cycle_inputs(
        "20260317-200000", first_time, newer_probability=5,
    )

    snapshot_1, _ = detection_main.main(
        str(radar_1), None, str(ps_1), None, str(precip_1), None,
        (34.0, 36.0), (262.0, 264.0), config,
        cleanup_stormcells=False,
    )
    integration_main.main(
        snapshot_1,
        remove_old_cells=False,
        disable_ctam_modules=True,
        input_manifest=manifest_1,
    )

    published_1 = json.loads(snapshot_1.read_text())
    assert [cell["id"] for cell in published_1["features"]] == [17]
    properties = published_1["features"][0]["properties"]
    assert properties["ProbSevere"] == 82.0
    assert properties["maxEchoTop30"] == 10.0
    assert properties["GLM_FLASH_COUNT"] == 2
    assert properties["GLM_TOTAL_ENERGY"] == 150.0
    assert properties["wind_field"]["u850"] == 12.0
    assert properties["temp_2m"] == 26.85

    storm_index = json.loads((fs.STORMCELL_DIR / "stormcell_index.json").read_text())
    cell_index = json.loads((fs.CELL_DIR / "cell_index.json").read_text())
    assert storm_index["timestamps"] == ["20260317-200000"]
    assert cell_index["cellIds"] == [17]
    assert len(json.loads((fs.CELL_DIR / "17.json").read_text())) == 1

    second_time = datetime(2026, 3, 17, 20, 2, tzinfo=timezone.utc)
    radar_2, precip_2, ps_2, manifest_2 = _cycle_inputs(
        "20260317-200200", second_time,
    )
    snapshot_2, _ = detection_main.main(
        str(radar_1), str(radar_2), str(ps_1), str(ps_2),
        str(precip_1), str(precip_2),
        (34.0, 36.0), (262.0, 264.0), config,
        cleanup_stormcells=False,
    )
    integration_main.main(
        snapshot_2,
        remove_old_cells=False,
        disable_ctam_modules=True,
        input_manifest=manifest_2,
    )

    published_2 = json.loads(snapshot_2.read_text())
    assert [cell["id"] for cell in published_2["features"]] == [17]
    assert published_2["features"][0]["modules"]["StormCast"]["status"] == "success"
    history = json.loads((fs.CELL_DIR / "17.json").read_text())
    assert [entry["timestamp"] for entry in history] == [
        "2026-03-17T20:00:00", "2026-03-17T20:02:00",
    ]


def test_malformed_required_raster_has_explicit_empty_detection(tmp_path):
    malformed = tmp_path / "MRMS_MergedReflectivityQC_3D_20260317-200000.nc"
    malformed.write_bytes(b"not-netcdf")

    result = detection_main.detect_cells(
        str(malformed), None, None, detection_main.io_manager,
        34.0, 36.0, 262.0, 264.0,
        detection_config=DetectionConfig.from_yaml(),
    )

    assert result == []
