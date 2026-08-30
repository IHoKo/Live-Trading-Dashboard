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


async def require_session() -> None:
    """Placeholder for the Phase 6 session-cookie check.

    Not wired to any route yet. When it is, it goes on the authenticated API
    router in `main.py` — never on the health router.
    """
    raise NotImplementedError("Auth lands in Phase 6 (plan.md §10).")
