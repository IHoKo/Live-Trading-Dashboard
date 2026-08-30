"""Response caching in front of a provider — plan.md §10, Phase 1.

Quotes cache for 5s, candles for 60s. On the free tier's 60 calls/minute this is
not a nicety: a watchlist of 20 symbols polled by two open tabs would blow the
budget in seconds without it.

Wraps any MarketDataProvider and satisfies the same interface, so callers can't
tell the difference and the §4 one-file provider swap still holds.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.providers.base import (
    Candle,
    MarketDataProvider,
    Quote,
    Resolution,
    SymbolMatch,
    Trade,
)


class TTLCache:
    """Single-process TTL cache with per-key locking.

    The lock matters as much as the TTL: without it, N concurrent misses on the
    same key all hit upstream, which is exactly the burst the cache exists to
    prevent. Single-process is correct here because the app is pinned to one
    machine (CLAUDE.md, plan.md §2).
    """

    def __init__(self, ttl: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        # Entries are evicted lazily, when an expired key is read again. That
        # is only safe because every key here is *stable* — symbol for quotes,
        # (symbol, resolution, window length) for candles — so a key is always
        # revisited and cleaned. Put anything clock-derived in a cache key and
        # this dict grows for the life of the process; see CachingProvider.candles.
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}

    def _live(self, key: Any) -> tuple[bool, Any]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._entries.pop(key, None)
            return False, None
        return True, value

    async def get_or_set(self, key: Any, factory: Callable[[], Awaitable[Any]]) -> Any:
        hit, value = self._live(key)
        if hit:
            return value

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check: another waiter may have filled it while we queued.
            hit, value = self._live(key)
            if hit:
                return value
            value = await factory()
            self._entries[key] = (self._clock() + self._ttl, value)
            return value

    def invalidate(self) -> None:
        self._entries.clear()


class CachingProvider(MarketDataProvider):
    """Caches quote and candle reads. Search is not cached — §10 specifies TTLs
    for quotes and candles only, and symbol lookup is a low-frequency call made
    while a human types.
    """

    def __init__(
        self,
        inner: MarketDataProvider,
        *,
        quote_ttl: float = 5.0,
        candle_ttl: float = 60.0,
    ) -> None:
        self._inner = inner
        self.name = inner.name
        self._quotes = TTLCache(quote_ttl)
        self._candles = TTLCache(candle_ttl)

    async def quote(self, symbol: str) -> Quote:
        return await self._quotes.get_or_set(symbol, lambda: self._inner.quote(symbol))

    async def candles(self, symbol: str, resolution: Resolution, frm: int, to: int) -> list[Candle]:
        # Keyed on the window's LENGTH, never its absolute position. An
        # absolute (frm, to) moves with the clock, so every request minted a
        # key nothing would ever read again — entries that the TTL expired but
        # nothing ever evicted, growing for as long as the process ran.
        #
        # Length still separates the ranges that share a resolution: 6M and 1Y
        # are both "D" bars, so a (symbol, resolution) key would collide and
        # serve six months of data to a one-year chart.
        key = (symbol, resolution, to - frm)
        return await self._candles.get_or_set(
            key, lambda: self._inner.candles(symbol, resolution, frm, to)
        )

    async def search(self, query: str) -> list[SymbolMatch]:
        return await self._inner.search(query)

    def stream(self, symbols: set[str]) -> AsyncIterator[Trade]:
        return self._inner.stream(symbols)

    async def aclose(self) -> None:
        aclose = getattr(self._inner, "aclose", None)
        if aclose is not None:
            await aclose()
