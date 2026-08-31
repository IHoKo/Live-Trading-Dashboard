"""FIFO lot engine — plan.md §5, §11. CLAUDE.md: transactions are the truth.

The rules this file exists to enforce:

* `transactions` is **append-only**. Nothing here updates a transaction row.
* `lots` and `realized_pnl` are **derived**, and `rebuild_from_transactions`
  can reproduce both from the transaction log alone. If it ever can't, the
  derived tables have become a second source of truth and this design has
  failed.
* **Positions are computed**, never stored. There is deliberately no
  `holdings.shares` column: average cost silently loses information the moment
  you sell part of a position.
* A SELL consumes the **oldest open lots first** and writes a `realized_pnl`
  row for what it closed.

Synchronous on purpose — callers wrap a whole unit of work in
`asyncio.to_thread`. That keeps this module a plain function the tests can
drive directly, which is what makes it testable to the standard §11 asks for.
"""

import sqlite3
from dataclasses import dataclass
from typing import Literal

from app.db.connection import transaction

Side = Literal["BUY", "SELL"]

# CLAUDE.md: money is REAL for v1, rounded to 4dp on write and formatted with
# Decimal at the presentation layer.
MONEY_DP = 4


class PortfolioError(Exception):
    """Base for ledger errors the caller should surface, not swallow."""


class OversellError(PortfolioError):
    """A sale for more shares than are held. Rejected before anything is written."""


class TransactionNotFound(PortfolioError):
    pass


@dataclass(frozen=True)
class NewTransaction:
    symbol: str
    side: Side
    quantity: float
    price: float
    executed_at: str
    fees: float = 0.0
    note: str | None = None
    source: str = "ui"


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    cost_basis: float

    @property
    def average_cost(self) -> float:
        return self.cost_basis / self.quantity if self.quantity else 0.0


def _money(value: float) -> float:
    return round(value, MONEY_DP)


# --- reads ------------------------------------------------------------------


def positions(conn: sqlite3.Connection) -> list[Position]:
    """One query over open lots. Nothing is stored; this is the position."""
    rows = conn.execute(
        """
        SELECT symbol,
               SUM(quantity_open)                  AS quantity,
               SUM(quantity_open * cost_per_share) AS cost_basis
        FROM lots
        WHERE quantity_open > 0
        GROUP BY symbol
        HAVING SUM(quantity_open) > 0
        ORDER BY symbol
        """
    ).fetchall()
    return [
        Position(
            symbol=row["symbol"],
            quantity=row["quantity"],
            cost_basis=_money(row["cost_basis"]),
        )
        for row in rows
    ]


def realized_total(conn: sqlite3.Connection, symbol: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(proceeds - cost_basis), 0.0) AS total FROM realized_pnl"
    params: tuple = ()
    if symbol:
        sql += " WHERE symbol = ?"
        params = (symbol,)
    return _money(conn.execute(sql, params).fetchone()["total"])


def shares_held(conn: sqlite3.Connection, symbol: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity_open), 0.0) AS q FROM lots WHERE symbol = ?", (symbol,)
    ).fetchone()
    return row["q"]


# --- writes -----------------------------------------------------------------


