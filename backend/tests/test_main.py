"""Phase 0 route contract.

These cover the two things plan.md §9.4 is emphatic about, both of which fail in
ways that look like something else entirely:

  * /api/health carries no auth — otherwise the Fly check 401s, the machine is
    marked unhealthy, and `fly deploy` rolls back on a perfectly healthy app.
  * the SPA catch-all serves index.html for unmatched paths — otherwise a hard
    refresh at /portfolio 404s, but only in production, and only on refresh.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.health import router as health_router

client = TestClient(app)


# --- /api/health ------------------------------------------------------------


def test_health_is_reachable_without_credentials() -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_health_route_has_no_dependencies() -> None:
    """Structural guard: catches auth added to the health router by mistake.

    Inspects the health router directly rather than walking `app.routes` —
    FastAPI wraps included routers, and the wrapper's shape is an internal
    detail that has changed between versions.
    """
    assert health_router.dependencies == []
    route = next(r for r in health_router.routes if r.path == "/api/health")
    assert route.dependencies == []  # type: ignore[attr-defined]
    assert route.dependant.dependencies == []  # type: ignore[attr-defined]


def test_no_app_wide_auth_would_wrap_health() -> None:
    """Auth belongs on the API router, never as a global dep or middleware."""
    assert app.router.dependencies == []
    assert app.user_middleware == []


def test_health_reports_feed_state_in_body_not_status_code() -> None:
    """200 even with no feed, or the machine never passes a cold boot."""
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    # No FINNHUB_API_KEY in the test env, so there is no hub.
    assert body["feed"] == "not_started"
    assert body["ws_connected"] is False
    # Market hours no longer depend on the feed, so this is a real answer.
    assert isinstance(body["market_open"], bool)


# --- SPA catch-all ----------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/portfolio", "/a/deep/link", "/portfolio?tab=x"])
def test_spa_paths_serve_the_shell(path: str) -> None:
    res = client.get(path)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "<title>Ticker</title>" in res.text


def test_spa_deep_link_answers_head() -> None:
    """plan.md §9.5 verifies the deep link with `curl -sI`, which sends HEAD."""
    assert client.head("/portfolio").status_code == 200


@pytest.mark.parametrize("path", ["/api/nope", "/api/portfolio", "/ws/prices"])
def test_unmatched_api_and_ws_paths_are_404_not_the_shell(path: str) -> None:
    """A typo'd endpoint must not return HTML with a 200."""
    res = client.get(path)
    assert res.status_code == 404
    assert res.headers["content-type"].startswith("application/json")


# --- static files -----------------------------------------------------------


def test_hashed_assets_are_served_from_the_assets_mount() -> None:
    res = client.get("/assets/index-abc123.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]


def test_root_level_public_files_are_served() -> None:
    assert client.get("/favicon.ico").status_code == 200


@pytest.mark.parametrize("path", ["/../pyproject.toml", "/%2e%2e/pyproject.toml"])
def test_traversal_cannot_escape_the_static_dir(path: str) -> None:
    res = client.get(path)
    assert "[project]" not in res.text
    assert "<title>Ticker</title>" in res.text
