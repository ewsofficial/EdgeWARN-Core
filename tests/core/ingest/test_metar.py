import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from EdgeWARN.ingest import metar

# === Tests for parse_metar ===

def test_parse_metar_basic():
    """Test parsing a basic METAR string."""
    metar_str = "METAR KJFK 121756Z 31009KT 10SM FEW250 M02/M17 A3039"
    result = metar.parse_metar(metar_str, "2023/01/12 17:56")
    
    assert result["type"] == "METAR"
    assert result["station"] == "KJFK"
    assert result["observation_time"] == "2023/01/12 17:56"
    assert result["wind"]["direction"] == "310"
    assert result["wind"]["speed"] == "09"
    assert result["wind"]["gust"] is None
    assert result["visibility"] == "10"
    assert result["temperature"] == "M02"
    assert result["dewpoint"] == "M17"
    assert result["pressure"] == 30.39
    assert result["clouds"] == [{"code": "FEW", "altitude": 25000}]

def test_parse_metar_with_gust():
    """Test parsing METAR with wind gusts."""
    metar_str = "KORD 121756Z 27015G25KT 5SM RA OVC020 08/06 A2990"
    result = metar.parse_metar(metar_str, "2023/01/12 17:56")
    
    assert result["wind"]["direction"] == "270"
    assert result["wind"]["speed"] == "15"
    assert result["wind"]["gust"] == "25"
    assert "RA" in result["weather"]
    assert result["clouds"] == [{"code": "OVC", "altitude": 2000}]

def test_parse_metar_variable_wind():
    """Test parsing METAR with variable wind direction (VRB)."""
    metar_str = "KATL 121756Z VRB03KT 10SM CLR 15/08 A3010"
    result = metar.parse_metar(metar_str, "2023/01/12 17:56")
    
    assert result["wind"]["direction"] == "VRB"
    assert result["wind"]["speed"] == "03"
    assert result["clouds"] == [{"code": "CLR"}]

def test_parse_metar_fractional_visibility():
    """Test parsing METAR with fractional visibility (e.g., 1/2SM)."""
    metar_str = "KDEN 121756Z 36010KT 1/2SM FG VV002 M01/M02 A3025"
    result = metar.parse_metar(metar_str, "2023/01/12 17:56")
    
    assert result["visibility"] == "1/2"
    assert "FG" in result["weather"]
    assert result["clouds"] == [{"code": "VV", "altitude": 200}]

def test_parse_metar_empty():
    """Test parsing an empty string."""
    result = metar.parse_metar("", "2023/01/12 17:56")
    assert result is None

def test_parse_metar_speci():
    """Test parsing a SPECI report."""
    metar_str = "SPECI KLAS 121800Z 25018G30KT 3SM TSRA OVC015CB 22/18 A2980"
    result = metar.parse_metar(metar_str, "2023/01/12 18:00")
    
    assert result["type"] == "SPECI"
    assert result["station"] == "KLAS"
    assert result["wind"]["gust"] == "30"
    assert "TSRA" in result["weather"]
    assert result["clouds"] == [{"code": "OVC", "altitude": 1500, "type": "CB"}]

def test_parse_metar_comprehensive():
    """Test parsing a complex METAR with all new fields."""
    metar_str = "KORD 121756Z 27015G25KT 5SM +RA BKN020 OVC040 08/06 A2990 RMK AO2 SLP134 T00830061"
    result = metar.parse_metar(metar_str, "2023/01/12 17:56")

    assert result["station"] == "KORD"
    assert result["wind"]["speed"] == "15"
    assert "+RA" in result["weather"]
    assert len(result["clouds"]) == 2
    assert result["clouds"][0] == {"code": "BKN", "altitude": 2000}
    assert result["clouds"][1] == {"code": "OVC", "altitude": 4000}
    assert result["remarks"] == "AO2 SLP134 T00830061"

# === Tests for process_content ===

