"""FastAPI application: static SPA mount, routers, lifespan.

Route registration order is load-bearing. The SPA catch-all matches everything,
so it must be registered last — after health, after the API router, after the
asset mount.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routers import health

logger = logging.getLogger("ticker")

settings = get_settings()

# Resolved once at import so the traversal guard in the catch-all compares
# against an absolute, symlink-free base.
STATIC_DIR = settings.static_dir.resolve()
ASSETS_DIR = STATIC_DIR / "assets"
INDEX_HTML = STATIC_DIR / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and graceful shutdown.

    Schema migrations belong here, not in a Fly `release_command`: the release
    machine runs with no volumes attached, so a migration there writes to an
    ephemeral disk and vanishes. Whatever goes here runs on every boot and must
    be idempotent (plan.md §9.4). Phase 3 adds it.
    """
    if not INDEX_HTML.is_file():
        logger.warning(
            "No frontend build at %s — API works, but the SPA will 503. "
            "Run `npm run build` in frontend/ and copy dist/ to %s.",
            INDEX_HTML,
            STATIC_DIR,
        )
    yield
    # Phase 2 closes the upstream feed and the browser sockets here. fly.toml
    # allows 30s for it (kill_signal = SIGTERM, kill_timeout = 30).


app = FastAPI(
    title="Ticker",
    version="0.1.0",
    lifespan=lifespan,
)

# --- 1. Health: registered directly on the app, outside any auth. -----------
# Do not move this onto `api` below, and do not add auth middleware that would
# wrap it. See app/deps.py.
app.include_router(health.router)

# --- 2. Authenticated API ---------------------------------------------------
# Phase 6 attaches auth here and only here:
#     api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])
# Quotes, portfolio, transactions, watchlist and chat routers hang off this.
api = APIRouter(prefix="/api")
app.include_router(api)

# --- 3. Built frontend assets ----------------------------------------------
# Hashed, immutable files from the Vite build. Mounted at /assets specifically
# so it does NOT swallow the API and WS routes — mounting StaticFiles at "/"
# is what makes a hard refresh at /portfolio 404 (plan.md §9.4).
if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


# --- 4. SPA catch-all: must be registered last ------------------------------
# HEAD is explicit: unlike Starlette's plain Route, FastAPI's APIRoute does not
# imply HEAD from GET, and the deploy check in plan.md §9.5 probes this route
# with `curl -sI` — which would otherwise come back 405.
@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def spa(full_path: str) -> FileResponse:
    """Serve the SPA shell for any unmatched path.

    A hard refresh at /portfolio has no server-side route — React Router owns
    that path — so it must return index.html rather than 404.
    """
    # Unmatched API and WS paths are genuine 404s. Without this they would fall
    # through and return the HTML shell with a 200, which turns a typo'd
    # endpoint into a confusing JSON-parse error in the client.
    if full_path == "api" or full_path.startswith(("api/", "ws/")):
        raise HTTPException(status_code=404, detail="Not found")

    # Root-level build output that isn't under /assets: favicon.ico,
    # robots.txt, vite.svg, anything dropped in frontend/public.
    if full_path:
        candidate = (STATIC_DIR / full_path).resolve()
        if candidate.is_relative_to(STATIC_DIR) and candidate.is_file():
            return FileResponse(candidate)

    if not INDEX_HTML.is_file():
        raise HTTPException(
            status_code=503,
            detail="Frontend not built. Run `npm run build` in frontend/.",
        )
    return FileResponse(INDEX_HTML)
