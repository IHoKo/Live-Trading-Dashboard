"""Caching wrapper and the three Phase 1 endpoints.

The TTLs are a checklist item in their own right (§10: quotes 5s, candles 60s)
because the free tier allows 60 calls/minute and a dashboard polling N symbols
from two tabs blows that budget without them.
"""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.deps import get_provider
from app.main import app
from app.providers.base import (
    AccessDenied,
    Candle,
    Quote,
    RateLimited,
    SymbolMatch,
    SymbolNotFound,
)
from app.providers.cache import CachingProvider, TTLCache
from app.routers.quotes import candle_window, normalize_symbol, parse_symbols


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    """Counts calls so cache hits are observable."""

    name = "fake"

    def __init__(self) -> None:
        self.quote_calls: list[str] = []
        self.candle_calls: list[tuple[Any, ...]] = []
        self.search_calls: list[str] = []
        self.raise_on_quote: Exception | None = None

    async def quote(self, symbol: str) -> Quote:
        self.quote_calls.append(symbol)
        if self.raise_on_quote is not None:
            raise self.raise_on_quote
        return Quote(
            symbol=symbol,
            price=100.0,
            change=1.0,
            change_pct=1.01,
            prev_close=99.0,
            as_of=1_754_800_000,
        )

    async def candles(self, symbol: str, resolution: str, frm: int, to: int) -> list[Candle]:
        self.candle_calls.append((symbol, resolution, frm, to))
        return [Candle(time=frm, open=1, high=2, low=0.5, close=1.5, volume=10)]

    async def search(self, query: str) -> list[SymbolMatch]:
        self.search_calls.append(query)
        return [
            SymbolMatch(
                symbol=f"S{i}", display_symbol=f"S{i}", description="x", type="Common Stock"
            )
            for i in range(40)
        ]

    def stream(self, symbols: set[str]) -> Any:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


# --- TTLCache ---------------------------------------------------------------


async def test_cache_returns_the_stored_value_until_the_ttl_expires() -> None:
    clock = FakeClock()
    cache = TTLCache(ttl=5.0, clock=clock)
    calls = {"n": 0}

    async def factory() -> str:
        calls["n"] += 1
        return f"v{calls['n']}"

    assert await cache.get_or_set("k", factory) == "v1"
    clock.advance(4.9)
    assert await cache.get_or_set("k", factory) == "v1"
    assert calls["n"] == 1

    clock.advance(0.2)  # now past the 5s TTL
    assert await cache.get_or_set("k", factory) == "v2"
    assert calls["n"] == 2


async def test_concurrent_misses_on_one_key_make_a_single_upstream_call() -> None:
    """Without per-key locking, a burst of misses becomes a burst upstream —
    the exact thing the cache is there to prevent."""
    import asyncio

    cache = TTLCache(ttl=60.0)
    calls = {"n": 0}

    async def factory() -> str:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return "value"

    results = await asyncio.gather(*(cache.get_or_set("k", factory) for _ in range(10)))

    assert results == ["value"] * 10
    assert calls["n"] == 1


async def test_quote_ttl_is_5s_and_candle_ttl_is_60s() -> None:
    inner = FakeProvider()
    cached = CachingProvider(inner)
    assert cached._quotes._ttl == 5.0
    assert cached._candles._ttl == 60.0


async def test_caching_provider_collapses_repeat_reads() -> None:
    inner = FakeProvider()
    cached = CachingProvider(inner, quote_ttl=60.0, candle_ttl=60.0)

    await cached.quote("AAPL")
    await cached.quote("AAPL")
    await cached.quote("MSFT")
    assert inner.quote_calls == ["AAPL", "MSFT"]

    await cached.candles("AAPL", "D", 0, 100)
    await cached.candles("AAPL", "D", 0, 100)
    await cached.candles("AAPL", "D", 0, 200)  # different LENGTH, different key
    assert len(inner.candle_calls) == 2


async def test_search_is_not_cached() -> None:
    """§10 specifies TTLs for quotes and candles only, and search runs while a
    human types — stale suggestions would be worse than an extra call."""
    inner = FakeProvider()
    cached = CachingProvider(inner)
    await cached.search("app")
    await cached.search("app")
    assert inner.search_calls == ["app", "app"]


# --- window bucketing -------------------------------------------------------


def test_candle_window_snaps_to_a_60s_boundary() -> None:
    """Deterministic window bounds within a minute, so chart data stays stable
    while a user clicks between ranges."""
    res_a, frm_a, to_a = candle_window("1D", now=1_754_800_031)
    res_b, frm_b, to_b = candle_window("1D", now=1_754_800_059)

    assert (res_a, frm_a, to_a) == (res_b, frm_b, to_b)
    assert to_a % 60 == 0

    _, _, to_next = candle_window("1D", now=1_754_800_081)
    assert to_next == to_a + 60


