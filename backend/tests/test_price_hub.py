"""PriceHub: coalescing, fan-out, and the three fallback rungs.

The coalescing tests are the important ones. A liquid symbol prints hundreds of
trades per second; if each one became a WebSocket message the tab would lock up.
Everything else in Phase 2 is plumbing around that single guarantee.
"""

import asyncio
from typing import Any

import pytest

from app.providers.base import ProviderError, Quote, SymbolNotFound, Trade
from app.services.price_hub import FeedState, PriceHub


class RecordingSink:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def ticks(self, symbol: str | None = None) -> list[dict]:
        out = [m for m in self.sent if m.get("type") == "tick"]
        return [m for m in out if m["s"] == symbol] if symbol else out


class DeadSink:
    async def send_json(self, payload: dict) -> None:
        raise ConnectionResetError("browser went away")


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.quotes: dict[str, Quote] = {}
        self.quote_calls: list[str] = []
        self.raise_on_quote: Exception | None = None
        self.trades: list[Trade] = []

    async def quote(self, symbol: str) -> Quote:
        self.quote_calls.append(symbol)
        if self.raise_on_quote is not None:
            raise self.raise_on_quote
        return self.quotes.get(
            symbol, Quote(symbol=symbol, price=100.0, prev_close=99.0, as_of=1_700_000_000)
        )

    async def candles(self, *a: Any) -> list:
        return []

    async def search(self, q: str) -> list:
        return []

    async def stream(self, symbols: set[str]):
        for trade in self.trades:
            yield trade

    async def aclose(self) -> None:
        return None


def make_hub(provider: FakeProvider | None = None, **kw: Any) -> PriceHub:
    """A hub in the state start() would leave it in, minus the background tasks.

    The loops check `_running` so they can be cancelled cleanly at shutdown;
    setting it here lets the tests drive `flush()` and `_poll_once()` directly
    without racing real timers.
    """
    hub = PriceHub(provider or FakeProvider(), **kw)
    hub._running = True
    return hub


# --- coalescing: the point of the phase -------------------------------------


async def test_hundreds_of_prints_collapse_to_one_message_per_flush() -> None:
    hub = make_hub()
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])

    for i in range(500):
        hub.record("AAPL", 100.0 + i * 0.01, 1_700_000_000_000 + i)

    await hub.flush()

    assert len(sink.ticks()) == 1, "500 prints must become exactly one message"
    assert sink.ticks()[0]["p"] == pytest.approx(104.99), "and it must be the LAST price"


async def test_each_symbol_gets_its_own_coalesced_message() -> None:
    hub = make_hub()
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL", "NVDA"])

    for i in range(100):
        hub.record("AAPL", 200.0 + i, 1)
        hub.record("NVDA", 300.0 + i, 1)

    await hub.flush()

    assert len(sink.ticks()) == 2
    assert sink.ticks("AAPL")[0]["p"] == 299.0
    assert sink.ticks("NVDA")[0]["p"] == 399.0


async def test_a_quiet_symbol_sends_nothing() -> None:
    hub = make_hub()
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])

    await hub.flush()
    assert sink.ticks() == []

    hub.record("AAPL", 1.0, 1)
    await hub.flush()
    await hub.flush()  # nothing new since the last flush
    assert len(sink.ticks()) == 1


async def test_the_flush_loop_actually_runs_on_its_interval() -> None:
    hub = make_hub(coalesce_interval=0.02)
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])
    hub._running = True
    task = asyncio.create_task(hub._flush_loop())
    try:
        for i in range(50):
            hub.record("AAPL", float(i), i)
        await asyncio.sleep(0.07)
        assert 1 <= len(sink.ticks()) <= 4, "bursts must not become 50 messages"
    finally:
        hub._running = False
        task.cancel()


# --- fan-out ----------------------------------------------------------------


async def test_a_subscriber_only_receives_symbols_it_asked_for() -> None:
    hub = make_hub()
    a, b = RecordingSink(), RecordingSink()
    sub_a, sub_b = hub.add_sink(a), hub.add_sink(b)
    hub.subscribe(sub_a, ["AAPL"])
    hub.subscribe(sub_b, ["NVDA"])

    hub.record("AAPL", 1.0, 1)
    hub.record("NVDA", 2.0, 2)
    await hub.flush()

    assert [m["s"] for m in a.ticks()] == ["AAPL"]
    assert [m["s"] for m in b.ticks()] == ["NVDA"]


async def test_one_dead_socket_does_not_stop_delivery_to_healthy_ones() -> None:
    hub = make_hub()
    dead, alive = DeadSink(), RecordingSink()
    hub.subscribe(hub.add_sink(dead), ["AAPL"])
    sub_alive = hub.add_sink(alive)
    hub.subscribe(sub_alive, ["AAPL"])

    hub.record("AAPL", 5.0, 1)
    await hub.flush()

    assert len(alive.ticks()) == 1
    assert sub_alive in hub._subscribers
    assert len(hub._subscribers) == 1, "the dead subscriber is dropped"