def record_transaction(conn: sqlite3.Connection, tx: NewTransaction) -> int:
    """Append a transaction and apply it to the lot ledger, atomically.

    A rejected sale must leave nothing behind — no orphan transaction row for a
    trade that never happened — so the append and the lot updates share one
    transaction.
    """
    symbol = tx.symbol.strip().upper()
    if tx.quantity <= 0:
        raise PortfolioError("Quantity must be greater than zero.")
    if tx.price < 0:
        raise PortfolioError("Price cannot be negative.")

    with transaction(conn):
        if tx.side == "SELL":
            held = shares_held(conn, symbol)
            if tx.quantity > held + 1e-9:
                raise OversellError(f"Cannot sell {tx.quantity:g} {symbol}: only {held:g} held.")

        cursor = conn.execute(
            """
            INSERT INTO transactions (symbol, side, quantity, price, fees, executed_at, note, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                tx.side,
                tx.quantity,
                _money(tx.price),
                _money(tx.fees),
                tx.executed_at,
                tx.note,
                tx.source,
            ),
        )
        tx_id = int(cursor.lastrowid)
        _apply(
            conn,
            tx_id,
            symbol,
            tx.side,
            tx.quantity,
            _money(tx.price),
            _money(tx.fees),
            tx.executed_at,
        )
        return tx_id


def delete_transaction(conn: sqlite3.Connection, tx_id: int) -> None:
    """Remove a transaction and rebuild the derived tables (§6).

    Rebuilding rather than unwinding is the point of keeping lots derivable:
    reversing a sell that crossed three lots by hand is where the subtle bugs
    live. Replaying is boring, and boring is correct.
    """
    with transaction(conn):
        row = conn.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if row is None:
            raise TransactionNotFound(f"No transaction {tx_id}.")

        # lots and realized_pnl carry foreign keys to this row, so the derived
        # tables have to go first. They are about to be replayed anyway.
        _clear_derived(conn)
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        # Raises OversellError if the remaining history is impossible — e.g.
        # deleting a purchase you have already sold out of. The rollback then
        # restores both the deleted row and the derived tables.
        _replay(conn)


def rebuild_from_transactions(conn: sqlite3.Connection) -> None:
    """Discard every derived row and replay the log. Must be exactly reproducible."""
    with transaction(conn):
        _clear_derived(conn)
        _replay(conn)


# --- internals --------------------------------------------------------------


def _clear_derived(conn: sqlite3.Connection) -> None:
    """Drop everything reproducible. Caller must already hold a transaction."""
    conn.execute("DELETE FROM lots")
    conn.execute("DELETE FROM realized_pnl")


def _replay(conn: sqlite3.Connection) -> None:
    """Rebuild lots and realized_pnl from the transaction log, in time order."""
    rows = conn.execute(
        """
        SELECT id, symbol, side, quantity, price, fees, executed_at
        FROM transactions
        ORDER BY executed_at, id
        """
    ).fetchall()

    for row in rows:
        held = shares_held(conn, row["symbol"])
        if row["side"] == "SELL" and row["quantity"] > held + 1e-9:
            raise OversellError(
                f"Transaction {row['id']} sells {row['quantity']:g} {row['symbol']}"
                f" but only {held:g} would be held at that point."
            )
        _apply(
            conn,
            row["id"],
            row["symbol"],
            row["side"],
            row["quantity"],
            row["price"],
            row["fees"],
            row["executed_at"],
        )


def _apply(
    conn: sqlite3.Connection,
    tx_id: int,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    fees: float,
    executed_at: str,
) -> None:
    if side == "BUY":
        _open_lot(conn, tx_id, symbol, quantity, price, fees, executed_at)
    else:
        _consume_lots(conn, tx_id, symbol, quantity, price, fees, executed_at)


def _open_lot(
    conn: sqlite3.Connection,
    tx_id: int,
    symbol: str,
    quantity: float,
    price: float,
    fees: float,
    executed_at: str,
) -> None:
    # Buy-side fees are part of what the shares cost you, so they belong in the
    # basis rather than being booked as a separate expense.
    cost_per_share = _money((quantity * price + fees) / quantity)
    conn.execute(
        """
        INSERT INTO lots (symbol, quantity_open, cost_per_share, opened_at, tx_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (symbol, quantity, cost_per_share, executed_at, tx_id),
    )


def _consume_lots(
    conn: sqlite3.Connection,
    tx_id: int,
    symbol: str,
    quantity: float,
    price: float,
    fees: float,
    executed_at: str,
) -> None:
    """FIFO: oldest open lots first, by execution time then insertion order.

    Ordering by `opened_at` rather than `id` means a backdated purchase entered
    after the fact is still treated as the older lot, which is what FIFO means.
    """
    remaining = quantity
    # Sell-side fees reduce what you actually received.
    proceeds_total = _money(quantity * price - fees)
    cost_total = 0.0

    lots = conn.execute(
        """
        SELECT id, quantity_open, cost_per_share
        FROM lots
        WHERE symbol = ? AND quantity_open > 0
        ORDER BY opened_at, id
        """,
        (symbol,),
    ).fetchall()

    for lot in lots:
        if remaining <= 1e-9:
            break
        take = min(remaining, lot["quantity_open"])
        cost_total += take * lot["cost_per_share"]
        remaining -= take

        left = lot["quantity_open"] - take
        if left <= 1e-9:
            conn.execute("DELETE FROM lots WHERE id = ?", (lot["id"],))
        else:
            conn.execute("UPDATE lots SET quantity_open = ? WHERE id = ?", (left, lot["id"]))

    if remaining > 1e-9:  # pragma: no cover - callers check first
        raise OversellError(f"Ran out of lots consuming {quantity:g} {symbol}.")

    conn.execute(
        """
        INSERT INTO realized_pnl (symbol, quantity, proceeds, cost_basis, closed_at, sell_tx_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (symbol, quantity, proceeds_total, _money(cost_total), executed_at, tx_id),
    )
