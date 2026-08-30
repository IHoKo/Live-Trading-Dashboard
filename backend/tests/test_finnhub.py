"""Finnhub client behaviour against a mocked transport.

No network, no key. What matters here is the stuff that only shows up under
failure: 429 backoff, the "you don't have access" response, and the fact that an
unknown symbol comes back as a zero-filled 200 rather than a 404.
"""

import asyncio
from typing import Any

import httpx
import pytest

from app.providers.base import (
    AccessDenied,
    ProviderError,
    RateLimited,
    SymbolNotFound,
)
from app.providers.finnhub import BASE_URL, FinnhubProvider

QUOTE_OK = {
    "c": 213.44,
    "d": 1.76,
    "dp": 0.83,
    "h": 214.0,
    "l": 210.1,
    "o": 211.0,
    "pc": 211.68,
    "t": 1754800000,
}
QUOTE_UNKNOWN = {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}


def make_provider(handler: Any, **kwargs: Any) -> FinnhubProvider:
    client = httpx.AsyncClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
        headers={"X-Finnhub-Token": "test-key"},
    )
    return FinnhubProvider("test-key", client=client, base_backoff=0.01, **kwargs)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff delays instead of actually waiting them out."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


# --- auth / key handling ----------------------------------------------------


async def test_key_travels_in_a_header_not_the_query_string() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Finnhub-Token")
        return httpx.Response(200, json=QUOTE_OK)

    provider = make_provider(handler)
    await provider.quote("AAPL")

    assert seen["token"] == "test-key"
    assert "test-key" not in seen["url"], "key must not end up in URLs or access logs"


def test_constructing_without_a_key_fails_loudly() -> None:
    with pytest.raises(ValueError, match="FINNHUB_API_KEY"):
        FinnhubProvider("")


# --- quotes -----------------------------------------------------------------


async def test_quote_maps_finnhubs_single_letter_fields() -> None:
    provider = make_provider(lambda _r: httpx.Response(200, json=QUOTE_OK))
    quote = await provider.quote("AAPL")

    assert quote.symbol == "AAPL"
    assert quote.price == 213.44
    assert quote.change == 1.76
    assert quote.change_pct == 0.83
    assert quote.prev_close == 211.68
    assert quote.as_of == 1754800000


async def test_unknown_symbol_is_a_zero_filled_200_not_a_404() -> None:
    provider = make_provider(lambda _r: httpx.Response(200, json=QUOTE_UNKNOWN))
    with pytest.raises(SymbolNotFound):
        await provider.quote("NOTREAL")


# --- 429 backoff ------------------------------------------------------------


async def test_retries_with_exponential_backoff_then_succeeds(no_sleep: list[float]) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=QUOTE_OK)

    provider = make_provider(handler)
    quote = await provider.quote("AAPL")

    assert quote.price == 213.44
    assert calls["n"] == 3
    assert len(no_sleep) == 2
    assert no_sleep[1] > no_sleep[0], "backoff must grow between attempts"


async def test_retry_after_header_is_honoured(no_sleep: list[float]) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=QUOTE_OK)

    provider = make_provider(handler)
    await provider.quote("AAPL")
    assert no_sleep == [7.0]


async def test_exhausted_retries_raise_rate_limited(no_sleep: list[float]) -> None:
    provider = make_provider(
        lambda _r: httpx.Response(429, headers={"Retry-After": "3"}), max_attempts=3
    )

    with pytest.raises(RateLimited) as exc:
        await provider.quote("AAPL")

    assert exc.value.retry_after == 3.0
    assert len(no_sleep) == 2, "sleeps between attempts only, not after the last"


# --- access denied ----------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_no_access_to_resource_is_its_own_error(status: int) -> None:
    """Finnhub answers paid-plan endpoints with 401 or 403 depending on route.

    This must not collapse into a generic error: on the free tier it usually
    means the endpoint moved to a paid plan, which the operator can act on.
    """
    provider = make_provider(
        lambda _r: httpx.Response(status, json={"error": "You don't have access to this resource."})
    )
    with pytest.raises(AccessDenied, match="not on the current plan"):
        await provider.candles("AAPL", "D", 0, 1)


# --- candles ----------------------------------------------------------------


async def test_candles_zip_parallel_arrays_into_bars() -> None:
    payload = {
        "s": "ok",
        "t": [1700000000, 1700000060],
        "o": [1.0, 2.0],
        "h": [1.5, 2.5],
        "l": [0.5, 1.5],
        "c": [1.2, 2.2],
        "v": [100, 200],
    }
    provider = make_provider(lambda _r: httpx.Response(200, json=payload))
    candles = await provider.candles("AAPL", "5", 0, 1)

    assert len(candles) == 2
    assert candles[0].time == 1700000000
    assert (candles[0].open, candles[0].high, candles[0].low, candles[0].close) == (
        1.0,
        1.5,
        0.5,
        1.2,
    )
    assert candles[1].volume == 200


async def test_no_data_is_an_empty_list_not_an_error() -> None:
    provider = make_provider(lambda _r: httpx.Response(200, json={"s": "no_data"}))
    assert await provider.candles("AAPL", "D", 0, 1) == []


async def test_unexpected_candle_status_raises() -> None:
    provider = make_provider(lambda _r: httpx.Response(200, json={"s": "error"}))
    with pytest.raises(ProviderError):
        await provider.candles("AAPL", "D", 0, 1)


# --- search -----------------------------------------------------------------


async def test_search_maps_results_and_skips_rows_without_a_symbol() -> None:
    payload = {
        "count": 2,
        "result": [
            {
                "description": "APPLE INC",
                "displaySymbol": "AAPL",
                "symbol": "AAPL",
                "type": "Common Stock",
            },
            {"description": "junk row", "displaySymbol": "", "symbol": "", "type": ""},
        ],
    }
    provider = make_provider(lambda _r: httpx.Response(200, json=payload))
    results = await provider.search("apple")

    assert len(results) == 1
    assert results[0].symbol == "AAPL"
    assert results[0].description == "APPLE INC"


# --- stream is Phase 2 ------------------------------------------------------


def test_stream_is_not_implemented_yet() -> None:
    provider = make_provider(lambda _r: httpx.Response(200, json={}))
    with pytest.raises(NotImplementedError, match="Phase 2"):
        provider.stream({"AAPL"})
