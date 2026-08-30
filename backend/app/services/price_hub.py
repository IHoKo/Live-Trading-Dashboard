"""PriceHub — one upstream feed, fanned out to N browser sockets.

plan.md §2, §4, §6, §10 Phase 2.

Two things here are load-bearing:

**Coalescing (§6).** A liquid symbol prints hundreds of trades a second. Every
print updates the in-memory cache, but subscribers are flushed at most every
250ms with the *latest* price per symbol. Without this the browser receives
more messages than it can render and the tab locks up. This is the whole point
of the phase.

**The fallback ladder (§4).** All three rungs, because markets are closed most
of the week:
  1. upstream WS trade prints -> push immediately (coalesced)
  2. WS down or market closed -> poll REST quotes for subscribed symbols only
  3. provider erroring -> keep serving the last known price with `stale_since`

The hub is single-process by design: the app is pinned to one machine, so one
upstream connection and one cache is the whole system (CLAUDE.md, §2).
"""

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from app.providers.base import MarketDataProvider, ProviderError, SymbolNotFound
from app.services.market_hours import is_market_open

logger = logging.getLogger("ticker.price_hub")

COALESCE_INTERVAL = 0.25  # §6: flush at most every 250ms
POLL_INTERVAL_OPEN = 15.0  # §4 rung 2
POLL_INTERVAL_CLOSED = 60.0  # prices don't move; don't burn the quota
RECONNECT_BASE = 1.0
RECONNECT_CAP = 30.0

# Finnhub's free tier allows 60 calls/min. Polling N symbols every 15s exceeds
# that at N > 15, so upstream REST calls are spaced to stay inside a budget and
# a cycle simply takes longer than its nominal interval when the watchlist is
# large. Honest degradation beats a burst of 429s.
MAX_UPSTREAM_CALLS_PER_MIN = 50


class FeedState(str, Enum):
    NOT_STARTED = "not_started"
    LIVE = "live"  # rung 1
    POLLING = "polling"  # rung 2
    DOWN = "down"  # rung 3


@dataclass
class Tick:
    symbol: str
    price: float
    timestamp_ms: int
    change_pct: float | None = None
    # Set when the value is known to be old: provider failing, or a poll that
    # could not refresh it. Rendered greyed, per §4.
    stale_since: float | None = None


class TickSink(Protocol):
    """A browser connection. Kept minimal so the hub is testable without FastAPI."""

    async def send_json(self, payload: dict) -> None: ...


@dataclass
class _Subscriber:
    sink: TickSink
    symbols: set[str] = field(default_factory=set)


