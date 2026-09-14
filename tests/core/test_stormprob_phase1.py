"""Phase 1 StormProb input-collection tests.

Covers the four Phase 1 bullets with small deterministic known-answer cases:
radial geometry convention, versioned feature extraction, observation
readiness gating, and 30-row track tensors.
"""

import math

import pytest

from EdgeWARN.stormprob import features, geometry, records, tracks


def _square_cell():
    centroid = [35.0, 265.0]
    polygon = [
        [34.95, 264.95],
        [34.95, 265.05],
        [35.05, 265.05],
        [35.05, 264.95],
    ]
    return centroid, polygon


def _wind_properties(levels=(850, 700, 500), base=10.0):
    props = {"wind_field": {}}
    for i, level in enumerate(levels):
        props["wind_field"][f"u{level}"] = base + i
        props["wind_field"][f"v{level}"] = -(base + i)
    return props


def _full_properties():
    props = _wind_properties()
    for name in features.IMPORTANT_SCALAR_PROPERTY_FEATURES:
        props.setdefault(name, 1.5)
    props["morphology"] = {
        "aspect_ratio": 1.2,
        "branching_factor": 0,
        "defect_bearing": 45.0,
        "defect_max_depth": 0.5,
        "linearity": 0.8,
        "solidity": 0.9,
    }
    return props


# --- bullet 1: geometry ----------------------------------------------------

class TestRadialGeometry:
    def test_square_known_answer(self):
        centroid, polygon = _square_cell()
        result = geometry.radial_profile_for_cell(centroid, polygon)
        assert result["status"] == "ok"
        assert result["reason"] is None
        east = 0.05 * 111.0 * math.cos(math.radians(35.0))
        north = 0.05 * 111.0
        radii = result["radii_km"]
        assert len(radii) == 64
        assert radii[0] == pytest.approx(east, rel=1e-6)  # ray 0 starts east
        assert radii[16] == pytest.approx(north, rel=1e-6)  # ray 16 points north
        assert radii[8] == pytest.approx(east / math.cos(math.pi / 4), rel=1e-6)
        assert result["area_km2"] == pytest.approx(4 * east * north, rel=1e-9)
        assert result["log_area"] == pytest.approx(math.log(4 * east * north), rel=1e-9)

    def test_rays_start_east_rotate_counterclockwise(self):
        # Right triangle with the right angle at the centroid: only the
        # first-quadrant rays (0..90 deg) intersect.
        centroid = [35.0, 265.0]
        polygon = [[35.0, 265.0], [35.0, 265.1], [35.1, 265.0]]
        result = geometry.radial_profile_for_cell(centroid, polygon)
        assert result["status"] == "ok"
        radii = result["radii_km"]
        assert radii[0] > 0.0  # east along the triangle edge
        assert radii[16] > 0.0  # north along the triangle edge
        assert radii[32] == 0.0  # west: no intersection
        assert radii[48] == 0.0  # south: no intersection

    def test_reject_reasons(self):
        assert geometry.radial_profile_for_cell(None, [[0, 0]])["reason"] \
            == geometry.REASON_NON_FINITE_CENTROID
        assert geometry.radial_profile_for_cell([float("nan"), 0.0], [[0, 0]])["reason"] \
            == geometry.REASON_NON_FINITE_CENTROID
        assert geometry.radial_profile_for_cell([35.0, 265.0], [])["reason"] \
            == geometry.REASON_EMPTY_POLYGON
        assert geometry.radial_profile_for_cell([35.0, 265.0], [[35.0, 265.0]])["reason"] \
            == geometry.REASON_TOO_FEW_POINTS
        assert geometry.radial_profile_for_cell(
            [35.0, 265.0],
            [[35.0, 265.0], [float("nan"), 265.0], [35.1, 265.1]],
        )["reason"] == geometry.REASON_NON_FINITE_VERTEX
        assert geometry.radial_profile_for_cell(
            [35.0, 265.0],
            [[35.0, 265.0], [35.0, 265.0], [35.0, 265.0]],
        )["reason"] == geometry.REASON_ZERO_AREA

    def test_attach_is_additive(self):
        centroid, polygon = _square_cell()
        entry = {"id": 7, "centroid": [35.0, 265.0], "bbox": polygon,
                 "properties": {"morphology": {}}}
        geometry.attach_stormprob_geometry(entry, [35.000123, 265.000456], polygon)
        assert entry["centroid"] == [35.0, 265.0]  # untouched
        assert entry["bbox"] == polygon  # untouched
        stored = entry["stormprob"]["geometry"]
        assert stored["centroid_full"] == pytest.approx([35.000123, 265.000456])
        assert stored["radial"]["status"] == "ok"
        assert len(stored["radial"]["radii_km"]) == 64


