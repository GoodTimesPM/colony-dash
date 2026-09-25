"""The weekly allowance window and where its edges are.

The 7-day allowance resets Friday 05:00 local, not on a calendar boundary,
and the PO's sprints run on that clock.

`read()` parses the tray app's cache live. The tray app is the only process
allowed to call the rate-limited usage endpoint, so the file is free to
read; the pulse still writes the history rows.

`week_window()` prefers the reset instant the API reported and falls back to
Friday-05:00 arithmetic when there is no cache. The reported instant follows
DST and account changes; the constant cannot.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

CACHE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "claude-usage" / "usage.json"

# The ledger's timestamp format, also used for instants leaving this module.
TS = "%Y-%m-%d %H:%M:%S"

# Past this, the tray app has stopped and the figure on screen is a fossil.
STALE_AFTER = timedelta(minutes=20)

# Friday, 05:00, local. Monday is 0.
RESET_DOW = 4
RESET_HOUR = 5
WEEK = timedelta(days=7)


def parse_iso(text: str | None) -> datetime | None:
    """An ISO instant from the cache (UTC with offset) as a naive local
    datetime, to compare against the ledger's `datetime('now','localtime')`
    values.
    """
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    # Whole seconds, like every other ledger timestamp.
    if stamp.tzinfo is None:
        return stamp.replace(microsecond=0)
    return stamp.astimezone().replace(tzinfo=None, microsecond=0)


def read() -> dict | None:
    """The tray app's last good read, or None if there isn't one."""
    if not CACHE.is_file():
        return None
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        mtime = datetime.fromtimestamp(CACHE.stat().st_mtime)
    except (json.JSONDecodeError, OSError):
        return None
    return {
        "five_hour": data.get("five_hour"),
        "seven_day": data.get("seven_day"),
        "five_hour_resets": parse_iso(data.get("five_hour_resets")),
        "seven_day_resets": parse_iso(data.get("seven_day_resets")),
        "read_at": parse_iso(data.get("read_at")),
        "mtime": mtime,
        "stale": datetime.now() - mtime > STALE_AFTER,
    }


def json_safe(sample: dict | None) -> dict | None:
    """A `read()` result with datetimes rendered as ledger timestamps, for
    anything that goes through `json.dumps` (the pulse's `pulses.detail`).
    """
    if sample is None:
        return None
    out = {}
    for key, value in sample.items():
        out[key] = value.strftime(TS) if isinstance(value, datetime) else value
    return out


def next_reset(now: datetime | None = None) -> datetime:
    """The next Friday 05:00 at or after `now`, by arithmetic alone."""
    now = now or datetime.now()
    anchor = now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    ahead = (RESET_DOW - anchor.weekday()) % 7
    reset = anchor + timedelta(days=ahead)
    return reset + WEEK if reset <= now else reset


def week_window(now: datetime | None = None,
                resets_at: datetime | None = None) -> tuple[datetime, datetime]:
    """The allowance week `now` falls in, as half-open (start, end).

    `resets_at` is trusted only within a week of now. A stale cache from a
    stopped tray app can name a reset days old, and an arithmetic week beats
    last week.
    """
    now = now or datetime.now()
    end = resets_at
    if end is None or not (now - WEEK <= end <= now + WEEK):
        end = next_reset(now)
    while end <= now:
        end += WEEK
    return end - WEEK, end


def current_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """`week_window` fed from the cache, which is what every caller wants."""
    sample = read()
    return week_window(now, sample["seven_day_resets"] if sample else None)


def day_of(window: tuple[datetime, datetime], now: datetime | None = None) -> int:
    """Which day of the week we are on, 1-7, counting from the reset."""
    now = now or datetime.now()
    return min(7, max(1, int((now - window[0]).total_seconds() // 86400) + 1))
