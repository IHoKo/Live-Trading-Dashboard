"""Health check — deliberately unauthenticated.

This router is registered directly on the app in `main.py`, never on the
authenticated API router. See `app/deps.py` for why that matters.

Feed state is reported in the *body*, not the status code: the check must return
200 before the upstream market data feed is up, or the machine never passes its
grace period on a cold boot (plan.md §9.2).
"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import get_settings
from app.services.market_hours import is_market_open

router = APIRouter(prefix="/api", tags=["health"])


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    provider: str
    feed: Literal["not_started", "live", "polling", "down"] = "not_started"
    ws_connected: bool = False
    market_open: bool | None = None
    subscribed_symbols: int = 0


@router.get("/health", response_model=Health)
async def health(request: Request) -> Health:
    hub = getattr(request.app.state, "hub", None)
    if hub is None:
        # No market data configured. Still 200 — see the module docstring.
        return Health(provider=get_settings().provider, market_open=is_market_open())
    return Health(
        provider=get_settings().provider,
        feed=hub.state.value,
        ws_connected=hub.upstream_connected,
        market_open=is_market_open(),
        subscribed_symbols=len(hub.tracked_symbols()),
    )
