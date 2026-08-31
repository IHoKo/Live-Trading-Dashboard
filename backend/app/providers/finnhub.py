"""Finnhub implementation of MarketDataProvider — plan.md §4.

The API key travels in the `X-Finnhub-Token` header rather than a query string,
so it stays out of URLs, redirects, and access logs (§11: keys are server-side
only, and that includes not scattering them through log lines).
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import websockets

from app.providers.base import (
    AccessDenied,
    Candle,
    MarketDataProvider,
    ProviderError,
    Quote,
    RateLimited,
    Resolution,
    SymbolMatch,
    SymbolNotFound,
    Trade,
)

logger = logging.getLogger("ticker.provider.finnhub")

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubProvider(MarketDataProvider):
    """Free tier is 60 calls/min. The caching wrapper in `providers/cache.py`
    is what keeps a dashboard of N symbols inside that budget; the backoff here
    is the second line for when it isn't enough.
    """

    name = "finnhub"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
        base_backoff: float = 0.5,
        timeout: float = 10.0,
    ) -> None:
        if not api_key:
            raise ValueError("FINNHUB_API_KEY is required to construct FinnhubProvider")
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._base_backoff = base_backoff
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"X-Finnhub-Token": api_key, "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- transport ----------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """GET with exponential backoff on 429 (plan.md §10, Phase 1).

        Honours Retry-After when the upstream sends one; otherwise backs off
        exponentially with jitter, so N symbols rate-limited at the same instant
        don't retry in lockstep and trip the limit again.
        """
        last_retry_after: float | None = None

        for attempt in range(self._max_attempts):
            try:
                res = await self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                raise ProviderError(f"finnhub timed out on {path}") from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"finnhub request failed on {path}: {exc}") from exc

            if res.status_code == 429:
                last_retry_after = _retry_after_seconds(res)
                if attempt == self._max_attempts - 1:
                    break
                delay = last_retry_after
                if delay is None:
                    # Jitter is proportional (+/-25%), not a fixed window: a
                    # fixed 0.25s spread would swamp the first backoff and be
                    # noise by the third, and it lets the growth stay monotonic.
                    delay = self._base_backoff * (2**attempt) * random.uniform(1.0, 1.25)
                logger.warning(
                    "finnhub 429 on %s, retrying in %.2fs (attempt %d/%d)",
                    path,
                    delay,
                    attempt + 1,
                    self._max_attempts,
                )
                await asyncio.sleep(delay)
                continue

            # Finnhub answers "you don't have access to this resource" with 401
            # or 403 depending on the endpoint. Both mean the same thing to us:
            # this key cannot read this data.
            if res.status_code in (401, 403):
                raise AccessDenied(
                    f"finnhub denied {path} (HTTP {res.status_code}). Either the key is "
                    f"invalid or this endpoint is not on the current plan."
                )

            if res.status_code >= 400:
                raise ProviderError(f"finnhub returned HTTP {res.status_code} on {path}")

            try:
                return res.json()
            except ValueError as exc:
                raise ProviderError(f"finnhub returned non-JSON on {path}") from exc

        raise RateLimited(
            f"finnhub rate limit not cleared after {self._max_attempts} attempts on {path}",
            retry_after=last_retry_after,
        )

    # --- interface ----------------------------------------------------------

    async def quote(self, symbol: str) -> Quote:
        data = _as_object(await self._get("/quote", {"symbol": symbol}), "/quote")

        # An unknown symbol is a 200 with a zero-filled body, not a 404.
        if not data or (data.get("c") in (0, None) and data.get("pc") in (0, None)):
            raise SymbolNotFound(f"finnhub has no quote for {symbol!r}")

        with _parsing("/quote", symbol):
            return Quote(
                symbol=symbol,
                price=float(data["c"]),
                change=_opt_float(data.get("d")),
                change_pct=_opt_float(data.get("dp")),
                high=_opt_float(data.get("h")),
                low=_opt_float(data.get("l")),
                open=_opt_float(data.get("o")),
                prev_close=_opt_float(data.get("pc")),
                as_of=int(data.get("t") or 0),
            )

    async def candles(self, symbol: str, resolution: Resolution, frm: int, to: int) -> list[Candle]:
        data = _as_object(
            await self._get(
                "/stock/candle",
                {"symbol": symbol, "resolution": resolution, "from": frm, "to": to},
            ),
            "/stock/candle",
        )

        status = data.get("s")
        if status == "no_data":
            return []
        if status != "ok":
            raise ProviderError(f"finnhub candle status {status!r} for {symbol!r}")

        with _parsing("/stock/candle", symbol):
            return [
                Candle(
                    time=int(t),
                    open=float(o),
                    high=float(h),
                    low=float(low),
                    close=float(c),
                    volume=float(v),
                )
                for t, o, h, low, c, v in zip(
                    data["t"], data["o"], data["h"], data["l"], data["c"], data["v"], strict=True
                )
            ]

    async def search(self, query: str) -> list[SymbolMatch]:
        data = _as_object(await self._get("/search", {"q": query}), "/search")
        with _parsing("/search", query):
            return [
                SymbolMatch(
                    symbol=row["symbol"],
                    display_symbol=row.get("displaySymbol") or row["symbol"],
                    description=row.get("description", ""),
                    type=row.get("type", ""),
                )
                for row in data.get("result", [])
                if isinstance(row, dict) and row.get("symbol")
            ]

    async def stream(self, symbols: set[str]) -> AsyncIterator[Trade]:
        """Yield trade prints for `symbols` from Finnhub's socket.

        The symbol set is fixed for the life of the iterator, per the §4
        interface. The hub restarts the stream when its subscription set
        changes; watchlist edits are human-paced, so a brief reconnect is
        cheaper than leaking a vendor-specific "resubscribe" method through
        the provider abstraction.

        Unlike the REST calls, the key has to travel in the query string —
        the WS handshake takes no custom headers. It is TLS-encrypted in
        transit, but never log this URL.
        """
        if not symbols:
            return

        url = f"wss://ws.finnhub.io?token={self._api_key}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as sock:
            for symbol in sorted(symbols):
                await sock.send(json.dumps({"type": "subscribe", "symbol": symbol}))
            logger.info("finnhub socket open, subscribed to %d symbol(s)", len(symbols))

            async for raw in sock:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") != "trade":
                    continue  # 'ping' and errors are not trades
                for row in msg.get("data") or []:
                    try:
                        yield Trade(
                            symbol=row["s"],
                            price=float(row["p"]),
                            timestamp_ms=int(row["t"]),
                            volume=_opt_float(row.get("v")),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue  # a malformed print must not kill the feed


def _as_object(data: Any, path: str) -> dict[str, Any]:
    """Upstream must hand back a JSON object here.

    Anything else — a bare list, a string — would blow up on `.get` as an
    AttributeError, which escapes ProviderError and reaches the client as a 500.
    An upstream that changed shape is an upstream problem: say so with a 502.
    """
    if not isinstance(data, dict):
        raise ProviderError(
            f"finnhub returned {type(data).__name__}, expected an object, on {path}"
        )
    return data


@contextmanager
def _parsing(path: str, subject: str) -> Iterator[None]:
    """Turn payload-shape failures into ProviderError.

    Ragged parallel arrays (`zip(strict=True)`), a price of "n/a", a missing
    key — all of them are the upstream disagreeing with its own documented
    shape, and all of them used to surface as an unhandled 500.
    """
    try:
        yield
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderError(
            f"finnhub sent an unparseable payload on {path} for {subject!r}: {exc}"
        ) from exc


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _retry_after_seconds(res: httpx.Response) -> float | None:
    raw = res.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The HTTP-date form is legal but Finnhub sends seconds; not worth parsing.
        return None
