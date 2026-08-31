"""Portfolio value over time — plan.md §6, §10 Phase 4.

"Reconstruct from transactions + historical closes."

The shape of the problem: for every trading day in the range, work out how many
shares of each symbol were held *as of that day* by replaying the transaction
log, then value them at that day's close. Sum across symbols.

Two things this deliberately does not do:

* It does not use the current position as a starting point and work backwards.
  Transactions are the source of truth (§5), and replaying forwards is the same
  discipline the lot engine uses — reversing a history is where subtle bugs live.
* It does not invent a value for a day it has no close for. A gap is reported as
  a gap. §4's rule against rendering stale numbers as live applies just as much
  to a chart as to a price.
"""

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.providers.base import MarketDataProvider, ProviderError, SymbolNotFound

logger = logging.getLogger("ticker.performance")

# Points are daily. Intraday portfolio history would need intraday bars for
# every symbol ever held, which is a different cost profile entirely.
RESOLUTION = "D"


@dataclass(frozen=True)
class PerformancePoint:
    date: str
    value: float
    cost_basis: float

    @property
    def unrealized(self) -> float:
        return round(self.value - self.cost_basis, 4)


def transaction_history(conn: sqlite3.Connection, until: int | None = None) -> list[dict]:
    sql = "SELECT symbol, side, quantity, price, fees, executed_at FROM transactions"
    params: tuple = ()
    if until is not None:
        sql += " WHERE executed_at <= ?"
        params = (datetime.fromtimestamp(until, UTC).isoformat(),)
    sql += " ORDER BY executed_at, id"
    return [dict(row) for row in conn.execute(sql, params)]


def symbols_ever_held(rows: list[dict]) -> list[str]:
    return sorted({row["symbol"] for row in rows})


def _as_date(iso: str) -> date:
    """`executed_at` is ISO8601 UTC, but not always with an offset — the UI can
    send a bare local-looking stamp. A naive value is treated as UTC rather than
    as machine-local, or the same ledger would produce different charts on
    different machines."""
    moment = datetime.fromisoformat(iso)  # 3.11+ parses the "Z" suffix
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def holdings_by_day(rows: list[dict], days: list[date]) -> dict[date, dict[str, float]]:
    """Replay the log forwards, snapshotting holdings at the end of each day.

    Average cost is tracked alongside share count so the series can show what
    was invested at each point, not just what it was worth.
    """
    snapshots: dict[date, dict[str, float]] = {}
    shares: dict[str, float] = defaultdict(float)
    basis: dict[str, float] = defaultdict(float)

    index = 0
    for day in days:
        while index < len(rows) and _as_date(rows[index]["executed_at"]) <= day:
            row = rows[index]
            index += 1
            symbol = row["symbol"]
            quantity = row["quantity"]
            if row["side"] == "BUY":
                basis[symbol] += quantity * row["price"] + row["fees"]
                shares[symbol] += quantity
            else:
                # Reduce basis proportionally. This is average cost, which is the
                # right lens for a value chart; FIFO lots remain the source of
                # truth for realized P/L (§5).
                held = shares[symbol]
                if held > 0:
                    basis[symbol] -= basis[symbol] * (min(quantity, held) / held)
                shares[symbol] = max(0.0, held - quantity)

        snapshots[day] = {
            "__basis__": round(sum(basis.values()), 4),
            **{s: q for s, q in shares.items() if q > 1e-9},
        }
    return snapshots


async def closes_by_day(
    provider: MarketDataProvider, symbols: list[str], frm: int, to: int
) -> dict[str, dict[date, float]]:
    """Daily closes per symbol. A symbol the provider cannot serve is omitted
    rather than zero-filled — a missing price is not a price of zero."""
    out: dict[str, dict[date, float]] = {}
    for symbol in symbols:
        try:
            candles = await provider.candles(symbol, RESOLUTION, frm, to)
        except SymbolNotFound:
            logger.warning("no historical data for %s", symbol)
            continue
        except ProviderError as exc:
            logger.warning("historical data unavailable for %s: %s", symbol, exc)
            continue
        out[symbol] = {datetime.fromtimestamp(c.time, UTC).date(): c.close for c in candles}
    return out


async def portfolio_performance(
    conn: sqlite3.Connection,
    provider: MarketDataProvider,
    *,
    frm: int,
    to: int,
) -> tuple[list[PerformancePoint], list[str]]:
    """Returns (series, symbols we could not price)."""
    rows = transaction_history(conn, until=to)
    if not rows:
        return [], []

    symbols = symbols_ever_held(rows)
    closes = await closes_by_day(provider, symbols, frm, to)
    unpriced = [s for s in symbols if s not in closes]

    # The trading calendar comes from the data itself: the union of days any
    # symbol actually traded. Deriving it beats assuming weekdays-minus-holidays
    # and then disagreeing with the exchange.
    days = sorted({day for series in closes.values() for day in series})
    if not days:
        return [], unpriced

    snapshots = holdings_by_day(rows, days)
    last_seen: dict[str, float] = {}

    series: list[PerformancePoint] = []
    for day in days:
        holding = snapshots[day]
        basis = holding.get("__basis__", 0.0)
        value = 0.0
        for symbol, quantity in holding.items():
            if symbol == "__basis__":
                continue
            close = closes.get(symbol, {}).get(day)
            if close is None:
                # A symbol that did not trade that day (a halt, or a listing
                # gap) keeps its previous close rather than vanishing from the
                # total and putting a false cliff in the chart.
                close = last_seen.get(symbol)
                if close is None:
                    continue
            last_seen[symbol] = close
            value += quantity * close

        series.append(
            PerformancePoint(
                date=day.isoformat(), value=round(value, 4), cost_basis=round(basis, 4)
            )
        )
    return series, unpriced