# --- bullet 2: feature extractor --------------------------------------------

class TestFeatureExtractor:
    def test_order_contract(self):
        assert len(features.CURRENT_FEATURE_ORDER) == 135
        assert len(features.UNIVERSAL_PROPERTY_FEATURES) == 131
        assert len(features.TRAJECTORY_FEATURE_ORDER) == 16
        assert features.verify_against_manifest()["ok"] is True

    def test_flatten_properties(self):
        flat = features.flatten_properties({"a": 1, "morphology": {"solidity": 0.9}})
        assert flat == {"a": 1, "morphology.solidity": 0.9}

    def test_sentinel_policy(self):
        for bad in (None, "MATCH_ERROR", "PROCESSING_ERROR", float("nan"),
                    float("inf"), True, -999.0, -1000.0):
            value, flag = features.coerce_finite_or_sentinel(bad)
            assert value == -999.0 and flag == "missing-field"
        value, flag = features.coerce_finite_or_sentinel(3.25)
        assert value == pytest.approx(3.25) and flag == "ok"

    def test_initial_wind_mean(self):
        initial = features.predict_initial_wind(_wind_properties(levels=(850,), base=4.0))
        assert initial == {"u": 4.0, "v": -4.0}

    def test_initial_wind_skips_sentinel_levels(self):
        props = {"wind_field": {"u850": -999.0, "v850": -999.0,
                                "u700": 6.0, "v700": 2.0}}
        assert features.predict_initial_wind(props) == {"u": 6.0, "v": 2.0}

    def test_initial_wind_no_usable_pair(self):
        with pytest.raises(features.NoUsableWindPair):
            features.predict_initial_wind({"wind_field": {"u100": 5.0, "v100": 5.0}})
        with pytest.raises(features.NoUsableWindPair):
            features.predict_initial_wind({"wind_field": {}})
        with pytest.raises(features.NoUsableWindPair):
            features.predict_initial_wind({})

    def test_current_vector_shape_and_derived(self):
        entries = [
            {"timestamp": "2022-04-13T20:52:42", "centroid": [32.0, 268.0],
             "properties": _full_properties()},
            {"timestamp": "2022-04-13T20:57:42", "centroid": [32.1, 268.1],
             "properties": _full_properties()},
        ]
        vector, meta = features.build_current_feature_vector(entries, 1)
        assert len(vector) == 135
        assert meta["storm_age_seconds"] == pytest.approx(300.0)
        assert meta["valid_history_length"] == 2
        assert meta["initial_status"] == "ok"
        assert vector[-4] == pytest.approx(meta["initial_u"])
        assert vector[-2] == pytest.approx(300.0)
        assert vector[-1] == 2.0

    def test_source_audit(self):
        from datetime import datetime
        cycle = datetime(2022, 4, 13, 21, 0, 0)
        assert features.audit_source_times(cycle, {"rap": datetime(2022, 4, 13, 20, 0, 0)}) == []
        issues = features.audit_source_times(cycle, {"rap": None})
        assert issues == ["absent-source:rap"]
        issues = features.audit_source_times(cycle, {"mrms": datetime(2022, 4, 13, 21, 5, 0)})
        assert any(i.startswith("future-source:mrms") for i in issues)
        issues = features.audit_source_times(
            cycle, {"rap": datetime(2022, 4, 13, 18, 0, 0)}, rap_max_age_seconds=3600.0)
        assert any(i.startswith("stale-source:rap") for i in issues)

    def test_normalization_vectors(self):
        norm = features.load_normalization()
        env = features.normalize_radial_env([1.0] * 135)
        assert env.shape == (270,)
        assert env[135:] == pytest.approx(0.0)  # no missing flags
        sentineled = [1.0] * 135
        sentineled[0] = -999.0
        out = features.normalize_radial_env(sentineled)
        median = float(norm["radial_env"]["median"][0])
        expected = ((min(max(median, norm["radial_env"]["clip_min"][0]),
                         norm["radial_env"]["clip_max"][0])
                     - norm["radial_env"]["mean"][0]) / norm["radial_env"]["scale"][0])
        assert out[0] == pytest.approx(expected, rel=1e-5)
        assert out[135] == 1.0  # missing flag appended
        assert features.normalize_motion_current([0.0] * 135).shape == (270,)
        assert features.normalize_trajectory([0.0] * 16).shape == (16,)
        assert features.normalize_radial_profile([5.0] * 64).shape == (64,)
        assert features.normalize_radial_stats([[2.0]]).shape == (1, 1)


