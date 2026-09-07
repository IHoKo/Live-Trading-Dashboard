"""Transaction and portfolio endpoints — plan.md §6, Phase 3."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.connection import connect, migrate, open_database
from app.deps import get_db, get_provider_optional
from app.main import app
from app.providers.base import ProviderError, Quote


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    app.dependency_overrides[get_db] = lambda: conn
    app.dependency_overrides[get_provider_optional] = lambda: None
    yield conn
    app.dependency_overrides.clear()
    conn.close()


@pytest.fixture
def client(db: sqlite3.Connection) -> TestClient:
    return TestClient(app)


class PricedProvider:
    name = "fake"

    def __init__(self, prices: dict[str, float], fail: bool = False) -> None:
        self.prices = prices
        self.fail = fail

    async def quote(self, symbol: str) -> Quote:
        if self.fail:
            raise ProviderError("upstream is down")
        return Quote(symbol=symbol, price=self.prices[symbol], change_pct=1.5, as_of=1_700_000_000)

    async def candles(self, *a: Any) -> list:
        return []

    async def search(self, q: str) -> list:
        return []

    def stream(self, s: set[str]) -> Any:
        raise NotImplementedError


def post(client: TestClient, **kw: Any):
    body = {"symbol": "AAPL", "side": "BUY", "quantity": 10, "price": 100.0}
    body.update(kw)
    return client.post("/api/transactions", json=body)


# --- POST /api/transactions -------------------------------------------------


def test_recording_a_buy_returns_the_stored_row(client: TestClient) -> None:
    res = post(client, executed_at="2026-01-01T10:00:00Z")
    assert res.status_code == 201
    body = res.json()
    assert body["symbol"] == "AAPL"
    assert body["side"] == "BUY"
    assert body["source"] == "ui"
    assert body["id"] > 0


def test_symbols_are_normalised_on_write(client: TestClient) -> None:
    assert post(client, symbol=" aapl ").json()["symbol"] == "AAPL"


def test_executed_at_defaults_to_now(client: TestClient) -> None:
    assert post(client).json()["executed_at"].startswith("20")


@pytest.mark.parametrize(
    ("field", "value"),
    [("quantity", 0), ("quantity", -1), ("price", -5), ("side", "HOLD"), ("symbol", "")],
)
def test_invalid_input_is_rejected(client: TestClient, field: str, value: Any) -> None:
    assert post(client, **{field: value}).status_code == 422


def test_overselling_is_a_409_not_a_500(client: TestClient) -> None:
    """The user's ledger disagrees with them; that is a conflict, not a crash."""
    post(client, quantity=5)
    res = post(client, side="SELL", quantity=6, price=150.0)
    assert res.status_code == 409
    assert "only 5 held" in res.json()["detail"]


def test_a_rejected_oversell_leaves_no_transaction_behind(client: TestClient) -> None:
    post(client, quantity=5)
    post(client, side="SELL", quantity=6, price=150.0)
    assert client.get("/api/transactions").json()["total"] == 1


# --- GET /api/transactions --------------------------------------------------


def test_history_is_newest_first_and_paginated(client: TestClient) -> None:
    for day in range(1, 6):
        post(client, quantity=1, executed_at=f"2026-01-0{day}T10:00:00Z")

    page = client.get("/api/transactions", params={"limit": 2}).json()
    assert page["total"] == 5
    assert page["limit"] == 2
    assert [t["executed_at"][:10] for t in page["transactions"]] == ["2026-01-05", "2026-01-04"]

    second = client.get("/api/transactions", params={"limit": 2, "offset": 2}).json()
    assert [t["executed_at"][:10] for t in second["transactions"]] == ["2026-01-03", "2026-01-02"]


def test_history_can_be_filtered_by_symbol(client: TestClient) -> None:
    post(client, symbol="AAPL")
    post(client, symbol="MSFT")
    page = client.get("/api/transactions", params={"symbol": "msft"}).json()
    assert page["total"] == 1
    assert page["transactions"][0]["symbol"] == "MSFT"


# --- DELETE /api/transactions/{id} ------------------------------------------


def test_deleting_reverses_and_rebuilds(client: TestClient) -> None:
    first = post(client, quantity=10, price=100.0, executed_at="2026-01-01T10:00:00Z").json()
    post(client, quantity=10, price=200.0, executed_at="2026-01-02T10:00:00Z")

    assert client.delete(f"/api/transactions/{first['id']}").status_code == 204

    portfolio = client.get("/api/portfolio").json()
    assert portfolio["positions"][0]["quantity"] == 10
    assert portfolio["positions"][0]["average_cost"] == 200.0


def test_deleting_a_missing_transaction_is_404(client: TestClient) -> None:
    assert client.delete("/api/transactions/9999").status_code == 404


