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
    SymbolNotFound,
    Trade,
)

# A cache this size is already far larger than the app can use: quotes are keyed
# by symbol, candles by (symbol, resolution, window length). It exists so that a
# future key-design mistake costs a cache miss instead of the machine.
DEFAULT_MAX_ENTRIES = 512


class TTLCache:
    """Single-process TTL cache with per-key locking and a hard size bound.

    The lock matters as much as the TTL: without it, N concurrent misses on the
    same key all hit upstream, which is exactly the burst the cache exists to
    prevent. Single-process is correct here because the app is pinned to one
    machine (CLAUDE.md, plan.md §2).

    Two independent guarantees keep this bounded, because relying on either
    alone is how the candle cache leaked:

    * **Sweep on write.** Expired entries are cleared whenever anything is
      stored, not only when their own key is read again. Reading is not enough:
      a key nobody revisits is never cleaned, and it was exactly that class of
      key — one carrying a moving timestamp — that grew without limit.
    * **Hard cap.** Past `max_entries` the oldest write is dropped. Even if a
      caller invents an unbounded key space, memory stays flat and the only
      cost is a lower hit rate.

    `_locks` is pruned in lockstep with `_entries`; on its own it had no
    eviction path at all.
    """

    def __init__(
        self,
        ttl: float,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        # Insertion-ordered: the first key is the oldest write, which is what
        # eviction drops.
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}

    # --- internals ----------------------------------------------------------

    def _drop(self, key: Any) -> None:
        self._entries.pop(key, None)
        lock = self._locks.get(key)
        # Never discard a lock somebody is holding or waiting on — a fresh lock
        # would let a second caller through and defeat the single-flight guard.
        if lock is not None and not lock.locked():
            del self._locks[key]

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, (expires_at, _) in self._entries.items() if now >= expires_at]:
            self._drop(key)

    def _enforce_cap(self) -> None:
        while len(self._entries) > self._max_entries:
            self._drop(next(iter(self._entries)))

    def _live(self, key: Any) -> tuple[bool, Any]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._drop(key)
            return False, None
        return True, value

    def _store(self, key: Any, value: Any) -> None:
        self._sweep()
        # Re-insert rather than assign, so a refreshed key moves to the back of
        # the queue and eviction stays oldest-write-first.
        self._entries.pop(key, None)
        self._entries[key] = (self._clock() + self._ttl, value)
        self._enforce_cap()

    # --- api ----------------------------------------------------------------

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
            self._store(key, value)
            return value

    def get(self, key: Any) -> tuple[bool, Any]:
        """(hit, value). Sync, because a negative lookup must not await."""
        return self._live(key)

    def set(self, key: Any, value: Any) -> None:
        self._store(key, value)

    def invalidate(self) -> None:
        self._entries.clear()
        self._locks = {k: v for k, v in self._locks.items() if v.locked()}

    def __len__(self) -> int:
        return len(self._entries)


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
        not_found_ttl: float = 300.0,
    ) -> None:
        self._inner = inner
        self.name = inner.name
        self._quotes = TTLCache(quote_ttl)
        self._candles = TTLCache(candle_ttl)
        # Negative cache. A symbol the provider does not know will not start
        # existing in the next few seconds, but without this a single typo'd
        # ticker in the watchlist costs one upstream call on every poll — from
        # a budget of 60 a minute. Held far longer than a real quote, because
        # the answer is far more stable.
        self._missing = TTLCache(not_found_ttl)

    async def quote(self, symbol: str) -> Quote:
        known_missing, _ = self._missing.get(symbol)
        if known_missing:
            raise SymbolNotFound(f"{symbol!r} is not a known symbol (cached)")

        try:
            return await self._quotes.get_or_set(symbol, lambda: self._inner.quote(symbol))
        except SymbolNotFound:
            self._missing.set(symbol, True)
            raise

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
