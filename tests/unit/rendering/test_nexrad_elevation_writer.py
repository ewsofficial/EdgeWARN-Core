import json
from pathlib import Path

import util.file as fs
from common.ingest.nexrad.models import ElevationGroup, SweepRecord
from common.ingest.nexrad.writer import (
    NexradElevationStore,
    NexradLocalChunkStore,
    build_site_manifest,
    elevation_ar2v_path,
    elevation_dir,
    elevation_netcdf_path,
    elevation_manifest_path,
    runtime_dir,
    runtime_scan_path,
    local_elevation_complete,
    local_scan_elevations_complete,
    prune_elevation_artifacts,
    prune_runtime_volumes,
    site_manifest_path,
    write_elevation_artifacts,
    write_site_manifest,
)


def test_elevation_store_paths(tmp_path):
    fs.initialize_filesystem(tmp_path)
    store = NexradElevationStore()

    elev_dir = store.elevation_dir("KTLH", "0.5")
    assert str(elev_dir).endswith("NEXRAD_Level2/KTLH/0.5")

    nc_path = store.elevation_netcdf_path("KTLH", "0.5", "20260507-150000")
    assert "KTLH_0.5_20260507-150000.nc" in str(nc_path)

    ar2v_path = store.elevation_ar2v_path("KTLH", "0.5", "20260507-150000")
    assert "KTLH_0.5_20260507-150000.ar2v" in str(ar2v_path)

    manifest_path = store.elevation_manifest_path("KTLH", "0.5", "20260507-150000")
    assert "KTLH_0.5_20260507-150000.json" in str(manifest_path)

    runtime = store.runtime_dir("KTLH")
    assert str(runtime).endswith("NEXRAD_Level2/.runtime/KTLH")

    scan_path = store.runtime_scan_path("KTLH", "VOL123")
    assert "KTLH_VOL123.ar2v" in str(scan_path)


def test_elevation_dir_helper(tmp_path):
    fs.initialize_filesystem(tmp_path)
    d = elevation_dir("KDDC", "1.3")
    assert "KDDC" in str(d)
    assert "1.3" in str(d)


def test_elevation_netcdf_path_helper(tmp_path):
    fs.initialize_filesystem(tmp_path)
    p = elevation_netcdf_path("KDDC", "1.3", "20260101-120000")
    assert p.suffix == ".nc"
    assert "KDDC" in str(p)


def test_elevation_paths_normalize_iso_timestamps(tmp_path):
    fs.initialize_filesystem(tmp_path)
    store = NexradElevationStore()

    nc_path = store.elevation_netcdf_path("KTLH", "0.5", "2026-05-20T17:45:24Z")
    ar2v_path = store.elevation_ar2v_path("KTLH", "0.5", "2026-05-20T17:45:24Z")
    manifest_path = store.elevation_manifest_path("KTLH", "0.5", "2026-05-20T17:45:24Z")

    assert nc_path.name == "KTLH_0.5_20260520-174524.nc"
    assert ar2v_path.name == "KTLH_0.5_20260520-174524.ar2v"
    assert manifest_path.name == "KTLH_0.5_20260520-174524.json"


def test_elevation_manifest_path_helper(tmp_path):
    fs.initialize_filesystem(tmp_path)
    p = elevation_manifest_path("KDDC", "1.3", "20260101-120000")
    assert p.suffix == ".json"
    assert "KDDC" in str(p)


def test_runtime_dir_helper(tmp_path):
    fs.initialize_filesystem(tmp_path)
    d = runtime_dir("KTLH")
    assert ".runtime" in str(d)
    assert "KTLH" in str(d)


def test_runtime_scan_path_helper(tmp_path):
    fs.initialize_filesystem(tmp_path)
    p = runtime_scan_path("KTLH", "VOL456")
    assert p.suffix == ".ar2v"
    assert "KTLH" in str(p)
    assert "VOL456" in str(p)