class PriceHub:
    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        coalesce_interval: float = COALESCE_INTERVAL,
        poll_interval_open: float = POLL_INTERVAL_OPEN,
        poll_interval_closed: float = POLL_INTERVAL_CLOSED,
        max_calls_per_min: int = MAX_UPSTREAM_CALLS_PER_MIN,
        clock=time.time,
    ) -> None:
        self._provider = provider
        self._coalesce_interval = coalesce_interval
        self._poll_open = poll_interval_open
        self._poll_closed = poll_interval_closed
        self._min_call_spacing = 60.0 / max_calls_per_min if max_calls_per_min else 0.0
        self._clock = clock

        self.state = FeedState.NOT_STARTED
        self.upstream_connected = False

        self._last: dict[str, Tick] = {}
        self._prev_close: dict[str, float] = {}
        self._dirty: set[str] = set()
        self._subscribers: dict[int, _Subscriber] = {}
        self._last_call_at = 0.0

        self._tasks: list[asyncio.Task] = []
        # One event per waiting loop. A single shared Event would be wrong:
        # each waiter clears it, so whichever wakes first can swallow the
        # notification before the others observe it.
        self._change_events: list[asyncio.Event] = []
        self._running = False

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.state = FeedState.POLLING
        self._tasks = [
            asyncio.create_task(self._flush_loop(), name="hub-flush"),
            asyncio.create_task(self._upstream_loop(), name="hub-upstream"),
            asyncio.create_task(self._poll_loop(), name="hub-poll"),
        ]
        logger.info("price hub started")

    async def stop(self) -> None:
        """Cancel loops and wait for them. fly.toml allows 30s (§9.2)."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it
            except Exception:
                logger.exception("task %s failed during shutdown", task.get_name())
        self._tasks.clear()
        self._change_events.clear()
        self.state = FeedState.NOT_STARTED
        logger.info("price hub stopped")

    def _watch_changes(self) -> asyncio.Event:
        event = asyncio.Event()
        self._change_events.append(event)
        return event

    def _notify_change(self) -> None:
        for event in self._change_events:
            event.set()

    # --- subscriber registry ------------------------------------------------

    def add_sink(self, sink: TickSink) -> int:
        sub_id = id(sink)
        self._subscribers[sub_id] = _Subscriber(sink=sink)
        return sub_id

    def remove_sink(self, sub_id: int) -> None:
        if self._subscribers.pop(sub_id, None) is not None:
            self._notify_change()

    def subscribe(self, sub_id: int, symbols: Iterable[str]) -> set[str]:
        sub = self._subscribers.get(sub_id)
        if sub is None:
            return set()
        before = self.tracked_symbols()
        sub.symbols |= {s.strip().upper() for s in symbols if s.strip()}
        if self.tracked_symbols() != before:
            self._notify_change()
        return set(sub.symbols)

    def unsubscribe(self, sub_id: int, symbols: Iterable[str]) -> set[str]:
        sub = self._subscribers.get(sub_id)
        if sub is None:
            return set()
        before = self.tracked_symbols()
        sub.symbols -= {s.strip().upper() for s in symbols if s.strip()}
        if self.tracked_symbols() != before:
            self._notify_change()
        return set(sub.symbols)

    def tracked_symbols(self) -> set[str]:
        """Union of every subscriber's symbols — what the upstream needs."""
        out: set[str] = set()
        for sub in self._subscribers.values():
            out |= sub.symbols
        return out

    def snapshot(self, symbols: Iterable[str]) -> list[Tick]:
        """Cached last prices, for a socket that has just connected."""
        return [self._last[s] for s in symbols if s in self._last]

    # --- ingest -------------------------------------------------------------

    def record(self, symbol: str, price: float, timestamp_ms: int) -> None:
        """Update the cache and mark dirty. Deliberately does NOT send: the
        flush loop decides when, which is what makes coalescing work."""
        prev = self._prev_close.get(symbol)
        self._last[symbol] = Tick(
            symbol=symbol,
            price=price,
            timestamp_ms=timestamp_ms,
            change_pct=((price - prev) / prev * 100.0) if prev else None,
            stale_since=None,
        )
        self._dirty.add(symbol)

    # --- rung 1: upstream socket -------------------------------------------

    async def _upstream_loop(self) -> None:
        backoff = RECONNECT_BASE
        changed = self._watch_changes()
        while self._running:
            symbols = self.tracked_symbols()
            if not symbols or not is_market_open():
                self.upstream_connected = False
                self._recompute_state()
                await self._wait_for_change(changed, 5.0)
                continue

            try:
                await self._consume_upstream(symbols, changed)
                backoff = RECONNECT_BASE
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any upstream failure means reconnect
                self.upstream_connected = False
                self._recompute_state()
                logger.warning("upstream feed dropped: %s; retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_CAP)

    async def _consume_upstream(self, symbols: set[str], changed: asyncio.Event) -> None:
        """Read trades until the symbol set changes or the socket dies."""
        await self._ensure_prev_closes(symbols)
        stream = self._provider.stream(set(symbols))
        try:
            self.upstream_connected = True
            self._recompute_state()
            async for trade in stream:
                self.record(trade.symbol, trade.price, trade.timestamp_ms)
                if changed.is_set():
                    changed.clear()
                    return  # restart with the new set (§4 interface is fixed-set)
        finally:
            self.upstream_connected = False
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    # --- rung 2: REST polling ----------------------------------------------

    async def _poll_loop(self) -> None:
        changed = self._watch_changes()
        while self._running:
            symbols = sorted(self.tracked_symbols())
            open_now = is_market_open()

            if symbols and not (self.upstream_connected and open_now):
                await self._poll_once(symbols)

            await self._wait_for_change(changed, self._poll_open if open_now else self._poll_closed)

    async def _poll_once(self, symbols: list[str]) -> None:
        errors = 0
        for symbol in symbols:
            if not self._running:
                return
            await self._respect_call_budget()
            try:
                quote = await self._provider.quote(symbol)
            except SymbolNotFound:
                continue
            except ProviderError as exc:
                errors += 1
                logger.warning("poll failed for %s: %s", symbol, exc)
                self._mark_stale(symbol)
                continue
            if quote.prev_close:
                self._prev_close[symbol] = quote.prev_close
            self.record(symbol, quote.price, quote.as_of * 1000)

        # Rung 3: everything failed, so serve what we have and say it's stale.
        self.state = FeedState.DOWN if errors and errors == len(symbols) else self._nominal_state()

    def _mark_stale(self, symbol: str) -> None:
        tick = self._last.get(symbol)
        if tick is not None and tick.stale_since is None:
            tick.stale_since = self._clock()
            self._dirty.add(symbol)

    async def _respect_call_budget(self) -> None:
        if self._min_call_spacing <= 0:
            return
        elapsed = self._clock() - self._last_call_at
        if elapsed < self._min_call_spacing:
            await asyncio.sleep(self._min_call_spacing - elapsed)
        self._last_call_at = self._clock()

    async def _ensure_prev_closes(self, symbols: Iterable[str]) -> None:
        """Trade prints carry no day-change, so prev_close comes from REST."""
        for symbol in symbols:
            if symbol in self._prev_close:
                continue
            await self._respect_call_budget()
            try:
                quote = await self._provider.quote(symbol)
            except (ProviderError, SymbolNotFound):
                continue
            if quote.prev_close:
                self._prev_close[symbol] = quote.prev_close
            self.record(symbol, quote.price, quote.as_of * 1000)

    # --- flush: the coalescing loop ----------------------------------------

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._coalesce_interval)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("flush failed")

    async def _send(self, sub_id: int, sub: "_Subscriber", payload: dict) -> bool:
        """Deliver to one sink; drop the subscriber if the socket is gone.

        Broad by design: a single dead browser tab must never interrupt the
        fan-out to the healthy ones.
        """
        try:
            await sub.sink.send_json(payload)
        except Exception:  # noqa: BLE001 - see docstring
            self._subscribers.pop(sub_id, None)
            return False
        return True

    async def flush(self) -> None:
        """Send at most one message per symbol per interval, to each subscriber
        that asked for it. Hundreds of prints collapse to one price."""
        if not self._dirty:
            return
        dirty, self._dirty = self._dirty, set()

        for sub_id, sub in list(self._subscribers.items()):
            wanted = dirty & sub.symbols
            if not wanted:
                continue
            for symbol in sorted(wanted):
                tick = self._last.get(symbol)
                if tick is None:
                    continue
                delivered = await self._send(
                    sub_id,
                    sub,
                    {
                        "type": "tick",
                        "s": tick.symbol,
                        "p": round(tick.price, 4),
                        "t": tick.timestamp_ms,
                        "dp": round(tick.change_pct, 4) if tick.change_pct is not None else None,
                        "stale_since": tick.stale_since,
                    },
                )
                if not delivered:
                    break

    # --- state --------------------------------------------------------------

    def _nominal_state(self) -> FeedState:
        return FeedState.LIVE if self.upstream_connected else FeedState.POLLING

    def _recompute_state(self) -> None:
        if self.state is FeedState.DOWN and not self.upstream_connected:
            return  # a failing provider stays DOWN until a poll succeeds
        self.state = self._nominal_state()

    def status_payload(self) -> dict:
        return {
            "type": "status",
            "provider": self._provider.name,
            "state": self.state.value,
            "market_open": is_market_open(),
        }

    async def broadcast_status(self) -> None:
        payload = self.status_payload()
        for sub_id, sub in list(self._subscribers.items()):
            await self._send(sub_id, sub, payload)

    async def _wait_for_change(self, event: asyncio.Event, timeout: float) -> None:
        """Sleep, but wake early if this loop's subscription set changed."""
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return
        finally:
            event.clear()
