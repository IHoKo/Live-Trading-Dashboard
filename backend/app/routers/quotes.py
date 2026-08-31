"""Market data endpoints — plan.md §6, Phase 1.

    GET /api/quotes?symbols=AAPL,MSFT
    GET /api/candles/{symbol}?range=1D
    GET /api/search?q=apple

These hang off the authenticated API router in `main.py`. Unlike /api/health,
they are meant to sit behind the session check once Phase 6 adds it.
"""

import asyncio
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.deps import get_provider
from app.providers.base import (
    AccessDenied,
    Candle,
    MarketDataProvider,
    ProviderError,
    RateLimited,
    Resolution,
    SymbolMatch,
    SymbolNotFound,
)

router = APIRouter(tags=["market-data"])

MAX_SYMBOLS_PER_REQUEST = 50
MAX_SEARCH_RESULTS = 25

# Ceiling on one /api/quotes call. Symbols are fetched sequentially with a
# 10s per-call timeout, so without this a slow upstream could hold a request
# open for MAX_SYMBOLS_PER_REQUEST x 10s. Whatever has been fetched by the
# deadline is returned; the rest come back as unavailable.
QUOTES_DEADLINE_SECONDS = 15.0

ChartRange = Literal["1D", "5D", "1M", "6M", "1Y", "5Y"]

# range -> (resolution, lookback seconds). Market-hours awareness — so that a
# 1D chart on a Sunday reaches back to Friday's session rather than returning
# empty — arrives with the hub in Phase 2 (§10).
_RANGE_SPEC: dict[ChartRange, tuple[Resolution, int]] = {
    "1D": ("5", 60 * 60 * 24),
    "5D": ("15", 60 * 60 * 24 * 5),
    "1M": ("60", 60 * 60 * 24 * 31),
    "6M": ("D", 60 * 60 * 24 * 183),
    "1Y": ("D", 60 * 60 * 24 * 366),
    "5Y": ("W", 60 * 60 * 24 * 1827),
}

# Candle windows snap to a 60s boundary so repeated requests in the same minute
# describe the same window. The cache no longer depends on this — it keys on
# window length — but deterministic bounds keep chart data stable while a user
# clicks between ranges.
_WINDOW_BUCKET_SECONDS = 60


class QuoteOut(BaseModel):
    symbol: str
    price: float
    change: float | None
    change_pct: float | None
    prev_close: float | None
    # Provider's own timestamp, and how old it is by our clock. The LIVE /
    # DELAYED / STALE labelling in §4 is Phase 2's job; this is the raw input
    # that labelling will read.
    as_of: int
    age_seconds: int | None


class QuotesResponse(BaseModel):
    quotes: list[QuoteOut]
    # Symbols with no data this time round: unknown to the provider, or not
    # reached before the rate limit or the deadline. Reported rather than
    # failing the whole request, so one bad ticker can't blank a watchlist.
    unavailable: list[str] = Field(default_factory=list)
    # Set when the result is incomplete, explaining why. Null on a full result.
    notice: str | None = None


class CandlesResponse(BaseModel):
    symbol: str
    range: ChartRange
    resolution: Resolution
    candles: list[Candle]


class SearchResponse(BaseModel):
    query: str
    results: list[SymbolMatch]


def normalize_symbol(raw: str) -> str:
    """Uppercase and strip. §11 requires symbols be validated against the
    provider's search before *storage*; that gate belongs with the write path in
    Phase 3. Read endpoints only normalize."""
    return raw.strip().upper()


def parse_symbols(raw: str) -> list[str]:
    seen: list[str] = []
    for part in raw.split(","):
        symbol = normalize_symbol(part)
        if symbol and symbol not in seen:
            seen.append(symbol)
    if not seen:
        raise HTTPException(status_code=422, detail="No symbols supplied.")
    if len(seen) > MAX_SYMBOLS_PER_REQUEST:
        raise HTTPException(
            status_code=422,
            detail=f"Too many symbols; {MAX_SYMBOLS_PER_REQUEST} max per request.",
        )
    return seen


