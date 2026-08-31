"""Portfolio value over time — plan.md §6, §10 Phase 4.

Hand-computed like test_lots.py, because this is the other place a bug produces
a wrong number rather than an error.
"""

import sqlite3
from datetime import datetime
from typing import Any

import pytest

from app.db.connection import connect, migrate
from app.providers.base import Candle, ProviderError, SymbolNotFound
from app.services import portfolio as engine
from app.services.performance import holdings_by_day, portfolio_performance


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


def buy(conn, symbol, qty, price, at, fees=0.0):
    engine.record_transaction(
        conn,
        engine.NewTransaction(
            symbol=symbol, side="BUY", quantity=qty, price=price, fees=fees, executed_at=at
        ),
    )


def sell(conn, symbol, qty, price, at):
    engine.record_transaction(
        conn,
        engine.NewTransaction(
            symbol=symbol, side="SELL", quantity=qty, price=price, executed_at=at
        ),
    )


def ts(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp())


class CandleProvider:
    """Serves a fixed close per symbol per day."""

    name = "fake"

    def __init__(self, closes: dict[str, dict[str, float]], fail: set[str] | None = None) -> None:
        self.closes = closes
        self.fail = fail or set()
        self.calls: list[str] = []

    async def candles(self, symbol: str, resolution: str, frm: int, to: int) -> list[Candle]:
        self.calls.append(symbol)
        if symbol in self.fail:
            raise ProviderError("no historical data on this plan")
        if symbol not in self.closes:
            raise SymbolNotFound(symbol)
        return [
            Candle(time=ts(day), open=c, high=c, low=c, close=c, volume=0)
            for day, c in sorted(self.closes[symbol].items())
        ]

    async def quote(self, s: str) -> Any: ...
    async def search(self, q: str) -> list:
        return []

    def stream(self, s: set[str]) -> Any:
        raise NotImplementedError


# --- holdings replay --------------------------------------------------------


def test_holdings_are_replayed_forwards_not_derived_backwards(db) -> None:
    buy(db, "AAPL", 10, 100.0, "2026-01-02T10:00:00Z")
    buy(db, "AAPL", 5, 120.0, "2026-01-04T10:00:00Z")
    sell(db, "AAPL", 6, 150.0, "2026-01-06T10:00:00Z")

    from app.services.performance import transaction_history

    rows = transaction_history(db)
    days = [datetime.fromisoformat(f"2026-01-0{d}T00:00:00+00:00").date() for d in (1, 3, 5, 7)]
    snaps = holdings_by_day(rows, days)

    assert snaps[days[0]].get("AAPL") is None, "nothing held before the first buy"
    assert snaps[days[1]]["AAPL"] == pytest.approx(10)
    assert snaps[days[2]]["AAPL"] == pytest.approx(15)
    assert snaps[days[3]]["AAPL"] == pytest.approx(9)


def test_cost_basis_tracks_alongside_the_share_count(db) -> None:
    buy(db, "AAPL", 10, 100.0, "2026-01-02T10:00:00Z", fees=5.0)

    from app.services.performance import transaction_history

    days = [datetime.fromisoformat("2026-01-03T00:00:00+00:00").date()]
    snaps = holdings_by_day(transaction_history(db), days)
    assert snaps[days[0]]["__basis__"] == pytest.approx(1005.0), "fees join the basis"


# --- the series -------------------------------------------------------------


async def test_the_series_matches_hand_computed_values(db) -> None:
    #   Jan 2: BUY 10 AAPL @ 100  -> basis 1000
    #   Jan 4: BUY  5 MSFT @ 200  -> basis 1000 + 1000 = 2000
    #
    #   closes    AAPL   MSFT
    #   Jan 2      100     --
    #   Jan 3      110     --
    #   Jan 4      120    200
    #   Jan 5      130    210
    #
    #   value Jan 2 = 10x100                = 1000
    #   value Jan 3 = 10x110                = 1100
    #   value Jan 4 = 10x120 + 5x200        = 2200
    #   value Jan 5 = 10x130 + 5x210        = 2350
    buy(db, "AAPL", 10, 100.0, "2026-01-02T10:00:00Z")
    buy(db, "MSFT", 5, 200.0, "2026-01-04T10:00:00Z")

    provider = CandleProvider(
        {
            "AAPL": {"2026-01-02": 100, "2026-01-03": 110, "2026-01-04": 120, "2026-01-05": 130},
            "MSFT": {"2026-01-04": 200, "2026-01-05": 210},
        }
    )
    series, unpriced = await portfolio_performance(
        db, provider, frm=ts("2026-01-01"), to=ts("2026-01-06")
    )

    assert [p.value for p in series] == [1000.0, 1100.0, 2200.0, 2350.0]
    assert [p.cost_basis for p in series] == [1000.0, 1000.0, 2000.0, 2000.0]
    assert series[-1].unrealized == pytest.approx(350.0)
    assert unpriced == []


