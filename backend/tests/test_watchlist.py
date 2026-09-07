"""Watchlist — plan.md §5, §6, §8.1."""

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.connection import connect, migrate, open_database
from app.deps import get_db, get_provider_optional
from app.main import app
from app.providers.base import ProviderError, Quote, SymbolNotFound


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    app.dependency_overrides[get_db] = lambda: conn
    app.dependency_overrides[get_provider_optional] = lambda: KnownSymbols()
    yield conn
    app.dependency_overrides.clear()
    conn.close()


@pytest.fixture
def client(db: sqlite3.Connection) -> TestClient:
    return TestClient(app)


class KnownSymbols:
    """Knows every symbol except ones starting with 'X'."""

    name = "fake"

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail

    async def quote(self, symbol: str) -> Quote:
        if self.fail:
            raise self.fail
        if symbol.startswith("X"):
            raise SymbolNotFound(symbol)
        return Quote(symbol=symbol, price=100.0, as_of=1)

    async def candles(self, *a: Any) -> list:
        return []

    async def search(self, q: str) -> list:
        return []

    def stream(self, s: set[str]) -> Any:
        raise NotImplementedError


def symbols(client: TestClient) -> list[str]:
    return client.get("/api/watchlist").json()["symbols"]


# --- seeding ----------------------------------------------------------------


def test_the_tape_starts_with_the_symbols_it_used_to_hardcode(client: TestClient) -> None:
    """Turning the watchlist on must not blank anyone's dashboard."""
    assert symbols(client) == ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"]


def test_a_removed_seed_symbol_stays_removed_across_restarts(tmp_path: Any) -> None:
    """The seed is a migration, so it runs once. Deleting AAPL must not be
    undone by the next deploy."""
    path = tmp_path / "t.db"
    first = open_database(path)
    first.execute("DELETE FROM watchlist WHERE symbol = 'AAPL'")
    first.close()

    second = open_database(path)  # migrations run again, idempotently
    rows = [r["symbol"] for r in second.execute("SELECT symbol FROM watchlist")]
    second.close()
    assert "AAPL" not in rows


# --- reading ----------------------------------------------------------------


def test_order_is_explicit_not_alphabetical(client: TestClient) -> None:
    """The tape should read the same on every load."""
    entries = client.get("/api/watchlist").json()["entries"]
    assert [e["sort_order"] for e in entries] == sorted(e["sort_order"] for e in entries)
    assert entries[0]["symbol"] == "AAPL"
    assert entries[-1]["symbol"] == "TSLA"


# --- adding -----------------------------------------------------------------


def test_adding_a_symbol_appends_it(client: TestClient) -> None:
    res = client.post("/api/watchlist", json={"symbol": "AMD"})
    assert res.status_code == 201
    assert res.json()["symbols"][-1] == "AMD"
    assert symbols(client)[-1] == "AMD"


def test_symbols_are_normalised(client: TestClient) -> None:
    client.post("/api/watchlist", json={"symbol": "  amd "})
    assert "AMD" in symbols(client)


def test_adding_a_duplicate_is_a_409(client: TestClient) -> None:
    assert client.post("/api/watchlist", json={"symbol": "AAPL"}).status_code == 409
    assert symbols(client).count("AAPL") == 1


def test_an_unknown_symbol_is_refused(client: TestClient) -> None:
    """§11: validated against the provider before storage, or it would sit on
    the tape showing dashes forever."""
    res = client.post("/api/watchlist", json={"symbol": "XYZZY"})
    assert res.status_code == 404
    assert "XYZZY" not in symbols(client)


def test_a_dead_provider_does_not_stop_you_editing_your_own_watchlist(
    client: TestClient,
) -> None:
    app.dependency_overrides[get_provider_optional] = lambda: KnownSymbols(
        fail=ProviderError("upstream down")
    )
    assert client.post("/api/watchlist", json={"symbol": "AMD"}).status_code == 201


def test_it_works_with_no_provider_configured(client: TestClient) -> None:
    app.dependency_overrides[get_provider_optional] = lambda: None
    assert client.post("/api/watchlist", json={"symbol": "AMD"}).status_code == 201


@pytest.mark.parametrize("bad", ["", "   ", "A B", "x" * 20, "AA;DROP"])
def test_invalid_symbols_are_rejected(client: TestClient, bad: str) -> None:
    assert client.post("/api/watchlist", json={"symbol": bad}).status_code == 422


def test_the_watchlist_is_capped(client: TestClient, db: sqlite3.Connection) -> None:
    """Finnhub's free socket caps at 50; refusing here makes it a clear error
    rather than a feed that silently drops rows."""
    db.execute("DELETE FROM watchlist")
    for i in range(50):
        db.execute("INSERT INTO watchlist (symbol, sort_order) VALUES (?, ?)", (f"S{i}", i))

    res = client.post("/api/watchlist", json={"symbol": "AMD"})
    assert res.status_code == 422
    assert "50" in res.json()["detail"]


# --- removing ---------------------------------------------------------------


def test_removing_a_symbol(client: TestClient) -> None:
    res = client.delete("/api/watchlist/NVDA")
    assert res.status_code == 200
    assert "NVDA" not in res.json()["symbols"]
    assert "NVDA" not in symbols(client)


def test_removing_is_case_insensitive(client: TestClient) -> None:
    assert client.delete("/api/watchlist/nvda").status_code == 200
    assert "NVDA" not in symbols(client)


def test_removing_something_absent_is_a_404(client: TestClient) -> None:
    assert client.delete("/api/watchlist/ZZZZ").status_code == 404


def test_the_watchlist_can_be_emptied(client: TestClient) -> None:
    for s in list(symbols(client)):
        client.delete(f"/api/watchlist/{s}")
    assert symbols(client) == []


# --- auth -------------------------------------------------------------------


def test_the_watchlist_is_behind_auth() -> None:
    from app.services.auth import SessionManager
    from app.services.pending import RateLimiter

    app.dependency_overrides.clear()
    app.state.sessions = SessionManager("secret", "pass")
    app.state.login_limiter = RateLimiter(capacity=10, per_seconds=300.0)
    try:
        client = TestClient(app, base_url="https://testserver")
        assert client.get("/api/watchlist").status_code == 401
        assert client.post("/api/watchlist", json={"symbol": "AMD"}).status_code == 401
        assert client.delete("/api/watchlist/AAPL").status_code == 401
    finally:
        app.state.sessions = SessionManager(None, None)
