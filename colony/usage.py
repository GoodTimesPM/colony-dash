"""The weekly window, and where its edges actually are.

Anthropic's 7-day allowance does not reset on Monday, or at midnight, or on any
boundary the calendar knows about. It resets on **Friday at 05:00 local**, and
the tray app's cache reports the exact instant it will next do so. Jordan's
sprints run on that clock or they are measuring a week that does not exist:

    "my weekly token usage resets every friday at 5:00 AM. The weekly sprints
     and day count should abide by this range"

Two things live here, because they are the same fact seen twice.

`read()` is the cache file, parsed. The pulse used to be the only reader, once
an hour, and the dashboard showed whatever the last tick had copied into
`usage_samples` — so a number that changes every five minutes was arriving up to
an hour late and reading, at a glance, like a counter that had stopped. Nothing
about the cache costs anything to read: it is a local file written by the tray
app, which is the one process allowed to call the usage endpoint (~5 requests
per rolling 5 minutes per account, shared with the Claude Code CLI itself). The
history rows still come from the pulse; the live figure comes from here.

`week_window()` is the Friday-to-Friday range. It prefers the reset instant the
API itself reported, because that is the truth and this is only a model of it.
The Friday-05:00 arithmetic is the fallback for when there is no cache to read
— a fresh machine, a stopped tray app — and it is deliberately a fallback: a
constant in this file cannot know about a daylight-saving shift or an account
whose window moved, and the reported instant can.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

CACHE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "claude-usage" / "usage.json"

# Every ledger timestamp is written in this shape, and so is every instant that
# leaves this module for somewhere that cannot hold a datetime.
TS = "%Y-%m-%d %H:%M:%S"

# Past this, the tray app has stopped and the figure on screen is a fossil.
STALE_AFTER = timedelta(minutes=20)

# Friday, 05:00, local. Monday is 0.
RESET_DOW = 4
RESET_HOUR = 5
WEEK = timedelta(days=7)


def parse_iso(text: str | None) -> datetime | None:
    """An ISO instant from the cache, as a naive *local* datetime.

    The cache writes UTC with an offset: "2026-08-28T08:59:59.877882+00:00".
    The dashboard was printing the first sixteen characters of that string, so
    a window that closes at five in the morning was on screen as "08:59" — the
    right instant, told in a timezone nobody in this house lives in. Everything
    downstream compares against `datetime('now','localtime')` values from the
    ledger, so the conversion happens once, here.
    """
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    # Microseconds dropped here rather than at each print site. The ledger keeps
    # whole seconds everywhere, and "resets 2026-08-28 05:00:00.383185" is a
    # reset instant reported to a precision nobody can act on.
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
    """A `read()` result with its instants rendered as ledger timestamps.

    `read()` hands back real `datetime` objects, because everything that does
    arithmetic on a reset instant wants one. `json.dumps` does not, and the
    pulse writes its whole context into `pulses.detail` as JSON — so the
    moment `read()` started parsing instead of passing strings through, every
    tick began dying at that one boundary. Fifteen of them died before anyone
    noticed, because a scheduled task that exits 1 looks, from the dashboard,
    exactly like a colony with nothing to do.

    Anything crossing a serialisation boundary comes through here.
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
    """The allowance week `now` falls in, as (start, end).

    Half-open: a run at exactly the reset instant belongs to the week that is
    opening, not the one that just closed.

    `resets_at` is the end the API reported. It is trusted when it is anywhere
    near sane — within a week either side of now — and ignored when it is not,
    because a cache left behind by a stopped tray app can name a reset that
    happened days ago, and quietly measuring last week is worse than measuring
    an arithmetic week that is at least the right length.
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