async def test_tracked_symbols_is_the_union_across_subscribers() -> None:
    hub = make_hub()
    s1, s2 = hub.add_sink(RecordingSink()), hub.add_sink(RecordingSink())
    hub.subscribe(s1, ["AAPL", "MSFT"])
    hub.subscribe(s2, ["MSFT", "NVDA"])
    assert hub.tracked_symbols() == {"AAPL", "MSFT", "NVDA"}

    hub.unsubscribe(s1, ["MSFT"])
    assert hub.tracked_symbols() == {"AAPL", "MSFT", "NVDA"}, "s2 still wants MSFT"

    hub.remove_sink(s2)
    assert hub.tracked_symbols() == {"AAPL"}


async def test_symbols_are_normalised() -> None:
    hub = make_hub()
    sub = hub.add_sink(RecordingSink())
    hub.subscribe(sub, [" aapl ", "nvda"])
    assert hub.tracked_symbols() == {"AAPL", "NVDA"}


# --- day change -------------------------------------------------------------


async def test_change_pct_is_computed_from_prev_close() -> None:
    hub = make_hub()
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])
    hub._prev_close["AAPL"] = 100.0

    hub.record("AAPL", 101.5, 1)
    await hub.flush()
    assert sink.ticks()[0]["dp"] == pytest.approx(1.5)


async def test_change_pct_is_null_when_prev_close_is_unknown() -> None:
    hub = make_hub()
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])

    hub.record("AAPL", 101.5, 1)
    await hub.flush()
    assert sink.ticks()[0]["dp"] is None


# --- fallback ladder (§4) ---------------------------------------------------


async def test_rung_2_polling_updates_the_cache_from_rest() -> None:
    provider = FakeProvider()
    hub = make_hub(provider, max_calls_per_min=0)
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])

    await hub._poll_once(["AAPL"])
    await hub.flush()

    assert provider.quote_calls == ["AAPL"]
    assert sink.ticks()[0]["p"] == 100.0
    # flush rounds to 4dp on the wire, deliberately.
    assert sink.ticks()[0]["dp"] == pytest.approx(1.0101, abs=1e-4)


async def test_rung_3_serves_the_last_price_marked_stale_when_the_provider_fails() -> None:
    provider = FakeProvider()
    hub = make_hub(provider, max_calls_per_min=0, clock=lambda: 5000.0)
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])

    hub.record("AAPL", 42.0, 1)  # a good price arrives first
    await hub.flush()

    provider.raise_on_quote = ProviderError("upstream is down")
    await hub._poll_once(["AAPL"])
    await hub.flush()

    last = sink.ticks("AAPL")[-1]
    assert last["p"] == 42.0, "the last known price is still served"
    assert last["stale_since"] == 5000.0, "and is labelled stale"
    assert hub.state is FeedState.DOWN


async def test_an_unknown_symbol_is_skipped_not_fatal() -> None:
    provider = FakeProvider()
    provider.raise_on_quote = SymbolNotFound("nope")
    hub = make_hub(provider, max_calls_per_min=0)
    hub.subscribe(hub.add_sink(RecordingSink()), ["NOTREAL"])

    await hub._poll_once(["NOTREAL"])
    assert hub.state is not FeedState.DOWN


