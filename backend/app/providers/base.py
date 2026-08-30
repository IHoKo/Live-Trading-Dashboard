"""Market data provider interface — plan.md §4.

The interface exists so swapping providers is a one-file change. Nothing outside
`app/providers/` may import a vendor SDK or hardcode a vendor's response shape;
routers and services depend on this module's models only.

`stream()` is part of the interface as §4 defines it, but no implementation
lands until the PriceHub in Phase 2.
"""

from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

# Finnhub's vocabulary, which the other candidates in §4 also speak.
Resolution = Literal["1", "5", "15", "30", "60", "D", "W", "M"]


class Quote(BaseModel):
    symbol: str
    price: float
    change: float | None = None
    change_pct: float | None = None
    high: float | None = None
    low: float | None = None
    open: float | None = None
    prev_close: float | None = None
    # Unix seconds, as reported by the provider — not our clock. Staleness is
    # measured against this.
    as_of: int


class Candle(BaseModel):
    """One OHLCV bar. Field names match what lightweight-charts wants in Phase 4."""

    time: int  # unix seconds, bar open
    open: float
    high: float
    low: float
    close: float
    volume: float


class SymbolMatch(BaseModel):
    symbol: str
    display_symbol: str
    description: str
    type: str


class Trade(BaseModel):
    """A single trade print from the upstream socket. Consumed in Phase 2."""

    symbol: str
    price: float
    timestamp_ms: int
    volume: float | None = None


# --- Errors -----------------------------------------------------------------
# Routers map these to status codes. Keeping them provider-agnostic is what lets
# the swap in §4 stay a one-file change.


class ProviderError(RuntimeError):
    """Upstream failed in a way the caller cannot fix."""


class RateLimited(ProviderError):
    """Upstream returned 429 and retries were exhausted."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AccessDenied(ProviderError):
    """The key is rejected for this resource — wrong key, or a paid endpoint."""


class SymbolNotFound(ProviderError):
    """The provider has no such symbol."""


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    async def quote(self, symbol: str) -> Quote: ...

    async def candles(
        self, symbol: str, resolution: Resolution, frm: int, to: int
    ) -> list[Candle]: ...

    async def search(self, query: str) -> list[SymbolMatch]: ...

    def stream(self, symbols: set[str]) -> AsyncIterator[Trade]: ...
