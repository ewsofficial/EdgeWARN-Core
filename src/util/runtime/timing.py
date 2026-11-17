import time
from datetime import datetime, timedelta, timezone


def sleep_for(total_seconds, interval=1.0):
    end_time = time.monotonic() + total_seconds
    while time.monotonic() < end_time:
        time.sleep(min(interval, max(0.0, end_time - time.monotonic())))


def sleep_until_boundary(minutes, interval):
    now = datetime.now(timezone.utc)
    minutes_to_next = minutes - (now.minute % minutes)
    if minutes_to_next == 0 and now.second == 0 and now.microsecond == 0:
        minutes_to_next = minutes
    next_run = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_next)
    sleep_seconds = max(0.0, (next_run - now).total_seconds())
    if sleep_seconds > 0:
        sleep_for(sleep_seconds, interval=interval)
