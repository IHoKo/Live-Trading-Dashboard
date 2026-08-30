"""/ws/prices — the connection contract from plan.md §6."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.price_hub import PriceHub
from tests.test_price_hub import FakeProvider


@pytest.fixture
def hub() -> Any:
    h = PriceHub(FakeProvider())
    h._running = True  # no background loops; tests drive flush() directly
    app.state.hub = h
    yield h
    app.state.hub = None


def test_socket_sends_status_on_connect(hub: PriceHub) -> None:
    with TestClient(app).websocket_connect("/ws/prices") as sock:
        msg = sock.receive_json()
        assert msg["type"] == "status"
        assert msg["provider"] == "fake"
        assert "market_open" in msg


def test_subscribe_registers_symbols_with_the_hub(hub: PriceHub) -> None:
    with TestClient(app).websocket_connect("/ws/prices") as sock:
        sock.receive_json()  # opening status
        sock.send_json({"type": "subscribe", "symbols": ["aapl", "NVDA"]})
        sock.receive_json()  # status echo after subscribe
        assert hub.tracked_symbols() == {"AAPL", "NVDA"}


def test_subscribe_replays_cached_prices_immediately(hub: PriceHub) -> None:
    """A new tab should paint prices, not empty rows, while it waits for a print."""
    hub.record("AAPL", 213.44, 1_754_800_000_000)

    with TestClient(app).websocket_connect("/ws/prices") as sock:
        sock.receive_json()
        sock.send_json({"type": "subscribe", "symbols": ["AAPL"]})
        tick = sock.receive_json()

    assert tick["type"] == "tick"
    assert tick["s"] == "AAPL"
    assert tick["p"] == 213.44
    assert tick["t"] == 1_754_800_000_000


def test_unsubscribe_removes_the_symbol(hub: PriceHub) -> None:
    with TestClient(app).websocket_connect("/ws/prices") as sock:
        sock.receive_json()
        sock.send_json({"type": "subscribe", "symbols": ["AAPL", "NVDA"]})
        sock.receive_json()
        sock.send_json({"type": "unsubscribe", "symbols": ["NVDA"]})
        sock.send_json({"type": "subscribe", "symbols": []})  # round-trip to sync
        sock.receive_json()
        assert hub.tracked_symbols() == {"AAPL"}


def test_disconnect_deregisters_the_subscriber(hub: PriceHub) -> None:
    with TestClient(app).websocket_connect("/ws/prices") as sock:
        sock.receive_json()
        sock.send_json({"type": "subscribe", "symbols": ["AAPL"]})
        sock.receive_json()
        assert hub.tracked_symbols() == {"AAPL"}

    assert hub.tracked_symbols() == set(), "a closed tab must not keep a feed alive"


def test_garbage_symbols_are_ignored(hub: PriceHub) -> None:
    with TestClient(app).websocket_connect("/ws/prices") as sock:
        sock.receive_json()
        sock.send_json({"type": "subscribe", "symbols": [None, 42, "", "  ", "AAPL"]})
        sock.receive_json()
        assert hub.tracked_symbols() == {"AAPL"}


def test_socket_closes_cleanly_when_no_hub_is_configured() -> None:
    app.state.hub = None
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        TestClient(app).websocket_connect("/ws/prices") as sock,
    ):
        sock.receive_json()