async def test_upstream_call_budget_spaces_rest_requests() -> None:
    """Free tier is 60/min; polling 20 symbols every 15s would exceed it."""
    now = {"t": 0.0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    hub = make_hub(max_calls_per_min=60, clock=lambda: now["t"])
    import app.services.price_hub as mod

    original, asyncio.sleep = asyncio.sleep, fake_sleep
    try:
        await hub._respect_call_budget()
        await hub._respect_call_budget()
        await hub._respect_call_budget()
    finally:
        asyncio.sleep = original
        assert mod is not None

    assert sleeps, "consecutive calls must be spaced"
    assert all(s == pytest.approx(1.0) for s in sleeps), "60/min => 1s apart"


# --- status -----------------------------------------------------------------


async def test_status_payload_matches_the_documented_shape() -> None:
    hub = make_hub()
    payload = hub.status_payload()
    assert payload["type"] == "status"
    assert payload["provider"] == "fake"
    assert payload["state"] in {"not_started", "live", "polling", "down"}
    assert isinstance(payload["market_open"], bool)


async def test_snapshot_returns_cached_prices_for_a_new_socket() -> None:
    hub = make_hub()
    hub.record("AAPL", 1.0, 1)
    hub.record("NVDA", 2.0, 2)
    assert [t.symbol for t in hub.snapshot(["AAPL", "NVDA", "TSLA"])] == ["AAPL", "NVDA"]


# --- end to end: upstream burst -> coalesced fan-out ------------------------


async def test_a_thousand_upstream_prints_do_not_become_a_thousand_messages() -> None:
    """The full path: provider stream -> record -> flush loop -> subscriber.

    This is the guarantee the phase exists for. A liquid symbol prints hundreds
    of times a second; the browser must receive a bounded trickle.
    """
    provider = FakeProvider()
    provider.trades = [
        Trade(symbol="AAPL", price=200.0 + i * 0.01, timestamp_ms=1_700_000_000_000 + i)
        for i in range(1000)
    ]

    hub = make_hub(provider, coalesce_interval=0.02)
    sink = RecordingSink()
    sub = hub.add_sink(sink)
    hub.subscribe(sub, ["AAPL"])
    hub._prev_close["AAPL"] = 200.0

    flusher = asyncio.create_task(hub._flush_loop())
    try:
        await hub._consume_upstream({"AAPL"}, hub._watch_changes())
        await asyncio.sleep(0.05)  # let a final flush land
    finally:
        hub._running = False
        flusher.cancel()

    received = len(sink.ticks("AAPL"))
    assert received < 50, f"1000 prints produced {received} messages — coalescing is not working"
    assert sink.ticks("AAPL")[-1]["p"] == pytest.approx(209.99), "last message is the latest price"


# --- manual refresh mode ----------------------------------------------------
# "Live when the market is open, manual when it's closed." Background polling is
# a fallback for a broken socket during the session, not an always-on fetcher.


async def test_no_background_polling_while_the_market_is_closed(monkeypatch) -> None:
    import app.services.price_hub as mod

    monkeypatch.setattr(mod, "is_market_open", lambda *a: False)
    provider = FakeProvider()
    hub = make_hub(provider, max_calls_per_min=0, poll_interval_closed=0.01)
    hub.subscribe(hub.add_sink(RecordingSink()), ["AAPL"])

    task = asyncio.create_task(hub._poll_loop())
    await asyncio.sleep(0.06)  # several poll cycles would have elapsed
    hub._running = False
    task.cancel()

    assert provider.quote_calls == [], "closed market must not fetch on a timer"
    assert hub.state is FeedState.IDLE


async def test_polling_still_covers_a_broken_socket_during_the_session(monkeypatch) -> None:
    import app.services.price_hub as mod

    monkeypatch.setattr(mod, "is_market_open", lambda *a: True)
    provider = FakeProvider()
    hub = make_hub(provider, max_calls_per_min=0, poll_interval_open=0.01)
    hub.subscribe(hub.add_sink(RecordingSink()), ["AAPL"])
    hub.upstream_connected = False

    task = asyncio.create_task(hub._poll_loop())
    await asyncio.sleep(0.05)
    hub._running = False
    task.cancel()

    assert provider.quote_calls, "rung 2 must still cover a dead socket mid-session"


async def test_refresh_fetches_on_demand_even_when_closed(monkeypatch) -> None:
    import app.services.price_hub as mod

    monkeypatch.setattr(mod, "is_market_open", lambda *a: False)
    provider = FakeProvider()
    hub = make_hub(provider, max_calls_per_min=0, clock=lambda: 4242.0)
    sink = RecordingSink()
    hub.subscribe(hub.add_sink(sink), ["AAPL"])

    refreshed = await hub.refresh()
    await hub.flush()

    assert refreshed == ["AAPL"]
    assert provider.quote_calls == ["AAPL"]
    assert sink.ticks("AAPL")[0]["p"] == 100.0
    assert hub.last_refresh_at == 4242.0


async def test_concurrent_refreshes_collapse_to_one(monkeypatch) -> None:
    """Button spam must not multiply upstream calls."""
    import app.services.price_hub as mod

    monkeypatch.setattr(mod, "is_market_open", lambda *a: False)
    provider = FakeProvider()

    # Real upstream calls suspend on I/O. Without a yield here the first refresh
    # runs start to finish before the second even begins, and the concurrency
    # this test exists to check never happens.
    plain_quote = provider.quote

    async def slow_quote(symbol: str) -> Any:
        await asyncio.sleep(0.01)
        return await plain_quote(symbol)

    provider.quote = slow_quote  # type: ignore[method-assign]

    hub = make_hub(provider, max_calls_per_min=0)
    hub.subscribe(hub.add_sink(RecordingSink()), ["AAPL", "NVDA"])

    await asyncio.gather(*(hub.refresh() for _ in range(5)))

    assert provider.quote_calls == ["AAPL", "NVDA"], "five clicks, one fetch"


async def test_refresh_with_no_symbols_is_a_no_op() -> None:
    provider = FakeProvider()
    hub = make_hub(provider, max_calls_per_min=0)
    assert await hub.refresh() == []
    assert provider.quote_calls == []
