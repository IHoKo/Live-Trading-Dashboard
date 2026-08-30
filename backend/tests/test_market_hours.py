"""Market hours — plan.md §4.

Holidays are computed from NYSE rules rather than hardcoded, so these assert the
rules produce the real 2026/2027 calendar. Getting this wrong means labelling a
two-day-old price as LIVE, which §4 explicitly forbids.
"""

import datetime as dt

import pytest

from app.services.market_hours import (
    EXCHANGE_TZ,
    early_close_days,
    is_market_open,
    market_holidays,
    market_status,
)


def et(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=EXCHANGE_TZ)


@pytest.mark.parametrize(
    ("day", "name"),
    [
        ("2026-01-01", "New Year's Day"),
        ("2026-01-19", "MLK Jr Day"),
        ("2026-02-16", "Washington's Birthday"),
        ("2026-04-03", "Good Friday"),
        ("2026-05-25", "Memorial Day"),
        ("2026-06-19", "Juneteenth"),
        ("2026-07-03", "Independence Day (Jul 4 is a Saturday, observed Friday)"),
        ("2026-09-07", "Labor Day"),
        ("2026-11-26", "Thanksgiving"),
        ("2026-12-25", "Christmas"),
    ],
)
def test_2026_nyse_holidays(day: str, name: str) -> None:
    assert dt.date.fromisoformat(day) in market_holidays(2026), name


def test_2027_good_friday_tracks_easter() -> None:
    # Easter 2027 is 28 March, so Good Friday is the 26th.
    assert dt.date(2027, 3, 26) in market_holidays(2027)


def test_holiday_on_a_sunday_is_observed_on_monday() -> None:
    # 2027-12-25 is a Saturday -> observed Friday the 24th.
    assert dt.date(2027, 12, 24) in market_holidays(2027)


@pytest.mark.parametrize(
    ("stamp", "expected", "why"),
    [
        ("2026-08-28 10:00", True, "Friday mid-session"),
        ("2026-08-28 09:30", True, "the opening bell counts as open"),
        ("2026-08-28 09:29", False, "one minute before the bell"),
        ("2026-08-28 16:00", False, "the close is exclusive"),
        ("2026-08-29 12:00", False, "Saturday"),
        ("2026-08-30 12:00", False, "Sunday"),
        ("2026-12-25 11:00", False, "Christmas"),
    ],
)
def test_session_boundaries(stamp: str, expected: bool, why: str) -> None:
    assert is_market_open(et(stamp)) is expected, why


def test_half_days_close_at_1pm() -> None:
    assert dt.date(2026, 11, 27) in early_close_days(2026)  # day after Thanksgiving
    assert is_market_open(et("2026-11-27 12:59")) is True
    assert is_market_open(et("2026-11-27 13:00")) is False
    assert market_status(et("2026-11-27 12:00")).early_close is True


def test_a_normal_day_is_not_flagged_early_close() -> None:
    assert market_status(et("2026-08-28 10:00")).early_close is False


def test_utc_input_is_converted_to_exchange_time() -> None:
    # 14:30 UTC on a summer Friday is 10:30 ET — open.
    assert is_market_open(dt.datetime(2026, 8, 28, 14, 30, tzinfo=dt.UTC)) is True
    # 21:30 UTC is 17:30 ET — closed.
    assert is_market_open(dt.datetime(2026, 8, 28, 21, 30, tzinfo=dt.UTC)) is False
