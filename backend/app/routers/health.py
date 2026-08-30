"""Health check — deliberately unauthenticated.

This router is registered directly on the app in `main.py`, never on the
authenticated API router. See `app/deps.py` for why that matters.

Feed state is reported in the *body*, not the status code: the check must return
200 before the upstream market data feed is up, or the machine never passes its
grace period on a cold boot (plan.md §9.2).
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(prefix="/api", tags=["health"])


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    provider: str
    # "not_started" until the PriceHub lands in Phase 2; then live/polling/down.
    feed: Literal["not_started", "live", "polling", "down"] = "not_started"
    ws_connected: bool = False
    # None until market-hours awareness lands with the hub in Phase 2. Reported
    # as null rather than a guessed boolean — a wrong "open" is worse than none.
    market_open: bool | None = None


@router.get("/health", response_model=Health)
async def health() -> Health:
    return Health(provider=get_settings().provider)
