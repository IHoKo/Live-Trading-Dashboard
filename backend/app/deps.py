"""Shared FastAPI dependencies: auth and DB session.

Both arrive in later phases (auth in Phase 6, the DB in Phase 3). This module
exists now so the wiring in `main.py` is unambiguous about where they attach —
and, just as importantly, where they must *not*.

`/api/health` never takes an auth dependency. If session middleware or a router
level `Depends(require_session)` ever wraps it, the Fly health check gets a 401,
the machine is marked unhealthy, and `fly deploy` rolls back with an error that
looks nothing like the cause — while the app itself is running fine
(plan.md §9.2, §9.4).
"""

import sqlite3
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from app.services.auth import COOKIE_NAME

if TYPE_CHECKING:
    from app.providers.base import MarketDataProvider


def require_session(request: Request) -> None:
    """Reject anything without a valid session cookie (§11).

    Attached to the authenticated API router in `main.py` and to the price
    socket — never to `/api/health`, and never to the SPA catch-all, which has
    to serve the login page to a logged-out browser.

    With no passphrase configured the app is left open rather than bricked: a
    half-configured deploy should still be reachable so you can fix it. It says
    so loudly at boot.
    """
    sessions = request.app.state.sessions
    if not sessions.configured:
        return
    if not sessions.valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not authenticated.")


def get_provider(request: Request) -> "MarketDataProvider":
    """The shared market data provider, built once in the lifespan hook.

    Absent only when FINNHUB_API_KEY is unset. That is a 503 on the market data
    endpoints, never a crash at import or boot: the app must still start and
    still answer /api/health, or a missing secret takes the machine down instead
    of degrading one feature (plan.md §9.3).
    """
    provider = getattr(request.app.state, "provider", None)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="Market data is unavailable: FINNHUB_API_KEY is not configured.",
        )
    return provider


def get_db(request: Request) -> sqlite3.Connection:
    """The process-wide SQLite connection, opened and migrated at boot.

    One connection is right here: one machine, one writer, WAL enabled, and
    every unit of work already serialised through `asyncio.to_thread`.
    """
    conn = getattr(request.app.state, "db", None)
    if conn is None:
        raise HTTPException(status_code=503, detail="Database is not available.")
    return conn


def get_provider_optional(request: Request) -> "MarketDataProvider | None":
    """Like `get_provider`, but returns None instead of 503.

    The portfolio is meaningful without live prices — cost basis and realized
    P/L are ours, not the provider's — so a missing key or a dead upstream
    should degrade the view, not refuse it.
    """
    return getattr(request.app.state, "provider", None)
