"""US equity market hours — plan.md §4, §10 Phase 2.

Drives two things: whether the fallback ladder should be polling at all, and
whether the UI says CLOSED rather than showing a two-day-old price as if it were
live. Getting this wrong is the dishonesty §4 warns about, so holidays are
computed from the NYSE rules rather than hardcoded as a date list that silently
rots after next January.
"""

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("America/New_York")

REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)


class MarketState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class MarketStatus:
    state: MarketState
    # True on a half-day, so the UI can explain a 13:00 close.
    early_close: bool = False

    @property
    def is_open(self) -> bool:
        return self.state is MarketState.OPEN


def _easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm. Good Friday is Easter minus two days."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th `weekday` (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))
    last_day = (
        (dt.date(year, month % 12 + 1, 1) - dt.timedelta(days=1))
        if month != 12
        else dt.date(year, 12, 31)
    )
    offset = (last_day.weekday() - weekday) % 7
    return last_day - dt.timedelta(days=offset)


def _observed(day: dt.date) -> dt.date:
    """A holiday on Saturday is observed Friday; on Sunday, Monday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def market_holidays(year: int) -> set[dt.date]:
    """NYSE full-day closures."""
    return {
        _observed(dt.date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - dt.timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _observed(dt.date(year, 6, 19)),  # Juneteenth
        _observed(dt.date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(dt.date(year, 12, 25)),  # Christmas
    }


def early_close_days(year: int) -> set[dt.date]:
    """Half days: 13:00 ET close."""
    days = {_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1)}  # day after Thanksgiving
    christmas_eve = dt.date(year, 12, 24)
    if christmas_eve.weekday() < 5:
        days.add(christmas_eve)
    july_3 = dt.date(year, 7, 3)
    if july_3.weekday() < 5 and _observed(dt.date(year, 7, 4)) == dt.date(year, 7, 4):
        days.add(july_3)
    return days


def market_status(now: dt.datetime | None = None) -> MarketStatus:
    """Regular session only. Pre/post market is out of scope for v1 (§1)."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(EXCHANGE_TZ)
    day = moment.date()

    if day.weekday() >= 5 or day in market_holidays(day.year):
        return MarketStatus(MarketState.CLOSED)

    early = day in early_close_days(day.year)
    close_at = EARLY_CLOSE if early else REGULAR_CLOSE
    open_now = REGULAR_OPEN <= moment.time() < close_at
    return MarketStatus(MarketState.OPEN if open_now else MarketState.CLOSED, early_close=early)


def is_market_open(now: dt.datetime | None = None) -> bool:
    return market_status(now).is_open
