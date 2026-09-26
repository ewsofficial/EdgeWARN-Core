"""
Timestamp utilities for MRMS data processing.
"""
import datetime


def round_to_nearest_even_minute(ts: datetime.datetime) -> datetime.datetime:
    """
    Round a timestamp to the nearest even minute.
    
    Examples:
        23:59:30 → 00:00:00 (next day if at midnight boundary)
        23:59:00 → 23:58:00
        23:58:59 → 23:58:00
        23:57:30 → 23:58:00
        23:56:00 → 23:56:00
    
    Args:
        ts: Timezone-aware datetime object
        
    Returns:
        Datetime rounded to nearest even minute with seconds/microseconds zeroed
    """
    # First, zero out seconds and microseconds
    base = ts.replace(second=0, microsecond=0)
    
    if base.minute % 2 == 0:
        return base

    # Only odd minutes cross a cycle boundary at the 30-second midpoint.
    if ts.second >= 30:
        return base + datetime.timedelta(minutes=1)
    return base - datetime.timedelta(minutes=1)
