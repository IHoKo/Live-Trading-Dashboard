"""Chat tool dispatch — plan.md §7.1, §7.2. CLAUDE.md: the model never writes.

The first section is the important one. Everything else in Phase 5 is plumbing
around the guarantee that a proposal is not a transaction.
"""

import sqlite3
from typing import Any

import pytest

from app.db.connection import connect, migrate
from app.providers.base import ProviderError, Quote, SymbolNotFound
from app.services import portfolio as engine
from app.services.chat import (
    READ_TOOLS,
    TOOLS,
    WRITE_TOOLS,
    ChatDeps,
    PortfolioReader,
    dispatch,
    system_prompt,
)
from app.services.pending import PendingActionStore


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    engine.record_transaction(
        conn,
        engine.NewTransaction(
            symbol="AAPL",
            side="BUY",
            quantity=10,
            price=100.0,
            executed_at="2026-01-01T10:00:00Z",
        ),
    )
    yield conn
    conn.close()


class FakeProvider:
    name = "fake"

    def __init__(self, price: float = 150.0, fail: Exception | None = None) -> None:
        self.price = price
        self.fail = fail

    async def quote(self, symbol: str) -> Quote:
        if self.fail:
            raise self.fail
        return Quote(symbol=symbol, price=self.price, change_pct=1.25, as_of=1_700_000_000)

    async def candles(self, *a: Any) -> list:
        return []

    async def search(self, q: str) -> list:
        return []

    def stream(self, s: set[str]) -> Any:
        raise NotImplementedError


@pytest.fixture
def deps(db: sqlite3.Connection) -> ChatDeps:
    return ChatDeps(
        reader=PortfolioReader(db), provider=FakeProvider(), pending=PendingActionStore()
    )


def snapshot(conn: sqlite3.Connection) -> dict:
    """Everything a write could possibly change."""
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("transactions", "lots", "realized_pnl", "watchlist", "idempotency_keys")
    }


# --- the guarantee ----------------------------------------------------------


async def test_no_tool_in_the_dispatch_path_writes_to_the_database(
    db: sqlite3.Connection, deps: ChatDeps
) -> None:
    """Run every tool, including both write tools, and assert the ledger is
    byte-for-byte unchanged."""
    before = snapshot(db)

    await dispatch("get_quote", {"symbols": ["AAPL"]}, deps)
    await dispatch("get_candles", {"symbol": "AAPL", "range": "1D"}, deps)
    await dispatch("get_portfolio", {}, deps)
    await dispatch("get_transactions", {}, deps)
    await dispatch("propose_add_shares", {"symbol": "MSFT", "quantity": 5}, deps)
    await dispatch("propose_remove_shares", {"symbol": "AAPL", "quantity": 5}, deps)

    assert snapshot(db) == before, "a tool wrote to the ledger"


def test_the_dispatcher_is_not_given_anything_that_can_write(deps: ChatDeps) -> None:
    """Structural guard: ChatDeps carries a reader, not a connection."""
    exposed = {name: type(value).__name__ for name, value in vars(deps).items()}
    assert "Connection" not in exposed.values()
    assert isinstance(deps.reader, PortfolioReader)
    assert not hasattr(deps.reader, "execute")
    assert {"positions", "realized_total", "shares_held", "transactions"} == {
        m for m in dir(deps.reader) if not m.startswith("_")
    }, "PortfolioReader grew a method — check it cannot write"


async def test_a_proposal_produces_a_pending_action_not_a_transaction(
    db: sqlite3.Connection, deps: ChatDeps
) -> None:
    text, payload = await dispatch(
        "propose_add_shares", {"symbol": "MSFT", "quantity": 5, "price": 400.0}, deps
    )

    assert payload is not None
    assert payload["symbol"] == "MSFT"
    assert payload["quantity"] == 5
    assert payload["total"] == 2000.0
    assert len(deps.pending) == 1
    assert "nothing has been recorded" in text
    assert db.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 1


def test_tool_definitions_split_cleanly_into_read_and_propose() -> None:
    names = {t.get("name") for t in TOOLS}
    assert WRITE_TOOLS <= names
    assert READ_TOOLS <= names
    assert all(n.startswith("propose_") for n in WRITE_TOOLS), "write tools propose only"
    for tool in TOOLS:
        if tool.get("name") in WRITE_TOOLS:
            assert "Does NOT execute" in tool["description"]


def test_the_web_search_tool_version_is_the_current_one() -> None:
    """§7.1 shows web_search_20250305; verified against the live API that
    web_search_20260209 is current for claude-sonnet-5."""
    search = next(t for t in TOOLS if t.get("name") == "web_search")
    assert search["type"] == "web_search_20260209"


# --- validation before a card is ever shown (§7.2) --------------------------


async def test_proposing_a_sale_larger_than_the_holding_is_refused(deps: ChatDeps) -> None:
    text, payload = await dispatch(
        "propose_remove_shares", {"symbol": "AAPL", "quantity": 999}, deps
    )
    assert payload is None, "no confirm card for something that cannot execute"
    assert "only 10 held" in text
    assert len(deps.pending) == 0