def test_prune_runtime_volumes_keeps_only_requested_volume(tmp_path):
    fs.initialize_filesystem(tmp_path)
    runtime = runtime_dir("KTLH")
    runtime.mkdir(parents=True, exist_ok=True)

    keep_ar2v = runtime / "KTLH_VOL456.ar2v"
    keep_json = runtime / "KTLH_VOL456.json"
    drop_ar2v = runtime / "KTLH_VOL123.ar2v"
    drop_json = runtime / "KTLH_VOL123.json"
    drop_part = runtime / "KTLH_VOL123.ar2v.part"

    for path in (keep_ar2v, keep_json, drop_ar2v, drop_json, drop_part):
        path.write_text("x", encoding="utf-8")

    prune_runtime_volumes("KTLH", "VOL456")

    assert keep_ar2v.exists()
    assert keep_json.exists()
    assert not drop_ar2v.exists()
    assert not drop_json.exists()
    assert not drop_part.exists()


def test_local_elevation_complete_false_when_missing(tmp_path):
    fs.initialize_filesystem(tmp_path)
    assert local_elevation_complete("KTLH", "0.5", "20260507-150000") is False


def test_local_elevation_complete_true_when_exists(tmp_path):
    fs.initialize_filesystem(tmp_path)
    nc_path = elevation_netcdf_path("KTLH", "0.5", "20260507-150000")
    nc_path.parent.mkdir(parents=True, exist_ok=True)
    nc_path.write_bytes(b"data")
    assert local_elevation_complete("KTLH", "0.5", "20260507-150000") is True


def test_local_elevation_complete_true_when_ar2v_exists(tmp_path):
    fs.initialize_filesystem(tmp_path)
    ar2v_path = elevation_ar2v_path("KTLH", "0.5", "20260507-150000")
    ar2v_path.parent.mkdir(parents=True, exist_ok=True)
    ar2v_path.write_bytes(b"data")
    assert local_elevation_complete("KTLH", "0.5", "20260507-150000") is True


def test_local_scan_elevations_complete_all_present(tmp_path):
    fs.initialize_filesystem(tmp_path)
    for elev in ("0.5", "0.9"):
        nc_path = elevation_netcdf_path("KTLH", elev, "20260507-150000")
        nc_path.parent.mkdir(parents=True, exist_ok=True)
        nc_path.write_bytes(b"data")

    required = [("0.5", "20260507-150000"), ("0.9", "20260507-150000")]
    assert local_scan_elevations_complete("KTLH", required) is True


def test_local_scan_elevations_complete_missing_one(tmp_path):
    fs.initialize_filesystem(tmp_path)
    nc_path = elevation_netcdf_path("KTLH", "0.5", "20260507-150000")
    nc_path.parent.mkdir(parents=True, exist_ok=True)
    nc_path.write_bytes(b"data")

    required = [("0.5", "20260507-150000"), ("0.9", "20260507-150000")]
    assert local_scan_elevations_complete("KTLH", required) is False


def test_prune_elevation_artifacts_keeps_recent(tmp_path):
    fs.initialize_filesystem(tmp_path)
    elev_dir = elevation_dir("KTLH", "0.5")
    elev_dir.mkdir(parents=True, exist_ok=True)

    for i in range(8):
        nc_file = elev_dir / f"KTLH_0.5_20260507-15000{i}.nc"
        nc_file.write_bytes(b"data")
        json_file = nc_file.with_suffix(".json")
        json_file.write_text("{}")

    prune_elevation_artifacts("KTLH", "0.5")

    remaining_nc = list(elev_dir.glob("*.nc"))
    assert len(remaining_nc) <= 5


class _FakeRawSweep:
    def __init__(self, group_name, waveform):
        self.group_name = group_name
        self.waveform = waveform
        self.record_ranges = [(0, 6)]


class _FakeRawVolume:
    volume_header = b"header"
    record_buffer = b"record"
    metadata_ranges = []

    def __init__(self, sweeps):
        self.sweeps = sweeps


