"""Portfolio and transaction endpoints — plan.md §6, Phase 3.

    GET    /api/portfolio        -> positions + totals + allocation
    GET    /api/transactions     -> paginated history
    POST   /api/transactions     -> record a buy or sell
    DELETE /api/transactions/{id} -> reverses and rebuilds lots

The lot engine is synchronous, so each unit of work goes through
`asyncio.to_thread`: one hop per operation, and the engine stays a plain
function the tests can drive directly.

`/api/portfolio/performance` is Phase 4 — it needs historical closes.
"""

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.deps import get_db, get_provider_optional
from app.errors import raise_for_provider_error
from app.providers.base import MarketDataProvider, ProviderError
from app.services import performance
from app.services import portfolio as engine

router = APIRouter(tags=["portfolio"])

MAX_PAGE_SIZE = 200

PerformanceRange = Literal["1M", "6M", "1Y", "5Y", "ALL"]

_DAY = 60 * 60 * 24
_PERFORMANCE_LOOKBACK: dict[str, int] = {
    "1M": 31 * _DAY,
    "6M": 183 * _DAY,
    "1Y": 366 * _DAY,
    "5Y": 1827 * _DAY,
}


class TransactionIn(BaseModel):
    """§11: Pydantic on every input."""

    symbol: str = Field(min_length=1, max_length=16)
    side: Literal["BUY", "SELL"]
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = Field(default=0.0, ge=0)
    executed_at: str | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("symbol")
    @classmethod
    def _normalise(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol.replace(".", "").replace("-", "").isalnum():
            raise ValueError("Symbol must be alphanumeric.")
        return symbol


class TransactionOut(BaseModel):
    id: int
    symbol: str
    side: str
    quantity: float
    price: float
    fees: float
    executed_at: str
    note: str | None
    source: str


class TransactionsPage(BaseModel):
    transactions: list[TransactionOut]
    total: int
    limit: int
    offset: int


class PositionOut(BaseModel):
    symbol: str
    quantity: float
    cost_basis: float
    average_cost: float
    # Filled from the price cache when available; null when it isn't, rather
    # than guessed. §4: never render a stale number as if it were live.
    last_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pct: float | None = None
    day_change_pct: float | None = None
    allocation_pct: float | None = None


class PerformancePointOut(BaseModel):
    date: str
    value: float
    cost_basis: float
    unrealized: float


class PerformanceOut(BaseModel):
    range: str
    points: list[PerformancePointOut]
    # Symbols with no historical data. Reported rather than silently dropped:
    # a chart missing a holding is worse than a chart that says so.
    unpriced_symbols: list[str]


class PortfolioOut(BaseModel):
    positions: list[PositionOut]
    cost_basis_total: float
    market_value_total: float | None
    unrealized_pnl_total: float | None
    realized_pnl_total: float
    priced_symbols: int
    unpriced_symbols: list[str]


def _row_to_out(row: sqlite3.Row) -> TransactionOut:
    # `.keys()` is required: iterating a sqlite3.Row yields values, not keys,
    # so the SIM118 "simplification" would silently build a garbage dict.
    return TransactionOut(**{k: row[k] for k in row.keys() if k != "created_at"})  # noqa: SIM118


@router.get("/portfolio", response_model=PortfolioOut)
async def get_portfolio(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    provider: Annotated[MarketDataProvider | None, Depends(get_provider_optional)],
) -> PortfolioOut:
    held = await asyncio.to_thread(engine.positions, db)
    realized = await asyncio.to_thread(engine.realized_total, db)

    prices: dict[str, tuple[float, float | None]] = {}
    if provider is not None:
        for position in held:
            try:
                quote = await provider.quote(position.symbol)
            except ProviderError:
                # A dead provider must not blank the portfolio: cost basis and
                # realized P/L are ours, and stay correct without any price.
                continue
            prices[position.symbol] = (quote.price, quote.change_pct)

    market_total = sum(prices[p.symbol][0] * p.quantity for p in held if p.symbol in prices)
    priced_all = bool(held) and len(prices) == len(held)

    out: list[PositionOut] = []
    for position in held:
        price, change_pct = prices.get(position.symbol, (None, None))
        value = price * position.quantity if price is not None else None
        unrealized = value - position.cost_basis if value is not None else None
        out.append(
            PositionOut(
                symbol=position.symbol,
                quantity=position.quantity,
                cost_basis=position.cost_basis,
                average_cost=round(position.average_cost, 4),
                last_price=price,
                market_value=round(value, 4) if value is not None else None,
                unrealized_pnl=round(unrealized, 4) if unrealized is not None else None,
                unrealized_pct=(
                    round(unrealized / position.cost_basis * 100, 4)
                    if unrealized is not None and position.cost_basis
                    else None
                ),
                day_change_pct=change_pct,
                allocation_pct=(
                    round(value / market_total * 100, 4)
                    if value is not None and market_total
                    else None
                ),
            )
        )

    return PortfolioOut(
        positions=out,
        cost_basis_total=round(sum(p.cost_basis for p in held), 4),
        market_value_total=round(market_total, 4) if priced_all else None,
        unrealized_pnl_total=(
            round(market_total - sum(p.cost_basis for p in held), 4) if priced_all else None
        ),
        realized_pnl_total=realized,
        priced_symbols=len(prices),
        unpriced_symbols=[p.symbol for p in held if p.symbol not in prices],
    )


@router.get("/portfolio/performance", response_model=PerformanceOut)
async def get_performance(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    provider: Annotated[MarketDataProvider | None, Depends(get_provider_optional)],
    range: Annotated[PerformanceRange, Query(description="1M/6M/1Y/5Y/ALL")] = "1Y",
) -> PerformanceOut:
    """Portfolio value over time, reconstructed from transactions + closes (§6)."""
    if provider is None:
        raise HTTPException(
            status_code=503, detail="Market data is unavailable, so history cannot be rebuilt."
        )

    now = int(time.time())
    frm = 0 if range == "ALL" else now - _PERFORMANCE_LOOKBACK[range]
    try:
        points, unpriced = await performance.portfolio_performance(db, provider, frm=frm, to=now)
    except ProviderError as exc:
        raise_for_provider_error(exc)
        raise  # unreachable

    return PerformanceOut(
        range=range,
        points=[
            PerformancePointOut(
                date=p.date, value=p.value, cost_basis=p.cost_basis, unrealized=p.unrealized
            )
            for p in points
        ],
        unpriced_symbols=unpriced,
    )


@router.get("/transactions", response_model=TransactionsPage)
async def list_transactions(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    symbol: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TransactionsPage:
    where, params = "", []
    if symbol:
        where = " WHERE symbol = ?"
        params = [symbol.strip().upper()]

    def read() -> tuple[list[sqlite3.Row], int]:
        total = db.execute(f"SELECT COUNT(*) AS n FROM transactions{where}", params).fetchone()["n"]
        rows = db.execute(
            f"SELECT * FROM transactions{where} ORDER BY executed_at DESC, id DESC"
            " LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return rows, total

    rows, total = await asyncio.to_thread(read)
    return TransactionsPage(
        transactions=[_row_to_out(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.post("/transactions", response_model=TransactionOut, status_code=201)
async def create_transaction(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    body: TransactionIn,
) -> TransactionOut:
    new = engine.NewTransaction(
        symbol=body.symbol,
        side=body.side,
        quantity=body.quantity,
        price=body.price,
        fees=body.fees,
        executed_at=body.executed_at or datetime.now(UTC).isoformat(timespec="seconds"),
        note=body.note,
        source="ui",
    )
    try:
        tx_id = await asyncio.to_thread(engine.record_transaction, db, new)
    except engine.OversellError as exc:
        # The user's ledger disagrees with them; that is a 409, not a 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except engine.PortfolioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = await asyncio.to_thread(
        lambda: db.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    )
    return _row_to_out(row)


@router.delete("/transactions/{tx_id}", status_code=204)
async def remove_transaction(
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    tx_id: int,
) -> None:
    try:
        await asyncio.to_thread(engine.delete_transaction, db, tx_id)
    except engine.TransactionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except engine.OversellError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"{exc} Delete the later sale first.",
        ) from exc
