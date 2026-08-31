"""The lot engine — plan.md §5, §11.

Written before the engine. This is the part §11 says to unit-test hard, because
it is the only place in the app where a bug silently produces a wrong number
rather than an error: partial sells, sells crossing lots, oversell rejection,
and delete-then-rebuild consistency.

The seeded set below has hand-computed expected values. If the engine ever
disagrees with the arithmetic in these comments, the engine is wrong.
"""

import sqlite3

import pytest

from app.db.connection import connect, migrate
from app.services.portfolio import (
    NewTransaction,
    OversellError,
    delete_transaction,
    positions,
    realized_total,
    rebuild_from_transactions,
    record_transaction,
)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


def buy(db, symbol, qty, price, fees=0.0, at="2026-01-01T10:00:00Z"):
    return record_transaction(
        db,
        NewTransaction(
            symbol=symbol, side="BUY", quantity=qty, price=price, fees=fees, executed_at=at
        ),
    )


def sell(db, symbol, qty, price, fees=0.0, at="2026-01-02T10:00:00Z"):
    return record_transaction(
        db,
        NewTransaction(
            symbol=symbol, side="SELL", quantity=qty, price=price, fees=fees, executed_at=at
        ),
    )


def position_for(db, symbol):
    return next((p for p in positions(db) if p.symbol == symbol), None)


# --- the seeded set, with arithmetic done by hand ---------------------------
#
#   1. BUY  10 AAPL @ 100        -> lot A: 10 @ 100
#   2. BUY  10 AAPL @ 120        -> lot B: 10 @ 120
#   3. SELL  5 AAPL @ 150        -> takes 5 from A (oldest first)
#                                   proceeds 750, cost 500, realized +250
#   4. SELL 10 AAPL @ 130        -> takes A's remaining 5 (cost 500)
#                                   plus 5 from B          (cost 600)
#                                   proceeds 1300, cost 1100, realized +200
#   5. BUY   4 MSFT @ 50, fee 4  -> cost per share (200 + 4) / 4 = 51.00
#   6. SELL  2 MSFT @ 60, fee 2  -> proceeds 120 - 2 = 118, cost 102, realized +16
#
#   realized total          = 250 + 200 + 16 = 466.00
#   AAPL remaining          = 5 shares from lot B @ 120  -> cost basis 600.00
#   MSFT remaining          = 2 shares @ 51             -> cost basis 102.00


@pytest.fixture
def seeded(db: sqlite3.Connection) -> sqlite3.Connection:
    buy(db, "AAPL", 10, 100.0, at="2026-01-01T10:00:00Z")
    buy(db, "AAPL", 10, 120.0, at="2026-01-02T10:00:00Z")
    sell(db, "AAPL", 5, 150.0, at="2026-01-03T10:00:00Z")
    sell(db, "AAPL", 10, 130.0, at="2026-01-04T10:00:00Z")
    buy(db, "MSFT", 4, 50.0, fees=4.0, at="2026-01-05T10:00:00Z")
    sell(db, "MSFT", 2, 60.0, fees=2.0, at="2026-01-06T10:00:00Z")
    return db


def test_seeded_set_matches_hand_computed_pnl(seeded) -> None:
    assert realized_total(seeded) == pytest.approx(466.00)


def test_seeded_set_matches_hand_computed_positions(seeded) -> None:
    aapl = position_for(seeded, "AAPL")
    assert aapl.quantity == pytest.approx(5.0)
    assert aapl.cost_basis == pytest.approx(600.00)
    assert aapl.average_cost == pytest.approx(120.00)

    msft = position_for(seeded, "MSFT")
    assert msft.quantity == pytest.approx(2.0)
    assert msft.cost_basis == pytest.approx(102.00)
    assert msft.average_cost == pytest.approx(51.00)


# --- partial sells ----------------------------------------------------------