def _write_test_artifact(site, volume_id, scan_timestamp, elevation, member_specs):
    group = ElevationGroup(
        elevation_id=elevation,
        canonical_angle_deg=float(elevation),
        members=[
            SweepRecord(
                index=index,
                group_name=group_name,
                fixed_angle=float(elevation),
                waveform=waveform,
                timestamp=timestamp,
                azimuth_count=360,
            )
            for index, (group_name, waveform, timestamp) in enumerate(member_specs)
        ],
        waveforms_present={waveform for _group_name, waveform, _timestamp in member_specs},
        first_sweep_index=0,
        last_sweep_index=len(member_specs) - 1,
        first_timestamp=member_specs[0][2],
        last_timestamp=member_specs[-1][2],
        complete=True,
    )
    source = _FakeRawVolume([_FakeRawSweep(group_name, waveform) for group_name, waveform, _timestamp in member_specs])
    write_elevation_artifacts(
        group,
        source,
        site=site,
        volume_id=volume_id,
        scan_timestamp=scan_timestamp,
        download_started_at="2026-05-07T15:00:00Z",
        elevation_label=elevation,
        elevation_timestamp=member_specs[0][2],
    )


def test_write_elevation_manifest_includes_member_sweep_timestamps(tmp_path):
    fs.initialize_filesystem(tmp_path)

    _write_test_artifact(
        "KTLH",
        "VOL-001",
        "20260507-150000",
        "0.5",
        [
            ("sweep_0", "contiguous_surveillance", "20260507-150001"),
            ("sweep_1", "batch", "20260507-150004"),
        ],
    )

    manifest = json.loads(
        elevation_manifest_path("KTLH", "0.5", "20260507-150001").read_text(encoding="utf-8")
    )

    assert manifest["volume_timestamp"] == "20260507-150000"
    assert manifest["download_started_at"] == "2026-05-07T15:00:00Z"
    assert manifest["first_sweep_timestamp"] == "20260507-150001"
    assert manifest["last_sweep_timestamp"] == "20260507-150004"
    assert manifest["file_written_at"].endswith("Z")
    assert manifest["member_sweeps"] == [
        {
            "group_name": "sweep_0",
            "sweep_index": 0,
            "fixed_angle": 0.5,
            "elevation_number": None,
            "waveform": "contiguous_surveillance",
            "timestamp": "20260507-150001",
        },
        {
            "group_name": "sweep_1",
            "sweep_index": 1,
            "fixed_angle": 0.5,
            "elevation_number": None,
            "waveform": "batch",
            "timestamp": "20260507-150004",
        },
    ]


def test_site_manifest_keeps_latest_two_volumes_by_volume_timestamp(tmp_path):
    fs.initialize_filesystem(tmp_path)

    volume_specs = [
        ("VOL-F", "20260507-155000"),
        ("VOL-C", "20260507-152000"),
        ("VOL-A", "20260507-150000"),
        ("VOL-D", "20260507-153000"),
        ("VOL-B", "20260507-151000"),
        ("VOL-E", "20260507-154000"),
    ]
    for volume_id, volume_timestamp in volume_specs:
        _write_test_artifact(
            "KTLH",
            volume_id,
            volume_timestamp,
            "0.5",
            [
                (f"{volume_id}_0", "contiguous_surveillance", f"{volume_timestamp[:-2]}01"),
                (f"{volume_id}_1", "batch", f"{volume_timestamp[:-2]}04"),
            ],
        )
        _write_test_artifact(
            "KTLH",
            volume_id,
            volume_timestamp,
            "0.9",
            [
                (f"{volume_id}_2", "batch", f"{volume_timestamp[:-2]}06"),
            ],
        )

    manifest_path = write_site_manifest("KTLH")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest_path == site_manifest_path("KTLH")
    assert [volume["volume_id"] for volume in manifest["volumes"]] == ["VOL-F", "VOL-E"]
    assert [volume["volume_timestamp"] for volume in manifest["volumes"]] == [
        "20260507-155000",
        "20260507-154000",
    ]
    assert manifest["volumes"][0]["download_started_at"] == "2026-05-07T15:00:00Z"
    assert manifest["volumes"][0]["sweeps"] == [
        {
            "sweep_index": 0,
            "group_name": "VOL-F_0",
            "elevation": 0.5,
            "timestamp": "20260507-155001",
            "waveform": "contiguous_surveillance",
        },
        {
            "sweep_index": 0,
            "group_name": "VOL-F_2",
            "elevation": 0.9,
            "timestamp": "20260507-155006",
            "waveform": "batch",
        },
        {
            "sweep_index": 1,
            "group_name": "VOL-F_1",
            "elevation": 0.5,
            "timestamp": "20260507-155004",
            "waveform": "batch",
        },
    ]

    pruned_sidecars = sorted(path.name for path in elevation_dir("KTLH", "0.5").glob("*.json"))
    assert pruned_sidecars == [
        "KTLH_0.5_20260507-154001.json",
        "KTLH_0.5_20260507-155001.json",
    ]