def candle_window(range_: ChartRange, now: int | None = None) -> tuple[Resolution, int, int]:
    resolution, lookback = _RANGE_SPEC[range_]
    current = int(time.time()) if now is None else now
    to = (current // _WINDOW_BUCKET_SECONDS) * _WINDOW_BUCKET_SECONDS
    return resolution, to - lookback, to


def _raise_for_provider_error(exc: ProviderError) -> None:
    """Translate provider failures into honest status codes.

    AccessDenied in particular must not surface as a generic 500: on Finnhub's
    free tier it most often means the endpoint is on a paid plan, and that is
    something the operator can act on.
    """
    if isinstance(exc, SymbolNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, RateLimited):
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        raise HTTPException(
            status_code=429,
            detail="Upstream market data rate limit reached. Try again shortly.",
            headers=headers,
        ) from exc
    if isinstance(exc, AccessDenied):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/quotes", response_model=QuotesResponse)
async def get_quotes(
    provider: Annotated[MarketDataProvider, Depends(get_provider)],
    symbols: Annotated[str, Query(min_length=1, description="Comma-separated, e.g. AAPL,MSFT")],
) -> QuotesResponse:
    wanted = parse_symbols(symbols)
    now = int(time.time())

    out: list[QuoteOut] = []
    unavailable: list[str] = []
    notice: str | None = None
    rate_limited: RateLimited | None = None

    # Sequential on purpose. Fanning out concurrently would multiply the burst
    # against a 60 calls/minute budget, and the 5s cache already collapses the
    # repeat traffic that actually dominates here.
    try:
        async with asyncio.timeout(QUOTES_DEADLINE_SECONDS):
            for symbol in wanted:
                try:
                    quote = await provider.quote(symbol)
                except SymbolNotFound:
                    unavailable.append(symbol)
                    continue
                except RateLimited as exc:
                    # Stop, but keep what we already have. Discarding fetched
                    # quotes here would contradict `unavailable` existing at all.
                    rate_limited = exc
                    break
                except ProviderError as exc:
                    _raise_for_provider_error(exc)
                    raise  # unreachable; satisfies the type checker
                out.append(
                    QuoteOut(
                        symbol=quote.symbol,
                        price=quote.price,
                        change=quote.change,
                        change_pct=quote.change_pct,
                        prev_close=quote.prev_close,
                        as_of=quote.as_of,
                        age_seconds=max(0, now - quote.as_of) if quote.as_of else None,
                    )
                )
    except TimeoutError:
        notice = f"Upstream was too slow; stopped after {QUOTES_DEADLINE_SECONDS:g}s."

    if rate_limited is not None:
        notice = "Upstream rate limit reached; some symbols were not fetched."

    fetched = {q.symbol for q in out}
    missed = [s for s in wanted if s not in fetched and s not in unavailable]
    unavailable.extend(missed)

    # Nothing usable came back, so there is no partial result to serve — report
    # the underlying failure instead of a misleadingly empty 200.
    if not out and rate_limited is not None:
        _raise_for_provider_error(rate_limited)

    return QuotesResponse(quotes=out, unavailable=unavailable, notice=notice)


@router.get("/candles/{symbol}", response_model=CandlesResponse)
async def get_candles(
    provider: Annotated[MarketDataProvider, Depends(get_provider)],
    symbol: str,
    response: Response,
    range: Annotated[ChartRange, Query(description="1D/5D/1M/6M/1Y/5Y")] = "1D",
) -> CandlesResponse:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise HTTPException(status_code=422, detail="Symbol is required.")

    resolution, frm, to = candle_window(range)
    try:
        candles = await provider.candles(normalized, resolution, frm, to)
    except ProviderError as exc:
        _raise_for_provider_error(exc)
        raise  # unreachable

    response.headers["Cache-Control"] = "private, max-age=60"
    return CandlesResponse(symbol=normalized, range=range, resolution=resolution, candles=candles)


@router.get("/search", response_model=SearchResponse)
async def search_symbols(
    provider: Annotated[MarketDataProvider, Depends(get_provider)],
    q: Annotated[str, Query(min_length=1, max_length=64, description="Company or ticker")],
) -> SearchResponse:
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query is required.")

    try:
        results = await provider.search(query)
    except ProviderError as exc:
        _raise_for_provider_error(exc)
        raise  # unreachable

    return SearchResponse(query=query, results=results[:MAX_SEARCH_RESULTS])
