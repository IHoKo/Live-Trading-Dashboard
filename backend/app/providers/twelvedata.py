"""Twelve Data — historical bars only. plan.md §4.

Finnhub's free tier returns 403 for `/stock/candle` (verified in production), so
candles come from a second source while quotes and search stay on Finnhub. This
is the swap §4's interface was designed for; see `composite.py` for the routing.

Chosen over Alpha Vantage on the numbers: 800 credits/day and 8/min here, versus
25 requests/day there — six range buttons would exhaust an Alpha Vantage key
before lunch. Verify current terms before relying on either; §4 is emphatic that
these change quietly, and it was right about Finnhub.
"""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from app.providers.base import (
    AccessDenied,
    Candle,
    ProviderError,
    Quote,
    RateLimited,
    Resolution,
    SymbolMatch,
    SymbolNotFound,
    Trade,
)

logger = logging.getLogger("ticker.provider.twelvedata")

BASE_URL = "https://api.twelvedata.com"

# Our resolutions -> Twelve Data intervals.
_INTERVALS: dict[str, str] = {
    "1": "1min",
    "5": "5min",
    "15": "15min",
    "30": "30min",
    "60": "1h",
    "D": "1day",
    "W": "1week",
    "M": "1month",
}

MAX_OUTPUTSIZE = 5000  # free-tier ceiling on data points per request


class TwelveDataProvider:
    """Implements the candle half of MarketDataProvider.

    quote/search/stream raise deliberately: this provider exists to fill one
    gap, and a half-working quote path would be worse than an obvious one.
    """

    name = "twelvedata"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError("TWELVEDATA_API_KEY is required to construct TwelveDataProvider")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def candles(self, symbol: str, resolution: Resolution, frm: int, to: int) -> list[Candle]:
        interval = _INTERVALS.get(resolution)
        if interval is None:
            raise ProviderError(f"twelvedata has no interval for resolution {resolution!r}")

        params = {
            "symbol": symbol,
            "interval": interval,
            "start_date": _stamp(frm),
            "end_date": _stamp(to),
            "outputsize": MAX_OUTPUTSIZE,
            "order": "ASC",
            "apikey": self._api_key,
        }
        try:
            res = await self._client.get("/time_series", params=params)
        except httpx.TimeoutException as exc:
            raise ProviderError("twelvedata timed out on /time_series") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"twelvedata request failed: {exc}") from exc

        if res.status_code == 429:
            raise RateLimited("twelvedata rate limit reached")
        if res.status_code in (401, 403):
            raise AccessDenied(f"twelvedata denied /time_series (HTTP {res.status_code}).")
        if res.status_code >= 400:
            raise ProviderError(f"twelvedata returned HTTP {res.status_code}")

        try:
            data = res.json()
        except ValueError as exc:
            raise ProviderError("twelvedata returned non-JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("twelvedata returned an unexpected payload shape")

        # Errors arrive as HTTP 200 with a status field, the same trap Finnhub
        # sets with its zero-filled quotes.
        if data.get("status") == "error":
            message = str(data.get("message", "unknown error"))
            code = data.get("code")
            if code in (401, 403) or "api key" in message.lower():
                raise AccessDenied(f"twelvedata rejected the key: {message}")
            if code == 429 or "limit" in message.lower():
                raise RateLimited(f"twelvedata: {message}")
            if "not found" in message.lower() or "symbol" in message.lower():
                raise SymbolNotFound(f"twelvedata has no data for {symbol!r}: {message}")
            raise ProviderError(f"twelvedata: {message}")

        values = data.get("values") or []
        out: list[Candle] = []
        for row in values:
            if not isinstance(row, dict):
                continue
            try:
                out.append(
                    Candle(
                        time=_parse_datetime(row["datetime"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue  # one bad bar must not lose the whole series
        return out

    # --- the rest of the interface, deliberately absent -------------------

    async def quote(self, symbol: str) -> Quote:
        raise NotImplementedError("Quotes come from Finnhub; see providers/composite.py")

    async def search(self, query: str) -> list[SymbolMatch]:
        raise NotImplementedError("Search comes from Finnhub; see providers/composite.py")

    def stream(self, symbols: set[str]) -> AsyncIterator[Trade]:
        raise NotImplementedError("The trade stream comes from Finnhub.")


def _stamp(unix_seconds: int) -> str:
    return datetime.fromtimestamp(unix_seconds, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _parse_datetime(value: Any) -> int:
    """Twelve Data returns 'YYYY-MM-DD' for daily bars and
    'YYYY-MM-DD HH:MM:SS' for intraday."""
    text = str(value)
    fmt = "%Y-%m-%d %H:%M:%S" if " " in text else "%Y-%m-%d"
    return int(datetime.strptime(text, fmt).replace(tzinfo=UTC).timestamp())