def test_a_partial_sell_leaves_the_rest_of_the_lot_open(db) -> None:
    buy(db, "AAPL", 10, 100.0)
    sell(db, "AAPL", 3, 150.0)

    pos = position_for(db, "AAPL")
    assert pos.quantity == pytest.approx(7.0)
    assert pos.cost_basis == pytest.approx(700.0), "the untouched 7 shares still cost 100 each"
    assert realized_total(db) == pytest.approx(150.0), "3 x (150 - 100)"


def test_selling_a_whole_position_closes_it_out(db) -> None:
    buy(db, "AAPL", 10, 100.0)
    sell(db, "AAPL", 10, 110.0)

    assert position_for(db, "AAPL") is None, "a zero position is not a position"
    assert realized_total(db) == pytest.approx(100.0)


# --- sells crossing lot boundaries ------------------------------------------


def test_a_sell_crossing_two_lots_consumes_the_oldest_first(db) -> None:
    buy(db, "AAPL", 10, 100.0, at="2026-01-01T10:00:00Z")
    buy(db, "AAPL", 10, 200.0, at="2026-01-02T10:00:00Z")
    sell(db, "AAPL", 15, 300.0, at="2026-01-03T10:00:00Z")

    # FIFO: all 10 @ 100 plus 5 @ 200 -> cost 1000 + 1000 = 2000
    # proceeds 15 x 300 = 4500 -> realized 2500
    assert realized_total(db) == pytest.approx(2500.0)

    pos = position_for(db, "AAPL")
    assert pos.quantity == pytest.approx(5.0)
    assert pos.average_cost == pytest.approx(200.0), "only the newer, dearer lot survives"


def test_a_sell_crossing_three_lots(db) -> None:
    buy(db, "AAPL", 2, 10.0, at="2026-01-01T10:00:00Z")
    buy(db, "AAPL", 3, 20.0, at="2026-01-02T10:00:00Z")
    buy(db, "AAPL", 5, 30.0, at="2026-01-03T10:00:00Z")
    sell(db, "AAPL", 9, 40.0, at="2026-01-04T10:00:00Z")

    # cost: 2x10 + 3x20 + 4x30 = 20 + 60 + 120 = 200
    # proceeds: 9 x 40 = 360 -> realized 160
    assert realized_total(db) == pytest.approx(160.0)
    assert position_for(db, "AAPL").quantity == pytest.approx(1.0)


def test_fifo_orders_by_execution_time_not_insertion_order(db) -> None:
    """A backdated purchase entered later is still the older lot."""
    buy(db, "AAPL", 5, 200.0, at="2026-01-05T10:00:00Z")
    buy(db, "AAPL", 5, 100.0, at="2026-01-01T10:00:00Z")  # backdated, cheaper
    sell(db, "AAPL", 5, 300.0, at="2026-01-06T10:00:00Z")

    # FIFO must take the 2026-01-01 lot: cost 500, proceeds 1500 -> 1000
    assert realized_total(db) == pytest.approx(1000.0)
    assert position_for(db, "AAPL").average_cost == pytest.approx(200.0)


# --- oversell rejection -----------------------------------------------------


def test_selling_more_than_held_is_rejected(db) -> None:
    buy(db, "AAPL", 5, 100.0)
    with pytest.raises(OversellError):
        sell(db, "AAPL", 6, 150.0)


def test_a_rejected_oversell_changes_nothing(db) -> None:
    """The whole unit of work rolls back — no orphan transaction row."""
    buy(db, "AAPL", 5, 100.0)
    before_tx = db.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]

    with pytest.raises(OversellError):
        sell(db, "AAPL", 6, 150.0)

    assert db.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == before_tx
    assert position_for(db, "AAPL").quantity == pytest.approx(5.0)
    assert realized_total(db) == pytest.approx(0.0)


def test_selling_a_symbol_never_held_is_rejected(db) -> None:
    with pytest.raises(OversellError):
        sell(db, "NVDA", 1, 100.0)


def test_selling_exactly_what_is_held_is_allowed(db) -> None:
    buy(db, "AAPL", 5, 100.0)
    sell(db, "AAPL", 5, 100.0)
    assert position_for(db, "AAPL") is None


# --- rebuild consistency ----------------------------------------------------