def test_build_site_manifest_ignores_runtime_and_scan_dirs(tmp_path):
    fs.initialize_filesystem(tmp_path)
    site_dir = Path(fs.NEXRAD_LEVEL2_DIR) / "KTLH"
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "20260507-150000").mkdir()
    (site_dir / "misc").mkdir()
    (Path(fs.NEXRAD_LEVEL2_DIR) / ".runtime" / "KTLH").mkdir(parents=True, exist_ok=True)
    (site_dir / "manifest.json").write_text("{}", encoding="utf-8")

    _write_test_artifact(
        "KTLH",
        "VOL-001",
        "20260507-150000",
        "0.5",
        [("sweep_0", "batch", "20260507-150001")],
    )

    manifest = build_site_manifest("KTLH")
    assert [volume["volume_id"] for volume in manifest["volumes"]] == ["VOL-001"]
    assert manifest["volumes"][0]["sweeps"] == [
        {
            "sweep_index": 0,
            "group_name": "sweep_0",
            "elevation": 0.5,
            "timestamp": "20260507-150001",
            "waveform": "batch",
        }
    ]


def test_site_manifest_keeps_duplicate_same_bin_sweeps_as_raw_entries(tmp_path):
    fs.initialize_filesystem(tmp_path)

    _write_test_artifact(
        "KTLH",
        "VOL-001",
        "20260507-150000",
        "0.5",
        [
            ("sweep_0", "contiguous_surveillance", "20260507-150001"),
            ("sweep_1", "batch", "20260507-150004"),
        ],
    )
    _write_test_artifact(
        "KTLH",
        "VOL-001",
        "20260507-150000",
        "0.5",
        [
            ("sweep_8", "contiguous_surveillance", "20260507-150031"),
            ("sweep_9", "batch", "20260507-150034"),
        ],
    )

    manifest = build_site_manifest("KTLH")
    assert manifest["volumes"][0]["sweeps"] == [
        {
            "sweep_index": 0,
            "group_name": "sweep_0",
            "elevation": 0.5,
            "timestamp": "20260507-150001",
            "waveform": "contiguous_surveillance",
        },
        {
            "sweep_index": 0,
            "group_name": "sweep_8",
            "elevation": 0.5,
            "timestamp": "20260507-150031",
            "waveform": "contiguous_surveillance",
        },
        {
            "sweep_index": 1,
            "group_name": "sweep_1",
            "elevation": 0.5,
            "timestamp": "20260507-150004",
            "waveform": "batch",
        },
        {
            "sweep_index": 1,
            "group_name": "sweep_9",
            "elevation": 0.5,
            "timestamp": "20260507-150034",
            "waveform": "batch",
        },
    ]


def test_prune_station_scan_dirs_ignores_elevation_directories(tmp_path):
    fs.initialize_filesystem(tmp_path)
    site_dir = Path(fs.NEXRAD_LEVEL2_DIR) / "KTLH"
    site_dir.mkdir(parents=True, exist_ok=True)

    for scan_dir_name in ("20260507-150000", "20260507-151000", "20260507-152000", "20260507-153000"):
        (site_dir / scan_dir_name).mkdir()
    for elevation_name in ("0.5", "0.9", "1.3"):
        (site_dir / elevation_name).mkdir()

    NexradLocalChunkStore().prune_station_scan_dirs("KTLH", "20260507-153000")

    assert not (site_dir / "20260507-150000").exists()
    assert (site_dir / "20260507-151000").exists()
    assert (site_dir / "20260507-152000").exists()
    assert (site_dir / "20260507-153000").exists()
    assert (site_dir / "0.5").exists()
    assert (site_dir / "0.9").exists()
    assert (site_dir / "1.3").exists()