@pytest.mark.parametrize(
    ("range_", "resolution"),
    [("1D", "5"), ("5D", "15"), ("1M", "60"), ("6M", "D"), ("1Y", "D"), ("5Y", "W")],
)
def test_every_documented_range_maps_to_a_resolution(range_: str, resolution: str) -> None:
    res, frm, to = candle_window(range_, now=1_754_800_000)  # type: ignore[arg-type]
    assert res == resolution
    assert frm < to


# --- input handling ---------------------------------------------------------


def test_symbols_are_normalized_and_deduped() -> None:
    assert parse_symbols(" aapl , MSFT ,aapl") == ["AAPL", "MSFT"]


def test_normalize_symbol_uppercases_and_strips() -> None:
    assert normalize_symbol("  nvda ") == "NVDA"


def test_empty_and_oversized_symbol_lists_are_rejected() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as empty:
        parse_symbols("  , ,")
    assert empty.value.status_code == 422

    with pytest.raises(HTTPException) as too_many:
        parse_symbols(",".join(f"S{i}" for i in range(51)))
    assert too_many.value.status_code == 422


# --- endpoints --------------------------------------------------------------


@pytest.fixture
def provider() -> Any:
    fake = FakeProvider()
    app.dependency_overrides[get_provider] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def client(provider: Any) -> TestClient:
    return TestClient(app)


def test_quotes_endpoint_returns_price_change_and_staleness(client: TestClient) -> None:
    res = client.get("/api/quotes", params={"symbols": "aapl,msft"})
    assert res.status_code == 200

    body = res.json()
    assert [q["symbol"] for q in body["quotes"]] == ["AAPL", "MSFT"]
    first = body["quotes"][0]
    assert first["price"] == 100.0
    assert first["change_pct"] == 1.01
    assert first["as_of"] == 1_754_800_000
    assert first["age_seconds"] >= 0
    assert body["unavailable"] == []


def test_one_bad_symbol_does_not_blank_the_whole_watchlist(
    client: TestClient, provider: Any
) -> None:
    provider.raise_on_quote = SymbolNotFound("nope")
    res = client.get("/api/quotes", params={"symbols": "AAPL,NOTREAL"})

    assert res.status_code == 200
    assert res.json()["quotes"] == []
    assert res.json()["unavailable"] == ["AAPL", "NOTREAL"]


def test_rate_limit_surfaces_as_429_with_retry_after(client: TestClient, provider: Any) -> None:
    provider.raise_on_quote = RateLimited("slow down", retry_after=12)
    res = client.get("/api/quotes", params={"symbols": "AAPL"})

    assert res.status_code == 429
    assert res.headers["Retry-After"] == "12"


def test_access_denied_surfaces_as_502_with_an_actionable_message(
    client: TestClient, provider: Any
) -> None:
    provider.raise_on_quote = AccessDenied(
        "finnhub denied /quote (HTTP 403). ...not on the current plan."
    )
    res = client.get("/api/quotes", params={"symbols": "AAPL"})

    assert res.status_code == 502
    assert "plan" in res.json()["detail"]


def test_candles_endpoint_defaults_to_1d(client: TestClient, provider: Any) -> None:
    res = client.get("/api/candles/aapl")
    assert res.status_code == 200

    body = res.json()
    assert body["symbol"] == "AAPL"
    assert body["range"] == "1D"
    assert body["resolution"] == "5"
    assert len(body["candles"]) == 1
    assert set(body["candles"][0]) == {"time", "open", "high", "low", "close", "volume"}


def test_candles_rejects_an_undocumented_range(client: TestClient) -> None:
    assert client.get("/api/candles/AAPL", params={"range": "3Y"}).status_code == 422


def test_search_endpoint_caps_results(client: TestClient) -> None:
    res = client.get("/api/search", params={"q": "apple"})
    assert res.status_code == 200
    assert len(res.json()["results"]) == 25


def test_search_requires_a_query(client: TestClient) -> None:
    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_market_endpoints_503_when_no_key_is_configured() -> None:
    """A missing FINNHUB_API_KEY degrades one feature; it must not stop the app
    booting or take /api/health down with it (plan.md §9.3)."""
    app.dependency_overrides.clear()
    with TestClient(app) as unconfigured:
        assert unconfigured.get("/api/health").status_code == 200
        res = unconfigured.get("/api/quotes", params={"symbols": "AAPL"})
        assert res.status_code == 503
        assert "FINNHUB_API_KEY" in res.json()["detail"]


# --- cache key stability: the fix for the unbounded-growth bug ---------------


async def test_a_window_that_slides_with_the_clock_still_hits_the_cache() -> None:
    """The bug: keying on absolute (frm, to) minted a fresh key every minute,
    and nothing ever read those keys again, so nothing ever evicted them."""
    inner = FakeProvider()
    cached = CachingProvider(inner, candle_ttl=60.0)

    # Same 1-day window, sampled a minute apart.
    await cached.candles("AAPL", "5", 1_000_000, 1_086_400)
    await cached.candles("AAPL", "5", 1_000_060, 1_086_460)
    await cached.candles("AAPL", "5", 1_000_120, 1_086_520)

    assert len(inner.candle_calls) == 1, "a sliding window of equal length is one key"


