"""Provider errors -> HTTP status codes.

Shared by every router that touches a provider, so an upstream failure gets the
same honest answer wherever it surfaces. AccessDenied in particular must not
collapse into a 500: on a free tier it usually means the endpoint is on a paid
plan, which is something the operator can act on.
"""

from fastapi import HTTPException

from app.providers.base import AccessDenied, ProviderError, RateLimited, SymbolNotFound


def raise_for_provider_error(exc: ProviderError) -> None:
    if isinstance(exc, SymbolNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, RateLimited):
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        raise HTTPException(
            status_code=429,
            detail="Upstream market data rate limit reached. Try again shortly.",
            headers=headers,
        ) from exc
    if isinstance(exc, AccessDenied):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=502, detail=str(exc)) from exc
