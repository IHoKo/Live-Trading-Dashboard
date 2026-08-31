"""Test fixtures.

`app.main` resolves the static directory at import time, so STATIC_DIR has to be
set before the app module is imported anywhere — hence the module-level setup
here rather than a fixture.
"""

import os
import tempfile
from pathlib import Path

_static = Path(tempfile.mkdtemp(prefix="ticker-static-"))
(_static / "assets").mkdir()
(_static / "index.html").write_text("<!doctype html><title>Ticker</title>", encoding="utf-8")
(_static / "assets" / "index-abc123.js").write_text("console.log(0)", encoding="utf-8")
(_static / "favicon.ico").write_bytes(b"\x00")

os.environ["STATIC_DIR"] = str(_static)

STATIC_DIR = _static


import pytest


@pytest.fixture(autouse=True)
def app_state():
    """Stand in for what the lifespan hook sets up.

    Auth is left *unconfigured* by default, so every existing test keeps
    exercising its own subject rather than the login flow. `test_auth.py`
    configures it explicitly.

    `require_session` deliberately does not fall back when this is missing:
    a defensive getattr would let a boot failure silently unauthenticate the
    app, and that is exactly the bug you never notice.
    """
    from app.main import app
    from app.services.auth import SessionManager
    from app.services.pending import PendingActionStore, RateLimiter

    app.state.sessions = SessionManager(None, None)
    app.state.login_limiter = RateLimiter(capacity=10, per_seconds=300.0)
    app.state.chat_limiter = RateLimiter()
    app.state.pending = PendingActionStore()
    app.state.anthropic = None
    yield