async def test_ranges_that_share_a_resolution_do_not_collide() -> None:
    """6M and 1Y are both daily bars — a (symbol, resolution) key would serve
    six months of data to a one-year chart."""
    inner = FakeProvider()
    cached = CachingProvider(inner, candle_ttl=60.0)

    _, frm_6m, to_6m = candle_window("6M", now=1_754_800_000)
    _, frm_1y, to_1y = candle_window("1Y", now=1_754_800_000)

    await cached.candles("AAPL", "D", frm_6m, to_6m)
    await cached.candles("AAPL", "D", frm_1y, to_1y)

    assert len(inner.candle_calls) == 2, "different lookbacks must be different keys"


async def test_candle_cache_size_is_bounded_by_symbols_times_ranges() -> None:
    """Simulate a day of chart loads; the cache must not grow with uptime."""
    inner = FakeProvider()
    cached = CachingProvider(inner, candle_ttl=1e9)  # never expire, so growth is visible
    symbols = ["AAPL", "MSFT", "NVDA"]
    ranges = ["1D", "5D", "1M", "6M", "1Y", "5Y"]

    for minute in range(24 * 60):
        now = 1_754_800_000 + minute * 60
        for symbol in symbols:
            for rng in ranges:
                resolution, frm, to = candle_window(rng, now=now)
                await cached.candles(symbol, resolution, frm, to)

    entries = len(cached._candles._entries)
    locks = len(cached._candles._locks)
    assert entries <= len(symbols) * len(ranges), f"{entries} entries after 24h of requests"
    assert locks <= len(symbols) * len(ranges), f"{locks} locks after 24h of requests"


# --- TTLCache is bounded by construction, not by convention -----------------
# The candle leak happened because the cache trusted its callers to pick stable
# keys. These assert it survives a caller that does not.


async def test_expired_entries_are_swept_on_write_not_only_on_read() -> None:
    """A key nobody revisits is never cleaned by read-time eviction alone —
    that is exactly the class of key that leaked."""
    clock = FakeClock()
    cache = TTLCache(ttl=5.0, clock=clock)

    for i in range(10):
        await cache.get_or_set(f"never-read-again-{i}", _value(i))
    assert len(cache) == 10

    clock.advance(6.0)  # everything above is now expired, and nothing rereads it
    await cache.get_or_set("a-different-key", _value("x"))

    assert len(cache) == 1, "the write should have swept the expired entries"


async def test_a_pathological_key_space_stays_bounded() -> None:
    """Simulates the original bug: a fresh key every call, forever."""
    cache = TTLCache(ttl=1e9, max_entries=64)

    for i in range(10_000):
        await cache.get_or_set(("AAPL", "5", i), _value(i))

    assert len(cache) == 64, "the hard cap must hold regardless of key design"
    assert len(cache._locks) <= 64, "locks must be pruned alongside entries"


async def test_eviction_drops_the_oldest_write_first() -> None:
    cache = TTLCache(ttl=1e9, max_entries=3)
    for key in ("a", "b", "c"):
        await cache.get_or_set(key, _value(key))

    await cache.get_or_set("d", _value("d"))

    assert set(cache._entries) == {"b", "c", "d"}, "'a' was the oldest write"


async def test_refreshing_a_key_moves_it_to_the_back_of_the_queue() -> None:
    clock = FakeClock()
    cache = TTLCache(ttl=1.0, max_entries=3, clock=clock)
    for key in ("a", "b", "c"):
        await cache.get_or_set(key, _value(key))

    clock.advance(2.0)  # everything expires
    await cache.get_or_set("a", _value("a2"))  # 'a' rewritten, others swept
    for key in ("b", "c", "d"):
        await cache.get_or_set(key, _value(key))

    assert "a" not in cache._entries, "'a' is now the oldest of the four"
    assert set(cache._entries) == {"b", "c", "d"}


async def test_a_held_lock_is_never_evicted() -> None:
    """Dropping a lock somebody is waiting on would let a second caller through
    and defeat the single-flight guard."""
    cache = TTLCache(ttl=1e9, max_entries=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> str:
        started.set()
        await release.wait()
        return "slow"

    task = asyncio.create_task(cache.get_or_set("held", slow))
    await started.wait()

    for i in range(50):  # push far past the cap while "held" is in flight
        await cache.get_or_set(f"filler-{i}", _value(i))

    assert "held" in cache._locks, "the in-flight lock survived eviction"
    release.set()
    assert await task == "slow"


def _value(v: Any) -> Any:
    async def factory() -> Any:
        return v

    return factory
