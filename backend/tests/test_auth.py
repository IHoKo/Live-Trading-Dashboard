"""Passphrase auth — plan.md §11.

"This is a single-user app on the public internet; assume it will be found."
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.services.auth import COOKIE_NAME, SessionManager
from app.services.pending import RateLimiter

SECRET = "test-secret-not-a-real-one"
PASSPHRASE = "correct-horse-battery-staple"

# Routes that must be closed to a stranger. /api/health is deliberately absent.
PROTECTED = [
    ("GET", "/api/portfolio"),
    ("GET", "/api/transactions"),
    ("POST", "/api/transactions"),
    ("GET", "/api/quotes?symbols=AAPL"),
    ("GET", "/api/search?q=a"),
    ("GET", "/api/candles/AAPL"),
    ("POST", "/api/chat/stream"),
    ("POST", "/api/chat/confirm"),
    ("GET", "/api/chat/history"),
    ("DELETE", "/api/chat/history"),
]


@pytest.fixture
def configured():
    """A real migrated DB too, so a 200 behind the gate means the route actually
    worked rather than merely getting past auth."""
    from app.db.connection import connect, migrate

    conn = connect(":memory:")
    migrate(conn)
    app.state.db = conn
    app.state.sessions = SessionManager(SECRET, PASSPHRASE)
    app.state.login_limiter = RateLimiter(capacity=50, per_seconds=300.0)
    yield
    app.state.sessions = SessionManager(None, None)
    app.state.db = None
    conn.close()


def https_client() -> TestClient:
    """https base URL on purpose: the session cookie is `Secure`, and httpx
    correctly refuses to store a Secure cookie received over plaintext http.
    A plain TestClient(app) silently drops it and every auth test then fails
    for the wrong reason."""
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def client(configured) -> TestClient:
    return https_client()


def login(client: TestClient, passphrase: str = PASSPHRASE):
    return client.post("/api/auth/login", json={"passphrase": passphrase})


# --- the gate ---------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_routes_reject_a_stranger(client: TestClient, method: str, path: str) -> None:
    res = client.request(method, path, json={})
    assert res.status_code == 401, f"{method} {path} was reachable without a session"


def test_health_is_never_behind_auth(client: TestClient) -> None:
    """If the Fly check gets a 401, the machine is marked unhealthy and the
    deploy rolls back on a perfectly healthy app (§9.2)."""
    assert client.get("/api/health").status_code == 200


def test_the_spa_shell_is_served_to_a_logged_out_browser(client: TestClient) -> None:
    """The catch-all has to serve the login page to someone with no session."""
    assert client.get("/portfolio").status_code == 200
    assert client.get("/").status_code == 200


def test_logging_in_opens_the_api(client: TestClient) -> None:
    assert client.get("/api/portfolio").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/portfolio").status_code == 200


def test_logging_out_closes_it_again(client: TestClient) -> None:
    login(client)
    assert client.get("/api/portfolio").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/portfolio").status_code == 401


# --- the cookie -------------------------------------------------------------


def test_the_session_cookie_has_the_flags_that_matter(client: TestClient) -> None:
    res = login(client)
    raw = res.headers["set-cookie"]
    assert "HttpOnly" in raw, "an XSS bug must not be able to read the session"
    assert "Secure" in raw, "the cookie must not travel over plaintext HTTP"
    assert "samesite=lax" in raw.lower(), "CSRF protection for every mutating route"


def test_a_forged_cookie_is_rejected(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "obviously-not-signed")
    assert client.get("/api/portfolio").status_code == 401


def test_a_cookie_signed_with_another_secret_is_rejected() -> None:
    other = SessionManager("a-different-secret", PASSPHRASE).issue()
    app.state.sessions = SessionManager(SECRET, PASSPHRASE)
    app.state.login_limiter = RateLimiter(capacity=50, per_seconds=300.0)
    client = https_client()
    client.cookies.set(COOKIE_NAME, other)
    assert client.get("/api/portfolio").status_code == 401
    app.state.sessions = SessionManager(None, None)


def test_an_expired_session_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.auth as mod

    monkeypatch.setattr(mod, "SESSION_MAX_AGE", -1)
    manager = SessionManager(SECRET, PASSPHRASE)
    assert manager.valid(manager.issue()) is False


# --- the passphrase ---------------------------------------------------------


def test_the_wrong_passphrase_is_a_401(client: TestClient) -> None:
    assert login(client, "nope").status_code == 401


def test_the_error_does_not_reveal_whether_auth_is_configured(client: TestClient) -> None:
    detail = login(client, "nope").json()["detail"]
    assert detail == "Incorrect passphrase."
    assert PASSPHRASE not in detail


def test_login_is_rate_limited(configured) -> None:
    """§11: without this a passphrase is a few hours of guessing."""
    app.state.login_limiter = RateLimiter(capacity=3, per_seconds=300.0)
    client = https_client()

    codes = [login(client, "wrong").status_code for _ in range(6)]
    assert codes.count(429) == 3, f"expected 3 allowed then 3 limited, got {codes}"
    assert "Retry-After" in login(client, "wrong").headers


def test_the_passphrase_check_is_constant_time() -> None:
    """Uses secrets.compare_digest, not ==."""
    import inspect

    source = inspect.getsource(SessionManager.verify_passphrase)
    assert "compare_digest" in source


# --- the websocket ----------------------------------------------------------


def test_the_price_socket_rejects_a_stranger(client: TestClient) -> None:
    """Otherwise the feed is the one hole in a closed API."""
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/prices") as sock:
        sock.receive_json()


def test_the_price_socket_accepts_a_session(client: TestClient) -> None:
    from app.services.price_hub import PriceHub
    from tests.test_price_hub import FakeProvider

    hub = PriceHub(FakeProvider())
    hub._running = True
    app.state.hub = hub
    login(client)
    # Starlette's TestClient does NOT apply its cookie jar to websocket_connect,
    # so the header goes on by hand. Browsers do send cookies on a same-origin
    # WS handshake — verified against production — so this is a test-harness
    # limitation, not a gap in the app.
    token = client.cookies.get(COOKIE_NAME)
    try:
        with client.websocket_connect(
            "/ws/prices", headers={"Cookie": f"{COOKIE_NAME}={token}"}
        ) as sock:
            assert sock.receive_json()["type"] == "status"
    finally:
        app.state.hub = None


# --- unconfigured -----------------------------------------------------------


def test_an_unconfigured_app_stays_open_but_says_so() -> None:
    """A half-configured deploy should be reachable so you can fix it. main.py
    logs a warning at boot."""
    app.state.sessions = SessionManager(None, None)
    client = https_client()
    # Not 200 — there is no DB configured in this fixture — but crucially not
    # 401 either: auth is not standing in the way.
    assert client.get("/api/portfolio").status_code != 401
    assert client.get("/api/auth/session").json() == {"authenticated": False, "configured": False}


def test_login_is_503_when_no_passphrase_is_set() -> None:
    app.state.sessions = SessionManager(None, None)
    app.state.login_limiter = RateLimiter(capacity=50, per_seconds=300.0)
    assert https_client().post("/api/auth/login", json={"passphrase": "x"}).status_code == 503


def test_session_endpoint_reports_status(client: TestClient) -> None:
    assert client.get("/api/auth/session").json() == {"authenticated": False, "configured": True}
    login(client)
    assert client.get("/api/auth/session").json()["authenticated"] is True