# --- bullet 3: observation records ------------------------------------------

def _integrated_cell(**overrides):
    centroid, polygon = _square_cell()
    cell = {
        "id": 42,
        "timestamp": "2022-04-13T21:00:00",
        "centroid": [round(centroid[0], 3), round(centroid[1], 3)],
        "bbox": polygon,
        "parent_ids": [],
        "split_from": None,
        "event_type": "ACTIVE",
        "properties": _full_properties(),
    }
    cell.update(overrides)
    return cell


class TestObservationRecords:
    def test_ready_cell(self):
        record = records.build_observation_record(_integrated_cell())
        assert record["schema_version"] == "stormprob-input/v1"
        assert len(record["current_features_raw"]) == 135
        assert record["inference_ready"] is True
        assert record["initial_wind_status"] == "ok"
        assert len(record["units"]) == 135
        assert record["geometry_status"] == "ok"

    def test_missing_weather_fields_stay_ready_with_sentinel(self):
        cell = _integrated_cell()
        cell["properties"]["MUCAPE"] = None
        cell["properties"]["VIL"] = "MATCH_ERROR"
        record = records.build_observation_record(cell)
        assert record["inference_ready"] is True
        assert any(r.startswith("missing-fields:") for r in record["reasons"])
        idx = features.CURRENT_FEATURE_ORDER.index("MUCAPE")
        assert record["current_features_raw"][idx] == -999.0
        assert record["quality"]["MUCAPE"] == "missing-field"

    def test_absent_source_flagged(self):
        cell = _integrated_cell()
        cell["properties"]["wind_field"] = {}
        record = records.build_observation_record(
            cell, source_times={"rap": None, "mrms": None})
        assert "missing-source:rap" in record["reasons"]
        assert record["quality"]["wind_field.u850"] == "missing-source"
        assert record["inference_ready"] is False  # no usable wind pair

    def test_no_wind_pair_not_ready_never_zero_filled(self):
        cell = _integrated_cell()
        cell["properties"]["wind_field"] = {}
        record = records.build_observation_record(cell)
        assert record["inference_ready"] is False
        assert any(r.startswith("no-usable-wind-pair") for r in record["reasons"])
        assert "not-ready:initial-wind" in record["reasons"]
        assert record["initial_wind_mps"] == {"u": -999.0, "v": -999.0}

    def test_corrupt_sample_not_ready(self):
        cell = _integrated_cell(centroid=[float("nan"), float("nan")], bbox=[])
        record = records.build_observation_record(cell)
        assert record["inference_ready"] is False
        assert "not-ready:geometry" in record["reasons"]


# --- bullet 4: track history -------------------------------------------------

def _obs(ts, lat=35.0, lon=265.0, **kw):
    obs = {"timestamp": ts, "centroid": [lat, lon],
           "properties": _full_properties(), "parent_ids": [],
           "split_from": None, "event_type": "ACTIVE"}
    obs.update(kw)
    return obs