def test_process_content_multiple():
    """Test processing content with multiple METARs."""
    content = """2023/01/12 17:56
KJFK 121756Z 31009KT 10SM FEW250 M02/M17 A3039
KORD 121756Z 27015KT 5SM OVC020 08/06 A2990

2023/01/12 18:00
KLAX 121800Z 25010KT 10SM CLR 18/10 A3000
"""
    with patch("EdgeWARN.ingest.metar.get_station_coordinates") as mock_coords:
        mock_coords.side_effect = lambda s: [40.0, -90.0]  # Return valid coords inside CONUS
        result = metar.process_content(content)
        
        assert len(result) == 3
        # ... logic as before ...
        assert result[0]["station"] == "KJFK"
        assert result[0]["observation_time"] == "2023/01/12 17:56"
        assert result[1]["station"] == "KORD"
        assert result[2]["station"] == "KLAX"
        assert result[2]["observation_time"] == "2023/01/12 18:00"

def test_process_content_empty():
    """Test processing empty content."""
    result = metar.process_content("")
    assert result == []

# === Tests for get_station_coordinates ===

def test_get_station_coordinates_found(mocker):
    """Test fetching coordinates for a known station."""
    # Mock the station database
    mocker.patch.object(metar, "_station_cache", {"KJFK": [40.6399, -73.7787]})
    
    coords = metar.get_station_coordinates("KJFK")
    assert coords == [40.6399, -73.7787]

def test_get_station_coordinates_not_found(mocker):
    """Test fetching coordinates for an unknown station."""
    mocker.patch.object(metar, "_station_cache", {"KJFK": [40.6399, -73.7787]})
    
    coords = metar.get_station_coordinates("XXXX")
    assert coords is None

def test_get_station_coordinates_case_insensitive(mocker):
    """Test that station lookup is case-insensitive."""
    mocker.patch.object(metar, "_station_cache", {"KJFK": [40.6399, -73.7787]})
    
    coords = metar.get_station_coordinates("kjfk")
    assert coords == [40.6399, -73.7787]

# === Tests for save_metar_data ===

def test_save_metar_data_success(mocker, mock_fs, mock_io_manager):
    """Test successful saving of METAR data."""
    metar.io = mock_io_manager
    mocker.patch("EdgeWARN.ingest.metar.fs.METAR_DIR", mock_fs / "metar")
    
    data = [{"station": "KJFK", "temperature": "M02"}]
    dt = datetime(2023, 1, 12, 17, tzinfo=timezone.utc)
    
    metar.save_metar_data(data, dt)
    
    expected_file = mock_fs / "metar" / "METAR_20230112-17z.json"
    assert expected_file.exists()

def test_save_metar_data_empty(mocker, mock_io_manager):
    """Test that saving empty data is a no-op."""
    metar.io = mock_io_manager
    
    metar.save_metar_data([], datetime.now(timezone.utc))
    
    mock_io_manager.write_warning.assert_called_with("No METAR data to save.")


def test_failed_station_download_retries_after_cooldown(tmp_path, monkeypatch):
    import io
    from datetime import timezone
    clock = [0.0]
    monkeypatch.setattr(metar, "_station_cache", None)
    monkeypatch.setattr(metar, "_station_retry_after", 0.0)
    monkeypatch.setattr(metar, "_station_cache_path", lambda: tmp_path / "stations.json")
    monkeypatch.setattr(metar.time, "monotonic", lambda: clock[0])
    calls = []
    class Response(io.BytesIO):
        def info(self):
            return {}
    def urlopen(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise OSError("upstream unavailable")
        return Response(b'[{"icaoId":"KJFK","lat":40.6,"lon":-73.7}]')
    monkeypatch.setattr(metar.urllib.request, "urlopen", urlopen)
    assert metar._load_station_database() == {}
    assert metar._load_station_database() == {}
    assert len(calls) == 1
    clock[0] = 61
    assert metar._load_station_database()["KJFK"] == [40.6, -73.7]