async def test_a_sale_reduces_the_value_and_the_basis(db) -> None:
    buy(db, "AAPL", 10, 100.0, "2026-01-02T10:00:00Z")
    sell(db, "AAPL", 5, 150.0, "2026-01-03T10:00:00Z")

    provider = CandleProvider({"AAPL": {"2026-01-02": 100, "2026-01-04": 100}})
    series, _ = await portfolio_performance(db, provider, frm=ts("2026-01-01"), to=ts("2026-01-05"))

    assert series[0].value == pytest.approx(1000.0)
    assert series[1].value == pytest.approx(500.0), "5 shares left"
    assert series[1].cost_basis == pytest.approx(500.0), "basis halves with the position"


async def test_a_missing_day_carries_the_previous_close_rather_than_cratering(db) -> None:
    """A halt or a listing gap must not put a false cliff in the chart."""
    buy(db, "AAPL", 10, 100.0, "2026-01-01T10:00:00Z")
    buy(db, "MSFT", 10, 100.0, "2026-01-01T10:00:00Z")

    provider = CandleProvider(
        {
            "AAPL": {"2026-01-02": 100, "2026-01-03": 100, "2026-01-04": 100},
            "MSFT": {"2026-01-02": 50, "2026-01-04": 50},  # no bar on the 3rd
        }
    )
    series, _ = await portfolio_performance(db, provider, frm=ts("2026-01-01"), to=ts("2026-01-05"))

    assert [p.value for p in series] == [1500.0, 1500.0, 1500.0], "no cliff on the 3rd"


async def test_a_symbol_with_no_history_is_reported_not_zero_filled(db) -> None:
    """§4: a missing price is not a price of zero."""
    buy(db, "AAPL", 10, 100.0, "2026-01-01T10:00:00Z")
    buy(db, "XYZZY", 10, 50.0, "2026-01-01T10:00:00Z")

    provider = CandleProvider({"AAPL": {"2026-01-02": 100}}, fail={"XYZZY"})
    series, unpriced = await portfolio_performance(
        db, provider, frm=ts("2026-01-01"), to=ts("2026-01-03")
    )

    assert unpriced == ["XYZZY"]
    assert series[0].value == pytest.approx(1000.0), "XYZZY contributes nothing, not zero"
    assert series[0].cost_basis == pytest.approx(1500.0), "but it is still money you spent"


async def test_an_empty_ledger_produces_an_empty_series(db) -> None:
    series, unpriced = await portfolio_performance(
        db, CandleProvider({}), frm=ts("2026-01-01"), to=ts("2026-01-05")
    )
    assert series == []
    assert unpriced == []


async def test_no_history_at_all_yields_an_empty_series_and_names_the_symbols(db) -> None:
    buy(db, "AAPL", 1, 1.0, "2026-01-01T10:00:00Z")
    series, unpriced = await portfolio_performance(
        db, CandleProvider({}, fail={"AAPL"}), frm=ts("2026-01-01"), to=ts("2026-01-05")
    )
    assert series == []
    assert unpriced == ["AAPL"]


async def test_transactions_after_the_window_are_excluded(db) -> None:
    buy(db, "AAPL", 10, 100.0, "2026-01-02T10:00:00Z")
    buy(db, "AAPL", 90, 100.0, "2026-06-01T10:00:00Z")  # well after `to`

    provider = CandleProvider({"AAPL": {"2026-01-02": 100, "2026-01-03": 100}})
    series, _ = await portfolio_performance(db, provider, frm=ts("2026-01-01"), to=ts("2026-01-04"))
    assert all(p.value == pytest.approx(1000.0) for p in series)


async def test_a_naive_timestamp_is_treated_as_utc(db) -> None:
    """The UI can send a stamp without an offset; the same ledger must not
    produce different charts on different machines."""
    buy(db, "AAPL", 10, 100.0, "2026-01-02T10:00:00")  # no Z, no offset

    provider = CandleProvider({"AAPL": {"2026-01-03": 100}})
    series, _ = await portfolio_performance(db, provider, frm=ts("2026-01-01"), to=ts("2026-01-05"))
    assert series[0].value == pytest.approx(1000.0)


async def test_each_symbol_costs_one_upstream_call(db) -> None:
    """800 credits/day on the free tier; one call per symbol per range."""
    buy(db, "AAPL", 1, 1.0, "2026-01-01T10:00:00Z")
    buy(db, "MSFT", 1, 1.0, "2026-01-01T10:00:00Z")

    provider = CandleProvider({"AAPL": {"2026-01-02": 1}, "MSFT": {"2026-01-02": 1}})
    await portfolio_performance(db, provider, frm=ts("2026-01-01"), to=ts("2026-01-03"))
    assert provider.calls == ["AAPL", "MSFT"]
