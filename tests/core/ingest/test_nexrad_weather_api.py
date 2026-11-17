import pytest
from common.ingest.nexrad import weather_api


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return _Response(self.payload)


def test_normalize_weather_vcp_accepts_r_prefixed_values():
    assert weather_api.normalize_weather_vcp("R215") == 215
    assert weather_api.normalize_weather_vcp("R212") == 212
    assert weather_api.normalize_weather_vcp("R12") == 12
    assert weather_api.normalize_weather_vcp("215") == 215
    assert weather_api.normalize_weather_vcp("VCP-215") == 215
    assert weather_api.normalize_weather_vcp(12) == 12


def test_normalize_weather_vcp_rejects_malformed_values():
    assert weather_api.normalize_weather_vcp(None) is None
    assert weather_api.normalize_weather_vcp("") is None
    assert weather_api.normalize_weather_vcp("R35X") is None
    assert weather_api.normalize_weather_vcp("ABC") is None


def test_fetch_radar_station_vcps_indexes_by_station_id():
    payload = {
        "features": [
            {
                "properties": {
                    "id": "KDDC",
                    "timeOfLastLevelTwoVolumeScan": "2026-05-06T21:00:00Z",
                    "rda": {"properties": {"volumeCoveragePattern": "R215", "timestamp": "2026-05-06T20:59:00Z"}},
                }
            },
            {"properties": {"id": "TXYZ", "rda": {"properties": {}}}},
        ]
    }

    stations = weather_api.fetch_radar_station_vcps(session=_Session(payload))
    assert stations["KDDC"].vcp == 215
    assert stations["KDDC"].raw_vcp == "R215"
    assert stations["TXYZ"].vcp is None


def test_fetch_radar_station_vcps_reads_live_latency_timestamp_shape():
    payload = {
        "features": [
            {
                "properties": {
                    "id": "KEPZ",
                    "latency": {"levelTwoLastReceivedTime": "2026-05-08T17:24:58+00:00"},
                    "rda": {
                        "timestamp": "2026-05-08T17:22:56+00:00",
                        "properties": {"volumeCoveragePattern": "R215"},
                    },
                }
            }
        ]
    }

    stations = weather_api.fetch_radar_station_vcps(session=_Session(payload))

    assert stations["KEPZ"].vcp == 215
    assert stations["KEPZ"].rda_timestamp == "2026-05-08T17:22:56+00:00"
    assert stations["KEPZ"].level_two_last_received_time == "2026-05-08T17:24:58+00:00"


def test_get_station_vcp_uses_short_ttl_cache():
    payload = {
        "features": [
            {"properties": {"id": "KDDC", "rda": {"properties": {"volumeCoveragePattern": "R215"}}}},
        ]
    }
    session = _Session(payload)
    weather_api.reset_station_vcp_cache()

    first = weather_api.get_station_vcp("kddc", cache_ttl_seconds=30, session=session)
    second = weather_api.get_station_vcp("KDDC", cache_ttl_seconds=30, session=session)

    assert first.vcp == 215
    assert second.vcp == 215
    assert session.calls == 1


@pytest.fixture(autouse=True)
def reset_catalog_cache():
    weather_api.reset_station_vcp_cache()
    yield
    weather_api.reset_station_vcp_cache()


def test_station_catalog_uses_last_known_good_after_transient_failure(monkeypatch):
    import requests
    catalog = weather_api.RadarStationCatalog(cache_ttl_seconds=30)
    now = [0.0]
    monkeypatch.setattr(weather_api.time, "time", lambda: now[0])
    session = _Session({"features": [{"properties": {"id": "KTLH"}}]})
    assert "KTLH" in catalog.fetch_radar_station_vcps(session=session)
    now[0] = 31
    monkeypatch.setattr(session, "get", lambda *a, **kw: (_ for _ in ()).throw(requests.RequestException("503")))
    assert "KTLH" in catalog.fetch_radar_station_vcps(session=session)
