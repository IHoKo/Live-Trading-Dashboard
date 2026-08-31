"""Login and logout — plan.md §6, §11."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services.auth import COOKIE_NAME, SESSION_MAX_AGE, NotConfigured, SessionManager

logger = logging.getLogger("ticker.auth.router")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    passphrase: str = Field(min_length=1, max_length=512)


def get_sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    body: LoginIn,
    sessions: Annotated[SessionManager, Depends(get_sessions)],
) -> dict:
    # §11: rate-limit the login endpoint. Without this, a passphrase is a
    # few hours of guessing on a URL anyone can find.
    limiter = request.app.state.login_limiter
    if not limiter.allow():
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Wait a moment.",
            headers={"Retry-After": str(limiter.retry_after())},
        )

    try:
        ok = sessions.verify_passphrase(body.passphrase)
    except NotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not ok:
        logger.warning(
            "failed login attempt from %s", request.client.host if request.client else "?"
        )
        # Deliberately vague: nothing here tells an attacker whether the
        # passphrase was close, or whether auth is even configured.
        raise HTTPException(status_code=401, detail="Incorrect passphrase.")

    response.set_cookie(
        COOKIE_NAME,
        sessions.issue(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"status": "ok"}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/session")
async def session(
    request: Request, sessions: Annotated[SessionManager, Depends(get_sessions)]
) -> dict:
    """Lets the SPA decide between the login screen and the dashboard without
    firing a 401 at every data endpoint on first paint."""
    return {
        "authenticated": sessions.valid(request.cookies.get(COOKIE_NAME)),
        "configured": sessions.configured,
    }
