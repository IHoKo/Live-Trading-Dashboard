"""Twelve Data candles + the composite routing — plan.md §4."""

from typing import Any

import httpx
import pytest

from app.providers.base import (
    AccessDenied,
    Candle,
    ProviderError,
    Quote,
    RateLimited,
    SymbolNotFound,
)
from app.providers.composite import CompositeProvider
from app.providers.twelvedata import BASE_URL, TwelveDataProvider

OK = {
    "meta": {"symbol": "AAPL", "interval": "1day"},
    "values": [
        {
            "datetime": "2026-01-02",
            "open": "100",
            "high": "105",
            "low": "99",
            "close": "104",
            "volume": "1000",
        },
        {
            "datetime": "2026-01-03",
            "open": "104",
            "high": "108",
            "low": "103",
            "close": "107",
            "volume": "900",
        },
    ],
    "status": "ok",
}


def provider(handler: Any) -> TwelveDataProvider:
    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return TwelveDataProvider("k", client=client)


async def test_daily_bars_are_parsed() -> None:
    p = provider(lambda _r: httpx.Response(200, json=OK))
    candles = await p.candles("AAPL", "D", 0, 1)

    assert len(candles) == 2
    assert candles[0].close == 104.0
    assert candles[1].volume == 900.0
    assert candles[0].time < candles[1].time


async def test_intraday_timestamps_are_parsed() -> None:
    payload = {
        "status": "ok",
        "values": [
            {
                "datetime": "2026-01-02 14:30:00",
                "open": "1",
                "high": "1",
                "low": "1",
                "close": "1",
                "volume": "1",
            }
        ],
    }
    p = provider(lambda _r: httpx.Response(200, json=payload))
    assert (await p.candles("AAPL", "5", 0, 1))[0].time > 0


async def test_the_interval_is_mapped_from_our_resolution() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["interval"] = request.url.params.get("interval")
        return httpx.Response(200, json=OK)

    await provider(handler).candles("AAPL", "D", 0, 1)
    assert seen["interval"] == "1day"


async def test_an_unmappable_resolution_is_an_error() -> None:
    with pytest.raises(ProviderError, match="no interval"):
        await provider(lambda _r: httpx.Response(200, json=OK)).candles("AAPL", "X", 0, 1)  # type: ignore[arg-type]


# --- errors arrive as HTTP 200, the same trap Finnhub sets -------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "error", "code": 401, "message": "Invalid API key"}, AccessDenied),
        (
            {"status": "error", "code": 429, "message": "You have run out of API credits"},
            RateLimited,
        ),
        ({"status": "error", "code": 404, "message": "symbol not found"}, SymbolNotFound),
        ({"status": "error", "code": 500, "message": "something else"}, ProviderError),
    ],
)
async def test_error_payloads_map_to_typed_errors(payload: dict, expected: type) -> None:
    with pytest.raises(expected):
        await provider(lambda _r: httpx.Response(200, json=payload)).candles("AAPL", "D", 0, 1)


async def test_a_malformed_bar_is_skipped_not_fatal() -> None:
    payload = {
        "status": "ok",
        "values": [
            {
                "datetime": "2026-01-02",
                "open": "x",
                "high": "1",
                "low": "1",
                "close": "1",
                "volume": "1",
            },
            {
                "datetime": "2026-01-03",
                "open": "1",
                "high": "1",
                "low": "1",
                "close": "2",
                "volume": "1",
            },
        ],
    }
    candles = await provider(lambda _r: httpx.Response(200, json=payload)).candles(
        "AAPL", "D", 0, 1
    )
    assert len(candles) == 1
    assert candles[0].close == 2.0


async def test_a_non_object_payload_is_a_provider_error() -> None:
    with pytest.raises(ProviderError):
        await provider(lambda _r: httpx.Response(200, json=[1, 2])).candles("AAPL", "D", 0, 1)


def test_constructing_without_a_key_fails_loudly() -> None:
    with pytest.raises(ValueError, match="TWELVEDATA_API_KEY"):
        TwelveDataProvider("")


# --- composite routing ------------------------------------------------------


class Primary:
    name = "finnhub"

    async def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, price=1.0, as_of=1)

    async def candles(self, *a: Any) -> list[Candle]:
        raise AssertionError("candles must not go to the primary")

    async def search(self, q: str) -> list:
        return []

    def stream(self, s: set[str]) -> Any:
        return "primary-stream"


class Candles:
    name = "twelvedata"

    async def candles(self, *a: Any) -> list[Candle]:
        return [Candle(time=1, open=1, high=1, low=1, close=1, volume=1)]

    async def quote(self, s: str) -> Quote:
        raise AssertionError("quotes must not go to the candles provider")

    async def search(self, q: str) -> list:
        raise AssertionError("search must not go to the candles provider")

    def stream(self, s: set[str]) -> Any:
        raise AssertionError


async def test_candles_route_to_the_second_provider_and_nothing_else_does() -> None:
    composite = CompositeProvider(Primary(), candles_from=Candles())

    assert (await composite.quote("AAPL")).price == 1.0
    assert await composite.search("a") == []
    assert composite.stream({"AAPL"}) == "primary-stream"
    assert len(await composite.candles("AAPL", "D", 0, 1)) == 1
    assert composite.name == "finnhub+twelvedata"


async def test_without_a_candles_provider_the_error_names_the_fix() -> None:
    composite = CompositeProvider(Primary())
    with pytest.raises(ProviderError, match="TWELVEDATA_API_KEY"):
        await composite.candles("AAPL", "D", 0, 1)
    assert composite.name == "finnhub"
