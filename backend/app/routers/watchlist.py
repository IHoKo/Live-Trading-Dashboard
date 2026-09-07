"""Watchlist — plan.md §5, §6, §8.1.

    GET    /api/watchlist
    POST   /api/watchlist          {symbol}
    DELETE /api/watchlist/{symbol}

The `watchlist` table has existed since Phase 3 and nothing read it; the tape
was hardcoded. This is what §8.1 means by "an always-on strip of watchlist
symbols".

Order is explicit (`sort_order`) rather than alphabetical or insertion-by-rowid,
so the tape reads the same on every load.
"""

import asyncio
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.deps import get_db, get_provider_optional
from app.providers.base import MarketDataProvider, ProviderError, SymbolNotFound

router = APIRouter(tags=["watchlist"])

# Finnhub's free socket caps at 50 symbols, and the hub caps a socket at 50 too.
# Refusing past that here means the failure is a clear 422 rather than a feed
# that silently stops delivering some rows.
MAX_SYMBOLS = 50


class WatchlistEntry(BaseModel):
    symbol: str
    added_at: str
    sort_order: int


class WatchlistOut(BaseModel):
    symbols: list[str]
    entries: list[WatchlistEntry]
    max_symbols: int = MAX_SYMBOLS


class AddSymbolIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)

    @field_validator("symbol")
    @classmethod
    def _normalise(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol.replace(".", "").replace("-", "").isalnum():
            raise ValueError("Symbol must be alphanumeric.")
        return symbol


def _read(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT symbol, added_at, sort_order FROM watchlist ORDER BY sort_order, symbol"
    ).fetchall()


def _to_out(rows: list[sqlite3.Row]) -> WatchlistOut:
    entries = [
        WatchlistEntry(symbol=r["symbol"], added_at=r["added_at"], sort_order=r["sort_order"])
        for r in rows
    ]
    return WatchlistOut(symbols=[e.symbol for e in entries], entries=entries)


@router.get("/watchlist", response_model=WatchlistOut)
async def get_watchlist(db: Annotated[sqlite3.Connection, Depends(get_db)]) -> WatchlistOut:
    return _to_out(await asyncio.to_thread(_read, db))


@router.post("/watchlist", response_model=WatchlistOut, status_code=201)
async def add_symbol(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    provider: Annotated[MarketDataProvider | None, Depends(get_provider_optional)],
    body: AddSymbolIn,
) -> WatchlistOut:
    symbol = body.symbol

    existing = await asyncio.to_thread(
        lambda: db.execute("SELECT 1 FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"{symbol} is already on the watchlist.")

    count = await asyncio.to_thread(
        lambda: db.execute("SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]
    )
    if count >= MAX_SYMBOLS:
        raise HTTPException(
            status_code=422,
            detail=f"The watchlist holds {MAX_SYMBOLS} symbols. Remove one first.",
        )

    # §11: validate against the provider before storing. A symbol that cannot be
    # priced would sit on the tape showing dashes forever.
    if provider is not None:
        try:
            await provider.quote(symbol)
        except SymbolNotFound as exc:
            raise HTTPException(
                status_code=404, detail=f"{symbol} is not a symbol the price feed knows."
            ) from exc
        except ProviderError:
            # A dead upstream must not stop you editing your own watchlist.
            pass

    def insert() -> list[sqlite3.Row]:
        nxt = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM watchlist").fetchone()[
            "n"
        ]
        db.execute("INSERT INTO watchlist (symbol, sort_order) VALUES (?, ?)", (symbol, nxt))
        return _read(db)

    return _to_out(await asyncio.to_thread(insert))


@router.delete("/watchlist/{symbol}", response_model=WatchlistOut)
async def remove_symbol(
    db: Annotated[sqlite3.Connection, Depends(get_db)], symbol: str
) -> WatchlistOut:
    normalised = symbol.strip().upper()

    def delete() -> tuple[int, list[sqlite3.Row]]:
        cursor = db.execute("DELETE FROM watchlist WHERE symbol = ?", (normalised,))
        return cursor.rowcount, _read(db)

    removed, rows = await asyncio.to_thread(delete)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{normalised} is not on the watchlist.")
    return _to_out(rows)
