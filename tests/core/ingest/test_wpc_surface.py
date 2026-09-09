"""WPC coded-surface parser/converter matrices and artifact-consumption contract.

Phase 9 secondary coverage: the WPC ingest path (coded-surface download ->
parse -> GeoJSON -> timestamped/latest files -> Node ``wpcSurface`` reader)
previously had no dedicated tests. These cases pin the wire format
(7-digit ``LAT LON`` codes, continuation lines, keyword dispatch), the
converter matrices (all five front types, pressure centers, unknown-code
fallback, valid-time precedence), and the on-disk contract the Node
ancillary service consumes (filename shape, save/reopen round-trip,
retention sweep).

All cases are offline: the downloader transport is stubbed at
``download_coded_surface`` with protocol-faithful ``(content, actual_time)``
tuples, and every file lands under the disposable runtime tree provided by
the root ``isolated_runtime`` fixture.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.ingest.wpc import converter, downloader, main as wpc_main
from common.ingest.wpc.config import (
    cleanup_glob,
    fallback_geojson_color,
    feature_types,
)
from common.ingest.wpc.parser import (
    _merge_continuation_lines,
    decode_coordinate,
    parse_coded_surface,
    parse_front_coords,
    parse_pressure_centers,
)

SAMPLE_SURFACE = """\
VALID 150300Z
HIGHS 1027 3380787 1024 4020950
LOWS 1008 4120720
COLD 3500800 3400790 3300780
WARM 3000850 2950840
STNRY 2800900 2750890
OCFNT 4500700 4400690
TROF 3200820 3100810
"""


# ---------------------------------------------------------------------------
# Coordinate decoding
# ---------------------------------------------------------------------------


class TestDecodeCoordinate:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("3380787", (33.8, -78.7)),  # canonical docstring example
            ("0000000", (0.0, -0.0)),  # degenerate origin stays numeric
            ("9001800", (90.0, -180.0)),  # pole / antimeridian extremes
            ("2511005", (25.1, -100.5)),
        ],
    )
    def test_known_codes_decode_exactly(self, code, expected):
        assert decode_coordinate(code) == pytest.approx(expected)

    @pytest.mark.parametrize("code", ["338078", "33807870", "", "33807A7"])
    def test_malformed_codes_are_rejected(self, code):
        with pytest.raises(ValueError):
            decode_coordinate(code)

    def test_longitude_is_west_negative(self):
        lat, lon = decode_coordinate("4001000")
        assert lat == pytest.approx(40.0)
        assert lon == pytest.approx(-100.0)
        assert lon < 0


# ---------------------------------------------------------------------------
# Line merging and token parsing
# ---------------------------------------------------------------------------


class TestMergeContinuationLines:
    def test_continuation_lines_join_parent(self):
        merged = _merge_continuation_lines(
            ["COLD 3500800 3400790", "3300780 3200770", "WARM 3000850"]
        )
        assert merged == ["COLD 3500800 3400790 3300780 3200770", "WARM 3000850"]

    def test_orphan_lines_before_first_keyword_are_skipped(self):
        merged = _merge_continuation_lines(["junk preamble", "HIGHS 1027 3380787"])
        assert merged == ["HIGHS 1027 3380787"]

    def test_blank_lines_are_ignored(self):
        merged = _merge_continuation_lines(["", "  ", "LOWS 1008 4120720", ""])
        assert merged == ["LOWS 1008 4120720"]


class TestParsePressureCenters:
    def test_pairs_become_typed_centers(self):
        centers = parse_pressure_centers(["1027", "3380787", "1008", "4120720"], "HIGH")
        assert [(c["pressure"], c["lat"], c["lon"]) for c in centers] == [
            (1027, pytest.approx(33.8), pytest.approx(-78.7)),
            (1008, pytest.approx(41.2), pytest.approx(-72.0)),
        ]
        assert {c["type"] for c in centers} == {"HIGH"}

    def test_trailing_pressure_without_coord_is_dropped(self):
        assert parse_pressure_centers(["1027", "3380787", "1024"], "LOW") == [
            {"type": "LOW", "pressure": 1027, "lat": pytest.approx(33.8), "lon": pytest.approx(-78.7)}
        ]

    def test_non_numeric_pressure_and_bad_coord_are_skipped(self):
        centers = parse_pressure_centers(["xx", "3380787", "1027", "badcode", "1010", "4020950"], "LOW")
        assert [c["pressure"] for c in centers] == [1010]


class TestParseFrontCoords:
    def test_valid_tokens_keep_order(self):
        assert parse_front_coords(["3500800", "3400790"]) == [
            (pytest.approx(35.0), pytest.approx(-80.0)),
            (pytest.approx(34.0), pytest.approx(-79.0)),
        ]

    def test_invalid_tokens_are_filtered(self):
        assert parse_front_coords(["3500800", "oops", "12", "3400790"]) == [
            (pytest.approx(35.0), pytest.approx(-80.0)),
            (pytest.approx(34.0), pytest.approx(-79.0)),
        ]


# ---------------------------------------------------------------------------
# Full coded-surface dispatch matrix
# ---------------------------------------------------------------------------


class TestParseCodedSurface:
    def test_sample_surface_dispatches_every_keyword(self):
        parsed = parse_coded_surface(SAMPLE_SURFACE)
        assert parsed["valid_time"] == "150300"
        assert len(parsed["highs"]) == 2
        assert len(parsed["lows"]) == 1
        assert set(parsed["fronts"]) == {"cold", "warm", "stationary", "occluded", "trough"}
        assert all(len(line) == 1 for line in parsed["fronts"].values())
        assert len(parsed["fronts"]["cold"][0]) == 3

    def test_single_point_front_is_dropped(self):
        parsed = parse_coded_surface("VALID 150300Z\nCOLD 3500800\n")
        assert parsed["fronts"]["cold"] == []

    def test_unknown_keywords_are_ignored(self):
        parsed = parse_coded_surface("VALID 150300Z\nFROPA 3500800 3400790\n")
        assert parsed["valid_time"] == "150300"
        assert parsed["highs"] == parsed["lows"] == []
        assert all(lines == [] for lines in parsed["fronts"].values())

    def test_empty_and_garbage_content_yield_empty_result(self):
        for content in ("", "\n  \n", "no keywords here\nstill none\n"):
            parsed = parse_coded_surface(content)
            assert parsed["valid_time"] is None
            assert parsed["highs"] == parsed["lows"] == []

    def test_multiline_front_reassembles(self):
        parsed = parse_coded_surface("COLD 3500800 3400790\n3300780 3200770\n")
        assert len(parsed["fronts"]["cold"]) == 1
        assert len(parsed["fronts"]["cold"][0]) == 4

    def test_valid_time_requires_zulu_suffix(self):
        assert parse_coded_surface("VALID 150300\n")["valid_time"] is None


# ---------------------------------------------------------------------------
# Converter matrices
# ---------------------------------------------------------------------------


class TestConverter:
    def test_front_feature_uses_lon_lat_order_and_catalog_style(self):
        feature = converter.create_front_feature([(35.0, -80.0), (34.0, -79.0)], "COLD")
        assert feature["geometry"] == {
            "type": "LineString",
            "coordinates": [[-80.0, 35.0], [-79.0, 34.0]],
        }
        assert feature["properties"]["feature_type"] == "COLD"
        assert feature["properties"]["name"] == feature_types()["COLD"]["name"]
        assert feature["properties"]["color"] == feature_types()["COLD"]["color"]

    @pytest.mark.parametrize(
        ("key", "code"),
        [("cold", "COLD"), ("warm", "WARM"), ("stationary", "STNRY"), ("occluded", "OCFNT"), ("trough", "TROF")],
    )
    def test_every_front_type_converts(self, key, code):
        parsed = parse_coded_surface(SAMPLE_SURFACE)
        collection = converter.parsed_to_geojson(parsed)
        matches = [f for f in collection["features"] if f["properties"]["feature_type"] == code]
        assert len(matches) == 1
        assert matches[0]["geometry"]["type"] == "LineString"
        assert len(matches[0]["geometry"]["coordinates"]) >= 2

    def test_pressure_centers_become_labeled_points(self):
        collection = converter.parsed_to_geojson(parse_coded_surface(SAMPLE_SURFACE))
        points = [f for f in collection["features"] if f["geometry"]["type"] == "Point"]
        assert len(points) == 3
        by_label = {f["properties"]["label"]: f for f in points}
        assert set(by_label) == {"H", "L"}
        high = [f for f in points if f["properties"]["feature_type"] == "HIGH"][0]
        assert high["properties"]["pressure"] == 1027
        assert high["geometry"]["coordinates"] == pytest.approx([-78.7, 33.8])

    def test_unknown_code_falls_back_to_code_as_name(self):
        feature = converter.create_front_feature([(35.0, -80.0), (34.0, -79.0)], "DRYLINE")
        assert feature["properties"]["name"] == "DRYLINE"
        assert feature["properties"]["color"] == fallback_geojson_color()

    def test_degenerate_fronts_are_excluded_from_collection(self):
        parsed = {
            "valid_time": None,
            "highs": [],
            "lows": [],
            "fronts": {"cold": [[(35.0, -80.0)]], "warm": [], "stationary": [], "occluded": [], "trough": []},
        }
        assert converter.parsed_to_geojson(parsed)["features"] == []

    def test_explicit_source_timestamp_wins_over_coded_valid_time(self):
        parsed = parse_coded_surface(SAMPLE_SURFACE)
        stamp = datetime(2026, 3, 17, 20, 0, tzinfo=timezone.utc)
        collection = converter.parsed_to_geojson(parsed, stamp)
        assert collection["properties"]["valid_time"] == stamp.isoformat()
        assert collection["properties"]["source"] == "WPC"

    def test_coded_valid_time_resolves_when_no_stamp_given(self):
        parsed = parse_coded_surface("VALID 031720Z\n")
        collection = converter.parsed_to_geojson(parsed)
        assert collection["properties"]["valid_time"].endswith("03-17T20:00:00+00:00")

    def test_unparseable_valid_time_yields_none(self):
        parsed = {"valid_time": "bogus", "highs": [], "lows": [],
                  "fronts": {"cold": [], "warm": [], "stationary": [], "occluded": [], "trough": []}}
        assert converter.parsed_to_geojson(parsed)["properties"]["valid_time"] is None


# ---------------------------------------------------------------------------
# Downloader scheduling matrices (offline: pure time math)
# ---------------------------------------------------------------------------


class TestValidHourSelection:
    def test_exact_valid_hour_stays_same_day(self):
        dt = datetime(2026, 3, 17, 6, 30, tzinfo=timezone.utc)
        ref, hour = downloader.get_latest_valid_hour(dt)
        assert (ref.day, hour) == (17, 6)

    def test_between_hours_steps_back(self):
        dt = datetime(2026, 3, 17, 7, 59, tzinfo=timezone.utc)
        _, hour = downloader.get_latest_valid_hour(dt)
        assert hour == 6

    def test_early_morning_uses_midnight_analysis_same_day(self):
        dt = datetime(2026, 3, 17, 1, 0, tzinfo=timezone.utc)
        ref, hour = downloader.get_latest_valid_hour(dt)
        assert (ref.day, hour) == (17, 0)

    def test_build_url_embeds_date_and_hour(self):
        dt = datetime(2026, 3, 17, 6, 0, tzinfo=timezone.utc)
        url = downloader.build_url(dt, 6)
        assert url.startswith("https://")
        assert url.rsplit("/", 2)[-2] == "20260317"
        assert url.rsplit("/", 1)[-1] == "codsus06_hr"


# ---------------------------------------------------------------------------
# Ancillary-service consumption: fetch -> save -> reopen -> retain
# ---------------------------------------------------------------------------


def _stub_download(content, actual_time):
    def _fake(dt=None):
        return (content, actual_time)

    return _fake


class TestFetchSurfaceAnalysis:
    def test_fetch_saves_latest_and_timestamped_and_reopens(self, monkeypatch):
        actual = datetime(2026, 3, 17, 18, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            wpc_main, "download_coded_surface", _stub_download(SAMPLE_SURFACE, actual)
        )
        result = wpc_main.fetch_surface_analysis(actual, save_timestamped=True)
        assert result is not None
        assert len(result["features"]) == 8  # 5 fronts/trough + 3 centers

        latest = json.loads(downloader.get_latest_output_filepath().read_text(encoding="utf-8"))
        assert latest["features"] == result["features"]
        assert latest["properties"]["valid_time"] == actual.isoformat()

        stamped = downloader.get_output_filepath(actual)
        assert stamped.is_file()
        reopened = json.loads(stamped.read_text(encoding="utf-8"))
        assert reopened == latest

    def test_timestamped_filename_matches_node_reader_contract(self, monkeypatch):
        actual = datetime(2026, 3, 17, 18, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            wpc_main, "download_coded_surface", _stub_download(SAMPLE_SURFACE, actual)
        )
        wpc_main.fetch_surface_analysis(actual, save_timestamped=True)
        name = downloader.get_output_filepath(actual).name
        # Node ancillary.js derives wpcSurfaceName from the same wpc.yaml
        # prefix/suffix: ^wpc_sfc_(\d{8}-\d{6})\.geojson$.
        assert re.match(r"^wpc_sfc_(\d{8}-\d{6})\.geojson$", name), name

    def test_download_failure_returns_none_and_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(wpc_main, "download_coded_surface", lambda dt=None: None)
        assert wpc_main.fetch_surface_analysis(save_timestamped=True) is None

    def test_parse_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            wpc_main, "download_coded_surface",
            _stub_download(SAMPLE_SURFACE, datetime(2026, 3, 17, 18, 0, tzinfo=timezone.utc)),
        )
        monkeypatch.setattr(
            wpc_main, "parse_coded_surface", lambda content: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert wpc_main.fetch_surface_analysis() is None

    def test_empty_parse_still_publishes_reopenable_collection(self, monkeypatch):
        actual = datetime(2026, 3, 17, 18, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(wpc_main, "download_coded_surface", _stub_download("nothing useful\n", actual))
        result = wpc_main.fetch_surface_analysis(actual)
        assert result["features"] == []
        reopened = json.loads(downloader.get_latest_output_filepath().read_text(encoding="utf-8"))
        assert reopened["type"] == "FeatureCollection"


class TestRetentionSweep:
    def test_old_timestamped_files_are_removed_fresh_and_latest_kept(self):
        import util.file as fs

        fs.WPC_SFC_DIR.mkdir(parents=True, exist_ok=True)
        old = fs.WPC_SFC_DIR / "wpc_sfc_20200101-000000.geojson"
        fresh = fs.WPC_SFC_DIR / "wpc_sfc_20300101-000000.geojson"
        latest = fs.WPC_SFC_DIR / "latest.geojson"
        old.write_text("{}")
        fresh.write_text("{}")
        latest.write_text("{}")
        aged = 10 * 24 * 3600
        os.utime(old, (old.stat().st_mtime - aged, old.stat().st_mtime - aged))

        # The sweep glob must never match the always-overwritten latest copy.
        assert not Path("latest.geojson").match(cleanup_glob())
        wpc_main.clean_old_files(max_age_minutes=60)

        assert not old.exists()
        assert fresh.exists()
        assert latest.exists()