def test_deleting_a_purchase_already_sold_out_of_is_409(client: TestClient) -> None:
    bought = post(client, quantity=10, executed_at="2026-01-01T10:00:00Z").json()
    post(client, side="SELL", quantity=10, price=150.0, executed_at="2026-01-02T10:00:00Z")

    res = client.delete(f"/api/transactions/{bought['id']}")
    assert res.status_code == 409
    assert "Delete the later sale first" in res.json()["detail"]


# --- GET /api/portfolio -----------------------------------------------------


def test_portfolio_is_empty_before_anything_is_recorded(client: TestClient) -> None:
    body = client.get("/api/portfolio").json()
    assert body["positions"] == []
    assert body["cost_basis_total"] == 0
    assert body["realized_pnl_total"] == 0


def test_portfolio_reports_cost_basis_without_a_provider(client: TestClient) -> None:
    """Cost basis and realized P/L are ours — they do not need a price feed."""
    post(client, quantity=10, price=100.0)
    body = client.get("/api/portfolio").json()

    assert body["cost_basis_total"] == 1000.0
    assert body["positions"][0]["last_price"] is None
    assert body["market_value_total"] is None, "not guessed when unknown"
    assert body["unpriced_symbols"] == ["AAPL"]


def test_portfolio_values_positions_when_prices_are_available(
    client: TestClient, db: sqlite3.Connection
) -> None:
    app.dependency_overrides[get_provider_optional] = lambda: PricedProvider(
        {"AAPL": 150.0, "MSFT": 50.0}
    )
    post(client, symbol="AAPL", quantity=10, price=100.0)
    post(client, symbol="MSFT", quantity=10, price=40.0)

    body = client.get("/api/portfolio").json()
    by_symbol = {p["symbol"]: p for p in body["positions"]}

    assert by_symbol["AAPL"]["market_value"] == 1500.0
    assert by_symbol["AAPL"]["unrealized_pnl"] == 500.0
    assert by_symbol["AAPL"]["unrealized_pct"] == 50.0
    assert body["market_value_total"] == 2000.0
    assert body["unrealized_pnl_total"] == 600.0  # 2000 - (1000 + 400)


def test_allocation_percentages_sum_to_one_hundred(client: TestClient) -> None:
    app.dependency_overrides[get_provider_optional] = lambda: PricedProvider(
        {"AAPL": 150.0, "MSFT": 50.0}
    )
    post(client, symbol="AAPL", quantity=10, price=100.0)
    post(client, symbol="MSFT", quantity=10, price=40.0)

    allocations = [p["allocation_pct"] for p in client.get("/api/portfolio").json()["positions"]]
    assert sum(allocations) == pytest.approx(100.0)


def test_a_dead_provider_does_not_blank_the_portfolio(client: TestClient) -> None:
    """§4 rung 3 applied to the portfolio: degrade the view, don't refuse it."""
    app.dependency_overrides[get_provider_optional] = lambda: PricedProvider({}, fail=True)
    post(client, quantity=10, price=100.0)

    body = client.get("/api/portfolio").json()
    assert body["cost_basis_total"] == 1000.0
    assert body["market_value_total"] is None
    assert body["unpriced_symbols"] == ["AAPL"]


def test_realized_pnl_survives_in_the_portfolio_view(client: TestClient) -> None:
    post(client, quantity=10, price=100.0, executed_at="2026-01-01T10:00:00Z")
    post(client, side="SELL", quantity=10, price=130.0, executed_at="2026-01-02T10:00:00Z")

    body = client.get("/api/portfolio").json()
    assert body["positions"] == [], "position is closed"
    assert body["realized_pnl_total"] == 300.0


# --- persistence ------------------------------------------------------------


def test_data_survives_reopening_the_database(tmp_path: Path) -> None:
    """§9.5's real test: a transaction must outlive the process. If this fails
    on Fly, the DB is on the ephemeral rootfs rather than the volume."""
    db_path = tmp_path / "ticker.db"

    first = open_database(db_path)
    app.dependency_overrides[get_db] = lambda: first
    app.dependency_overrides[get_provider_optional] = lambda: None
    post(TestClient(app), quantity=7, price=100.0)
    first.close()

    second = open_database(db_path)  # migrations run again, idempotently
    app.dependency_overrides[get_db] = lambda: second
    body = TestClient(app).get("/api/portfolio").json()
    second.close()
    app.dependency_overrides.clear()

    assert body["positions"][0]["quantity"] == 7


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "ticker.db"
    for _ in range(3):
        conn = open_database(db_path)
        conn.close()
    conn = open_database(db_path)
    applied = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    conn.close()

    from app.db.connection import MIGRATIONS_DIR

    on_disk = len(list(MIGRATIONS_DIR.glob("*.sql")))
    assert applied == on_disk, "each migration is recorded once, however many boots"


def test_wal_is_enabled(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "ticker.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"