class TestTrackBuilder:
    def test_single_row_new_cell_eligible(self):
        out = tracks.track_tensors_for_cell([_obs("2022-04-13T21:00:00")])
        assert out["status"] == "ok"
        tensors = out["tensors"]
        assert tensors["n_valid_rows"] == 1
        assert tensors["history_mask"] == [False] * 29 + [True]
        assert tensors["trajectory_mask"] == [False] * 29 + [True]
        assert tensors["trajectory_sequence"][-1][:14] == [0.0] * 14
        assert tensors["trajectory_sequence"][-1][15] == pytest.approx(1 / 30)
        assert len(tensors["history_sequence"]) == 30
        assert len(tensors["history_sequence"][0]) == 135

    def test_duplicate_timestamp_last_wins(self):
        first = _obs("2022-04-13T21:00:00", lat=35.0)
        second = _obs("2022-04-13T21:00:00", lat=36.0)
        out = tracks.track_tensors_for_cell([first, second])
        assert out["meta"]["duplicates_replaced"] == 1
        assert len(out["rows"]) == 1
        assert out["rows"][0]["centroid"] == [36.0, 265.0]

    def test_nonmonotone_arrival_reordered(self):
        late = _obs("2022-04-13T21:05:00")
        early = _obs("2022-04-13T21:00:00")
        out = tracks.track_tensors_for_cell([late, early])
        assert out["meta"]["reordered"] is True
        assert out["rows"][0]["timestamp"] == "2022-04-13T21:00:00"

    def test_scan_gap_flagged_not_interpolated(self):
        rows = [_obs("2022-04-13T21:00:00"), _obs("2022-04-13T21:38:00")]
        out = tracks.track_tensors_for_cell(rows)
        assert out["rows"][1]["post_gap"] is True
        assert out["rows"][1]["dt_minutes"] == pytest.approx(38.0)
        assert out["meta"]["gap_rows"] == [1]
        # Both rows still commit; no synthetic rows are invented.
        assert out["tensors"]["n_valid_rows"] == 2

    def test_full_30_rows_no_padding(self):
        from datetime import datetime, timedelta
        base = datetime(2022, 4, 13, 21, 0, 0)
        rows = [_obs((base + timedelta(minutes=i)).isoformat(),
                     lat=35.0 + 0.01 * i) for i in range(35)]
        out = tracks.track_tensors_for_cell(rows)
        assert out["tensors"]["history_mask"] == [True] * 30
        assert out["tensors"]["valid_history_length"] == 30

    def test_lineage_passthrough_and_policy(self):
        assert tracks.LINEAGE_POLICY == "per-cell-id-independent"
        child = _obs("2022-04-13T21:00:00", parent_ids=[7],
                     split_from="2022-04-13T20:55:00")
        out = tracks.track_tensors_for_cell([child])
        assert out["rows"][0]["parent_ids"] == [7]
        assert out["rows"][0]["split_from"] == "2022-04-13T20:55:00"
        assert out["meta"]["lineage_policy"] == tracks.LINEAGE_POLICY

    def test_trajectory_two_point_velocity(self):
        out = tracks.track_tensors_for_cell([
            _obs("2022-04-13T21:00:00", lat=35.0, lon=265.0),
            _obs("2022-04-13T21:01:00", lat=35.0, lon=265.01),
        ])
        last = out["tensors"]["trajectory_sequence"][-1]
        expected_dx_km = 0.01 * 111.0 * math.cos(math.radians(35.0))
        assert last[0] == pytest.approx(expected_dx_km, rel=1e-6)
        assert last[1] == pytest.approx(0.0, abs=1e-9)
        assert last[2] == pytest.approx(1.0)
        assert last[3] == pytest.approx(expected_dx_km * 1000.0 / 60.0, rel=1e-6)


# --- pipeline wiring ---------------------------------------------------------

class TestPipelineWiring:
    def test_source_time_resolution(self):
        from EdgeWARN.process.integrate.pipeline import _resolve_stormprob_source_times
        from datetime import datetime, timezone

        assert _resolve_stormprob_source_times(None) == {}

        class FakeRecord:
            def __init__(self, product, family, hour):
                self.product = product
                self.family = family
                self.analysis_time = datetime(2022, 4, 13, hour, 0, tzinfo=timezone.utc)

        class FakeManifest:
            def current_inputs(self):
                return (FakeRecord("RAP", "rap", 20),
                        FakeRecord("ProbSevere", "mrms", 21))

        resolved = _resolve_stormprob_source_times(FakeManifest())
        assert resolved["rap"].hour == 20
        assert resolved["probsevere"].hour == 21
        assert resolved["mrms"].hour == 21

    def test_attach_inputs_failure_isolated(self):
        from EdgeWARN.process.integrate.pipeline import _attach_stormprob_inputs
        cells = [_integrated_cell(), {"id": None, "properties": "broken"}]
        result = _attach_stormprob_inputs(cells, "2022-04-13T21:00:00", None)
        assert result[0]["stormprob"]["observation"]["inference_ready"] is True
        assert result[0]["stormprob"]["feature_schema"] == "stormprob-input/v1"
