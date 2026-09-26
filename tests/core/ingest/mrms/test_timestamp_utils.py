import pytest
from datetime import datetime, timezone, timedelta
from EdgeWARN.ingest.mrms.timestamp_utils import round_to_nearest_even_minute

# === Tests for round_to_nearest_even_minute ===

def test_round_even_minute_no_change():
    """Test that an even minute with low seconds stays the same."""
    ts = datetime(2023, 10, 15, 12, 30, 0, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.minute == 30
    assert result.second == 0

def test_round_even_minute_with_high_seconds():
    """An even-minute source remains in its own cycle."""
    ts = datetime(2023, 10, 15, 12, 30, 30, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.minute == 30
    assert result.second == 0

def test_round_odd_minute_low_seconds():
    """Test that an odd minute with <30 seconds rounds down."""
    ts = datetime(2023, 10, 15, 12, 31, 15, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.minute == 30
    assert result.second == 0

def test_round_odd_minute_high_seconds():
    """Test that an odd minute with >=30 seconds rounds up."""
    ts = datetime(2023, 10, 15, 12, 31, 45, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.minute == 32
    assert result.second == 0

def test_round_midnight_boundary():
    """Test rounding at midnight boundary (23:59:30 -> 00:00:00 next day)."""
    ts = datetime(2023, 10, 15, 23, 59, 30, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.day == 16
    assert result.hour == 0
    assert result.minute == 0
    assert result.second == 0

def test_round_preserves_timezone():
    """Test that timezone information is preserved."""
    ts = datetime(2023, 10, 15, 12, 31, 0, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.tzinfo == timezone.utc

def test_round_microseconds_zeroed():
    """Test that microseconds are always zeroed."""
    ts = datetime(2023, 10, 15, 12, 30, 5, 123456, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.microsecond == 0

def test_round_minute_zero():
    """Test rounding at minute 0."""
    ts = datetime(2023, 10, 15, 12, 0, 10, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.minute == 0

def test_round_minute_one_down():
    """Test minute 01 rounding down to 00."""
    ts = datetime(2023, 10, 15, 12, 1, 10, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.minute == 0

def test_round_minute_one_up():
    """Test minute 01 rounding up to 02."""
    ts = datetime(2023, 10, 15, 12, 1, 45, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.minute == 2

def test_round_minute_58_rollover():
    """Even minute 58 remains in the same hour."""
    ts = datetime(2023, 10, 15, 12, 58, 35, tzinfo=timezone.utc)
    result = round_to_nearest_even_minute(ts)
    
    assert result.hour == 12
    assert result.minute == 58


@pytest.mark.parametrize(
    ("source", "bucket"),
    [
        ("2026-09-23T00:40:40", "2026-09-23T00:40:00"),
        ("2026-09-23T00:41:29", "2026-09-23T00:40:00"),
        ("2026-09-23T00:41:30", "2026-09-23T00:42:00"),
        ("2026-09-23T00:42:39", "2026-09-23T00:42:00"),
        ("2026-09-23T00:43:29", "2026-09-23T00:42:00"),
        ("2026-09-23T00:43:30", "2026-09-23T00:44:00"),
        ("2026-09-23T00:59:29", "2026-09-23T00:58:00"),
        ("2026-09-23T00:59:30", "2026-09-23T01:00:00"),
        ("2026-09-23T23:59:30", "2026-09-24T00:00:00"),
    ],
)
def test_round_cycle_boundaries(source, bucket):
    assert round_to_nearest_even_minute(
        datetime.fromisoformat(source).replace(tzinfo=timezone.utc)
    ) == datetime.fromisoformat(bucket).replace(tzinfo=timezone.utc)
