"""Time utilities."""

import datetime
import logging
import math
import re
from zoneinfo import ZoneInfoNotFoundError

import pytz
from tzlocal import get_localzone

logger = logging.getLogger(__name__)

UTC_OFFSET_PATTERN = re.compile(
    r"^(?:UTC|GMT)\s*(?:([+-])\s*(\d{1,2})(?::?(\d{2}))?)?$", re.IGNORECASE
)


def posix_timezone(value: str) -> str:
    """Convert an IANA zone name or a UTC offset to a POSIX TZ string.

    Accepts names such as "Europe/Moscow" and offsets such as "UTC+3" or
    "GMT-5:30". POSIX offsets count hours west of UTC, so "UTC+3" becomes
    "UTC-3". Raises ValueError for anything else.
    """
    value = value.strip()
    match = UTC_OFFSET_PATTERN.match(value)

    if match:
        sign, hours, minutes = match.groups()
        hours = int(hours or 0)
        minutes = int(minutes or 0)

        if hours > 14 or minutes >= 60:
            raise ValueError(f"Invalid UTC offset: {value}")

        if hours == 0 and minutes == 0:
            return "UTC0"

        posix = f"UTC{'-' if sign == '+' else ''}{hours}"
        return f"{posix}:{minutes:02d}" if minutes else posix

    try:
        zone = pytz.timezone(value).zone

        with pytz.open_resource(zone) as file:
            data = file.read()
    except (pytz.UnknownTimeZoneError, OSError, ValueError) as e:
        raise ValueError(f"Unknown timezone: {value}") from e

    # version 2+ zone files end with the zone's current rule as a POSIX TZ string
    rule = data.rstrip(b"\n").rsplit(b"\n", 1)[-1] if data[4:5] >= b"2" else b""

    if not rule or b"\0" in rule:
        raise ValueError(f"No POSIX rule available for timezone: {value}")

    return rule.decode()


def get_tz_modifiers(tz_name: str) -> tuple[str, str, float]:
    seconds_offset = (
        datetime.datetime.now(pytz.timezone(tz_name)).utcoffset().total_seconds()
    )
    hours_offset = int(seconds_offset / 60 / 60)
    minutes_offset = int(seconds_offset / 60 - hours_offset * 60)
    hour_modifier = f"{hours_offset} hour"
    minute_modifier = f"{minutes_offset} minute"
    return hour_modifier, minute_modifier, seconds_offset


def get_tomorrow_at_time(hour: int) -> datetime.datetime:
    """Returns the datetime of the following day at 2am."""
    try:
        tomorrow = datetime.datetime.now(get_localzone()) + datetime.timedelta(days=1)
    except ZoneInfoNotFoundError:
        tomorrow = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
        logger.warning(
            "Using utc for maintenance due to missing or incorrect timezone set"
        )

    return tomorrow.replace(hour=hour, minute=0, second=0).astimezone(datetime.UTC)


def is_current_hour(timestamp: int) -> bool:
    """Returns if timestamp is in the current UTC hour."""
    start_of_next_hour = (
        datetime.datetime.now(datetime.UTC).replace(minute=0, second=0, microsecond=0)
        + datetime.timedelta(hours=1)
    ).timestamp()
    return timestamp < start_of_next_hour


def _utc_offset(tz: datetime.tzinfo, timestamp: float) -> float:
    dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.UTC)
    return dt.astimezone(tz).utcoffset().total_seconds()


def _find_transition(
    tz: datetime.tzinfo, lo: float, hi: float, lo_offset: float
) -> float:
    """Bisect (lo, hi] to the second where the UTC offset first differs from lo_offset."""
    # whole seconds, so the midpoint always advances (a fractional bound can
    # otherwise leave the midpoint sitting on lo) and lands on the transition
    low = math.floor(lo)
    high = math.ceil(hi)

    while high - low > 1:
        mid = (low + high) // 2
        if _utc_offset(tz, mid) == lo_offset:
            low = mid
        else:
            high = mid

    return float(high)


def get_dst_transitions(
    tz_name: str, start_time: float, end_time: float
) -> list[tuple[float, float, float]]:
    """
    Find DST transition points and return time periods with consistent offsets.

    Args:
        tz_name: Timezone name (e.g., 'America/New_York')
        start_time: Start timestamp (UTC)
        end_time: End timestamp (UTC)

    Returns:
        List of (period_start, period_end, seconds_offset) tuples representing
        continuous periods with the same UTC offset
    """
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        # If timezone is invalid, return single period with no offset
        return [(start_time, end_time, 0)]

    periods = []
    current = start_time
    period_start = start_time
    prev_offset = _utc_offset(tz, current)

    # Probe at most a day ahead, capped at end_time so a transition after the
    # last full day is still seen instead of silently kept in the last period.
    while current < end_time:
        next_probe = min(current + 86400, end_time)
        next_offset = _utc_offset(tz, next_probe)

        if next_offset != prev_offset:
            transition = _find_transition(tz, current, next_probe, prev_offset)
            periods.append((period_start, transition, prev_offset))
            period_start = transition
            prev_offset = _utc_offset(tz, transition)
            current = transition
        else:
            current = next_probe

    periods.append((period_start, end_time, prev_offset))

    return periods