async def test_proposing_a_sale_of_something_never_held_is_refused(deps: ChatDeps) -> None:
    _, payload = await dispatch("propose_remove_shares", {"symbol": "NVDA", "quantity": 1}, deps)
    assert payload is None


async def test_an_omitted_price_is_filled_from_the_quote_and_labelled(deps: ChatDeps) -> None:
    """§7.2: the card must say the price was filled in, so nobody confirms a
    number they never gave."""
    text, payload = await dispatch("propose_add_shares", {"symbol": "MSFT", "quantity": 2}, deps)
    assert payload["price"] == 150.0
    assert payload["price_source"] == "quote"
    assert "current price" in text


async def test_a_user_supplied_price_is_marked_as_theirs(deps: ChatDeps) -> None:
    _, payload = await dispatch(
        "propose_add_shares", {"symbol": "MSFT", "quantity": 2, "price": 99.0}, deps
    )
    assert payload["price"] == 99.0
    assert payload["price_source"] == "user"


async def test_an_unknown_symbol_is_refused_before_proposing(db: sqlite3.Connection) -> None:
    deps = ChatDeps(
        reader=PortfolioReader(db),
        provider=FakeProvider(fail=SymbolNotFound("nope")),
        pending=PendingActionStore(),
    )
    _, payload = await dispatch("propose_add_shares", {"symbol": "XYZZY", "quantity": 1}, deps)
    assert payload is None


async def test_a_dead_provider_does_not_block_recording_a_trade_you_made(
    db: sqlite3.Connection,
) -> None:
    """You know what you bought; a broken price feed shouldn't veto it."""
    deps = ChatDeps(
        reader=PortfolioReader(db),
        provider=FakeProvider(fail=ProviderError("upstream down")),
        pending=PendingActionStore(),
    )
    _, payload = await dispatch(
        "propose_add_shares", {"symbol": "MSFT", "quantity": 2, "price": 400.0}, deps
    )
    assert payload is not None


async def test_zero_quantity_is_refused(deps: ChatDeps) -> None:
    _, payload = await dispatch("propose_add_shares", {"symbol": "MSFT", "quantity": 0}, deps)
    assert payload is None


# --- read tools -------------------------------------------------------------


async def test_get_portfolio_reports_positions_and_realized_pnl(deps: ChatDeps) -> None:
    text, _ = await dispatch("get_portfolio", {}, deps)
    assert "AAPL" in text
    assert "10 shares" in text
    assert "Realized P/L" in text


async def test_get_portfolio_on_an_empty_ledger(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM lots")
    deps = ChatDeps(reader=PortfolioReader(db), provider=None, pending=PendingActionStore())
    text, _ = await dispatch("get_portfolio", {}, deps)
    assert "empty" in text


async def test_get_quote_reports_price_and_day_change(deps: ChatDeps) -> None:
    text, _ = await dispatch("get_quote", {"symbols": ["AAPL"]}, deps)
    assert "150.00" in text
    assert "+1.25%" in text


async def test_get_quote_without_a_provider_says_so(db: sqlite3.Connection) -> None:
    deps = ChatDeps(reader=PortfolioReader(db), provider=None, pending=PendingActionStore())
    text, _ = await dispatch("get_quote", {"symbols": ["AAPL"]}, deps)
    assert "not configured" in text


async def test_get_candles_explains_a_provider_refusal_rather_than_estimating(
    db: sqlite3.Connection,
) -> None:
    """Finnhub's free tier 403s on candles; the model must say so, not guess."""

    class NoCandles(FakeProvider):
        async def candles(self, *a: Any) -> list:
            raise ProviderError("finnhub denied /stock/candle (HTTP 403)")

    deps = ChatDeps(reader=PortfolioReader(db), provider=NoCandles(), pending=PendingActionStore())
    text, _ = await dispatch("get_candles", {"symbol": "AAPL", "range": "1M"}, deps)
    assert "unavailable" in text
    assert "rather than estimating" in text


async def test_a_failing_tool_returns_text_not_an_exception(deps: ChatDeps) -> None:
    """A broken tool should let Claude explain the problem, not kill the turn."""
    text, payload = await dispatch("get_transactions", {"limit": "not a number"}, deps)
    assert payload is None
    assert "failed" in text.lower()


async def test_an_unknown_tool_name_is_reported_not_raised(deps: ChatDeps) -> None:
    text, _ = await dispatch("rm_rf_everything", {}, deps)
    assert "Unknown tool" in text


# --- system prompt (§7.3, §11) ---------------------------------------------


def test_the_system_prompt_forbids_recommendations() -> None:
    prompt = system_prompt()
    assert "Never recommend buying or selling" in prompt
    assert "never give price targets" in prompt
    assert "never predict" in prompt


def test_the_system_prompt_says_the_model_cannot_execute_trades() -> None:
    assert "cannot execute a trade" in system_prompt()


def test_the_system_prompt_states_the_date_and_market_state() -> None:
    import datetime as dt

    prompt = system_prompt(dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.UTC))  # a Sunday
    assert "2026" in prompt
    assert "market is currently closed" in prompt
