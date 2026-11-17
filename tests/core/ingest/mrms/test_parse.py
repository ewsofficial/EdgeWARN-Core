import pytest
from datetime import datetime, timezone, timedelta
from EdgeWARN.ingest.mrms.parse import parse_mrms_bucket_path, parse_goes_bucket_path

# === Tests for parse_mrms_bucket_path ===

def test_parse_mrms_bucket_path_standard():
    """Test standard MRMS bucket path generation."""
    dt = datetime(2023, 10, 15, 12, 30, tzinfo=timezone.utc)
    result = parse_mrms_bucket_path(dt, "CONUS", "MergedReflectivityQCComposite_00.50")
    
    assert result == "CONUS/MergedReflectivityQCComposite_00.50/20231015/"

def test_parse_mrms_bucket_path_no_modifier():
    """Test path generation when modifier is None."""
    dt = datetime(2023, 10, 15, 12, 30, tzinfo=timezone.utc)
    result = parse_mrms_bucket_path(dt, "CONUS", None)
    
    assert result == "ProbSevere/20231015/"

def test_parse_mrms_bucket_path_year_boundary():
    """Test path generation around New Year."""
    dt = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    result = parse_mrms_bucket_path(dt, "CONUS", "ProbSevere")
    
    assert result == "ProbSevere/20240101/"

def test_parse_mrms_bucket_path_uses_code_owned_region():
    """Caller-supplied regions cannot alter the v2 CONUS source grammar."""
    dt = datetime(2023, 7, 4, 18, 0, tzinfo=timezone.utc)
    modifier = "MergedReflectivityQCComposite_00.50"
    assert parse_mrms_bucket_path(dt, "CONUS", modifier) == \
        "CONUS/MergedReflectivityQCComposite_00.50/20230704/"
    assert parse_mrms_bucket_path(dt, "ALASKA", modifier) == \
        "CONUS/MergedReflectivityQCComposite_00.50/20230704/"

# === Tests for parse_goes_bucket_path ===

def test_parse_goes_bucket_path_standard():
    """Test standard GOES bucket path generation."""
    dt = datetime(2023, 10, 15, 14, 30, tzinfo=timezone.utc)
    result = parse_goes_bucket_path(dt, "GLM-L2-LCFA")
    
    # Oct 15 is day 288 of 2023
    assert result == "GLM-L2-LCFA/2023/288/14/"

def test_parse_goes_bucket_path_with_offset():
    """Test GOES path with hour offset."""
    dt = datetime(2023, 10, 15, 14, 30, tzinfo=timezone.utc)
    result = parse_goes_bucket_path(dt, "GLM-L2-LCFA", hour_offset=2)
    
    # 14:30 - 2 hours = 12:30
    assert result == "GLM-L2-LCFA/2023/288/12/"

def test_parse_goes_bucket_path_offset_crosses_day():
    """Test GOES path when offset crosses day boundary."""
    dt = datetime(2023, 10, 15, 2, 0, tzinfo=timezone.utc)
    result = parse_goes_bucket_path(dt, "ABI-L2-ACHAC", hour_offset=3)
    
    # 02:00 - 3 hours = 23:00 on Oct 14 (day 287)
    assert result == "ABI-L2-ACHAC/2023/287/23/"

def test_parse_goes_bucket_path_leap_year():
    """Test GOES path generation on leap year."""
    dt = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    result = parse_goes_bucket_path(dt, "GLM-L2-LCFA")
    
    # 2024 is a leap year, March 1 = day 61
    assert result == "GLM-L2-LCFA/2024/061/12/"

def test_parse_goes_bucket_path_year_end():
    """Test GOES path at year end."""
    dt = datetime(2023, 12, 31, 23, 59, tzinfo=timezone.utc)
    result = parse_goes_bucket_path(dt, "GLM-L2-LCFA")
    
    # Dec 31 = day 365
    assert result == "GLM-L2-LCFA/2023/365/23/"