def snapshot(conn: sqlite3.Connection) -> dict:
    """Everything derived, in a comparable shape."""
    return {
        "positions": [
            (p.symbol, round(p.quantity, 6), round(p.cost_basis, 6)) for p in positions(conn)
        ],
        "realized": [
            tuple(round(v, 6) if isinstance(v, float) else v for v in row)
            for row in conn.execute(
                "SELECT symbol, quantity, proceeds, cost_basis, sell_tx_id"
                " FROM realized_pnl ORDER BY id"
            )
        ],
    }


def test_rebuilding_from_transactions_reproduces_the_same_state(seeded) -> None:
    """lots and realized_pnl are derived. Replaying the source of truth must
    land in exactly the same place."""
    before = snapshot(seeded)
    rebuild_from_transactions(seeded)
    assert snapshot(seeded) == before


def test_rebuilding_twice_is_idempotent(seeded) -> None:
    rebuild_from_transactions(seeded)
    once = snapshot(seeded)
    rebuild_from_transactions(seeded)
    assert snapshot(seeded) == once


def test_delete_then_rebuild_matches_never_having_recorded_it(db) -> None:
    """The §6 contract for DELETE /api/transactions/{id}: reverse and rebuild."""
    buy(db, "AAPL", 10, 100.0, at="2026-01-01T10:00:00Z")
    doomed = buy(db, "AAPL", 10, 120.0, at="2026-01-02T10:00:00Z")
    sell(db, "AAPL", 5, 150.0, at="2026-01-03T10:00:00Z")

    delete_transaction(db, doomed)
    after_delete = snapshot(db)

    # Now build the same history from scratch, without the deleted purchase.
    fresh = connect(":memory:")
    migrate(fresh)
    buy(fresh, "AAPL", 10, 100.0, at="2026-01-01T10:00:00Z")
    sell(fresh, "AAPL", 5, 150.0, at="2026-01-03T10:00:00Z")

    expected = snapshot(fresh)
    fresh.close()

    assert after_delete["positions"] == expected["positions"]
    assert [r[:4] for r in after_delete["realized"]] == [r[:4] for r in expected["realized"]]


def test_deleting_a_transaction_that_would_cause_an_oversell_is_rejected(db) -> None:
    """Removing a purchase you have already sold out of leaves the ledger
    impossible — reject rather than silently produce a negative position."""
    doomed = buy(db, "AAPL", 10, 100.0, at="2026-01-01T10:00:00Z")
    sell(db, "AAPL", 10, 150.0, at="2026-01-02T10:00:00Z")

    with pytest.raises(OversellError):
        delete_transaction(db, doomed)

    assert realized_total(db) == pytest.approx(500.0), "the ledger is untouched"


# --- money handling ---------------------------------------------------------


def test_amounts_are_rounded_to_four_decimal_places_on_write(db) -> None:
    """CLAUDE.md: REAL for v1, rounded to 4dp on write."""
    buy(db, "AAPL", 3, 10.0 / 3.0)  # 3.3333333...
    lot = db.execute("SELECT cost_per_share FROM lots").fetchone()
    assert lot["cost_per_share"] == pytest.approx(3.3333, abs=1e-9)


def test_buy_fees_join_the_cost_basis(db) -> None:
    buy(db, "AAPL", 10, 100.0, fees=25.0)
    assert position_for(db, "AAPL").cost_basis == pytest.approx(1025.0)
    assert position_for(db, "AAPL").average_cost == pytest.approx(102.50)


def test_sell_fees_come_off_the_proceeds(db) -> None:
    buy(db, "AAPL", 10, 100.0)
    sell(db, "AAPL", 10, 110.0, fees=50.0)
    assert realized_total(db) == pytest.approx(50.0), "1100 - 50 - 1000"


# --- positions are computed, never stored -----------------------------------


def test_positions_come_from_lots_not_a_stored_share_count(db) -> None:
    buy(db, "AAPL", 10, 100.0)
    tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "holdings" not in tables, "average cost loses information on a partial sell (§5)"
    assert position_for(db, "AAPL").quantity == pytest.approx(10.0)
