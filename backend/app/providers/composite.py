"""Route each capability to whichever provider actually serves it — plan.md §4.

Quotes, search and the trade stream stay on Finnhub. Candles go to a second
provider because Finnhub's free tier 403s on `/stock/candle`.

§4 promised that swapping providers would be "a one-file change". This is the
file. Callers see one `MarketDataProvider` and cannot tell the difference.
"""

import logging
from collections.abc import AsyncIterator

from app.providers.base import (
    Candle,
    MarketDataProvider,
    ProviderError,
    Quote,
    Resolution,
    SymbolMatch,
    Trade,
)

logger = logging.getLogger("ticker.provider.composite")


class CompositeProvider(MarketDataProvider):
    def __init__(
        self, primary: MarketDataProvider, *, candles_from: MarketDataProvider | None = None
    ) -> None:
        self._primary = primary
        self._candles = candles_from
        self.name = primary.name if candles_from is None else f"{primary.name}+{candles_from.name}"

    async def quote(self, symbol: str) -> Quote:
        return await self._primary.quote(symbol)

    async def search(self, query: str) -> list[SymbolMatch]:
        return await self._primary.search(query)

    def stream(self, symbols: set[str]) -> AsyncIterator[Trade]:
        return self._primary.stream(symbols)

    async def candles(self, symbol: str, resolution: Resolution, frm: int, to: int) -> list[Candle]:
        if self._candles is None:
            # Same honest 502 as before, with the fix named.
            raise ProviderError(
                "Historical bars are not available: Finnhub's free tier does not include "
                "/stock/candle, and no candles provider is configured. Set "
                "TWELVEDATA_API_KEY, or upgrade the Finnhub plan."
            )
        return await self._candles.candles(symbol, resolution, frm, to)

    async def aclose(self) -> None:
        for provider in (self._primary, self._candles):
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
