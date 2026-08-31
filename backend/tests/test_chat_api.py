"""Chat endpoints — plan.md §6, §7.2, §11."""

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.connection import connect, migrate
from app.deps import get_db, get_provider_optional
from app.main import app
from app.services import portfolio as engine
from app.services.pending import PendingActionStore, RateLimiter


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
    app.dependency_overrides[get_db] = lambda: conn
    app.dependency_overrides[get_provider_optional] = lambda: None
    app.state.pending = PendingActionStore()
    app.state.chat_limiter = RateLimiter()
    app.state.anthropic = None
    yield conn
    app.dependency_overrides.clear()
    conn.close()


@pytest.fixture
def client(db: sqlite3.Connection) -> TestClient:
    return TestClient(app)


def propose(kind: str = "add", **kw: Any):
    body = {
        "kind": kind,
        "symbol": "MSFT",
        "quantity": 5,
        "price": 400.0,
        "price_source": "user",
        "executed_at": "2026-03-01T10:00:00Z",
    }
    body.update(kw)
    return app.state.pending.propose(**body)


# --- confirm is the only write path (§7.2) ---------------------------------


def test_confirming_a_proposal_records_the_transaction(client: TestClient, db) -> None:
    action = propose()
    res = client.post("/api/chat/confirm", json={"action_id": action.id, "approved": True})

    assert res.status_code == 200
    assert res.json()["status"] == "recorded"
    row = db.execute("SELECT * FROM transactions WHERE symbol = 'MSFT'").fetchone()
    assert row["quantity"] == 5
    assert row["source"] == "chat", "chat-originated trades are tagged as such"


def test_declining_a_proposal_records_nothing(client: TestClient, db) -> None:
    action = propose()
    res = client.post("/api/chat/confirm", json={"action_id": action.id, "approved": False})

    assert res.json()["status"] == "cancelled"
    assert db.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 1
    assert len(app.state.pending) == 0, "a declined action is discarded"


def test_a_confirmation_cannot_be_replayed(client: TestClient, db) -> None:
    """§11 idempotency: a retried confirm must not double-book a purchase."""
    action = propose()
    first = client.post("/api/chat/confirm", json={"action_id": action.id, "approved": True})
    second = client.post("/api/chat/confirm", json={"action_id": action.id, "approved": True})

    assert first.status_code == 200
    assert second.status_code == 410
    assert (
        db.execute("SELECT COUNT(*) AS n FROM transactions WHERE symbol='MSFT'").fetchone()["n"]
        == 1
    )


def test_an_expired_proposal_is_refused(client: TestClient, db) -> None:
    """§7.2: pending actions expire after 5 minutes."""
    now = {"t": 1000.0}
    app.state.pending = PendingActionStore(ttl=300.0, clock=lambda: now["t"])
    action = propose()

    now["t"] += 301.0
    res = client.post("/api/chat/confirm", json={"action_id": action.id, "approved": True})

    assert res.status_code == 410
    assert "expired" in res.json()["detail"]
    assert db.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 1


def test_an_unknown_action_id_is_refused(client: TestClient) -> None:
    res = client.post("/api/chat/confirm", json={"action_id": "nope", "approved": True})
    assert res.status_code == 410


def test_confirming_a_sale_that_became_impossible_is_a_409(client: TestClient, db) -> None:
    """The holding can change between proposal and confirmation."""
    action = propose(kind="remove", symbol="AAPL", quantity=10, price=150.0)
    engine.record_transaction(
        db,
        engine.NewTransaction(
            symbol="AAPL",
            side="SELL",
            quantity=10,
            price=150.0,
            executed_at="2026-02-01T10:00:00Z",
        ),
    )
    res = client.post("/api/chat/confirm", json={"action_id": action.id, "approved": True})
    assert res.status_code == 409


def test_a_confirmed_trade_is_written_into_the_conversation(client: TestClient, db) -> None:
    action = propose()
    client.post("/api/chat/confirm", json={"action_id": action.id, "approved": True})
    rows = db.execute("SELECT content FROM chat_messages").fetchall()
    assert any("Recorded" in r["content"] for r in rows)


# --- stream guards ----------------------------------------------------------


def test_stream_is_503_without_an_api_key(client: TestClient) -> None:
    res = client.post("/api/chat/stream", json={"message": "hi"})
    assert res.status_code == 503
    assert "ANTHROPIC_API_KEY" in res.json()["detail"]


def test_the_rate_limit_returns_429_with_retry_after(client: TestClient) -> None:
    """§11: 20 messages/hour, so a stuck retry loop can't run up a bill."""
    app.state.anthropic = object()  # get past the config check
    app.state.chat_limiter = RateLimiter(capacity=2, per_seconds=3600.0)

    codes = [client.post("/api/chat/stream", json={"message": "hi"}).status_code for _ in range(4)]
    assert codes.count(429) == 2, f"expected 2 allowed then 2 limited, got {codes}"

    limited = client.post("/api/chat/stream", json={"message": "hi"})
    assert "Retry-After" in limited.headers


@pytest.mark.parametrize("message", ["", "x" * 5000])
def test_invalid_messages_are_rejected(client: TestClient, message: str) -> None:
    assert client.post("/api/chat/stream", json={"message": message}).status_code == 422


# --- history (§7.4) ---------------------------------------------------------


def test_history_round_trips_content_blocks(client: TestClient, db) -> None:
    """Tool calls must survive a reload, so blocks are stored as JSON."""
    db.execute(
        "INSERT INTO chat_messages (role, content) VALUES (?, ?)",
        ("assistant", '[{"type": "tool_use", "id": "t1", "name": "get_portfolio", "input": {}}]'),
    )
    body = client.get("/api/chat/history").json()
    assert body["messages"][0]["content"][0]["type"] == "tool_use"


def test_history_can_be_cleared(client: TestClient, db) -> None:
    db.execute("INSERT INTO chat_messages (role, content) VALUES ('user', '\"hi\"')")
    assert client.delete("/api/chat/history").status_code == 204
    assert client.get("/api/chat/history").json()["messages"] == []


def test_a_corrupt_history_row_does_not_break_the_conversation(client: TestClient, db) -> None:
    db.execute("INSERT INTO chat_messages (role, content) VALUES ('user', 'not json')")
    db.execute("INSERT INTO chat_messages (role, content) VALUES ('user', '\"fine\"')")
    assert len(client.get("/api/chat/history").json()["messages"]) == 1
