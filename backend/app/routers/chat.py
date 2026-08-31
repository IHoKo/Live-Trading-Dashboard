"""Chat endpoints — plan.md §6, §7, §11.

    POST   /api/chat/stream   -> SSE: text deltas, tool events, confirm cards
    POST   /api/chat/confirm  -> {action_id, approved} executes a proposal
    GET    /api/chat/history
    DELETE /api/chat/history

`/api/chat/confirm` is the **only** route in the app that turns a model
proposal into a transaction. Nothing on the streaming path can write to the
ledger — see `app/services/chat.py` for how that is enforced.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import get_db, get_provider_optional
from app.providers.base import MarketDataProvider
from app.services import chat as chat_service
from app.services import portfolio as engine

logger = logging.getLogger("ticker.chat.router")

router = APIRouter(prefix="/chat", tags=["chat"])

MAX_MESSAGE_CHARS = 4000


class ChatMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ConfirmIn(BaseModel):
    action_id: str = Field(min_length=1, max_length=64)
    approved: bool


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _load_history(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT role, content FROM chat_messages ORDER BY id").fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            out.append({"role": row["role"], "content": json.loads(row["content"])})
        except ValueError:
            continue  # a corrupt row must not break the whole conversation
    return out


def _save(db: sqlite3.Connection, role: str, content: object) -> None:
    db.execute(
        "INSERT INTO chat_messages (role, content) VALUES (?, ?)",
        (role, json.dumps(content)),
    )


@router.post("/stream")
async def stream(
    request: Request,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    provider: Annotated[MarketDataProvider | None, Depends(get_provider_optional)],
    body: ChatMessageIn,
) -> StreamingResponse:
    settings = get_settings()
    client = getattr(request.app.state, "anthropic", None)
    if client is None:
        raise HTTPException(
            status_code=503, detail="Chat is unavailable: ANTHROPIC_API_KEY is not configured."
        )

    # §11: a stuck retry loop must not run up an API bill.
    limiter = request.app.state.chat_limiter
    if not limiter.allow():
        raise HTTPException(
            status_code=429,
            detail="Chat rate limit reached. Try again shortly.",
            headers={"Retry-After": str(limiter.retry_after())},
        )

    pending = request.app.state.pending
    deps = chat_service.ChatDeps(
        reader=chat_service.PortfolioReader(db),
        provider=provider,
        pending=pending,
    )

    history = await asyncio.to_thread(_load_history, db)
    history.append({"role": "user", "content": body.message})
    await asyncio.to_thread(_save, db, "user", body.message)
    before = len(history)

    async def frames() -> AsyncIterator[str]:
        try:
            async for event in chat_service.run_turn(
                client, model=settings.anthropic_model, history=history, deps=deps
            ):
                yield _sse(event.type, event.data)
        except Exception:
            logger.exception("chat turn failed")
            yield _sse("error", {"message": "The assistant hit an unexpected error."})
        finally:
            # Persist the full content blocks, including tool_use/tool_result,
            # so history survives a reload (§7.4).
            for message in history[before:]:
                await asyncio.to_thread(_save, db, message["role"], message["content"])

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/confirm")
async def confirm(
    request: Request,
    db: Annotated[sqlite3.Connection, Depends(get_db)],
    body: ConfirmIn,
) -> dict:
    """The only path from a model proposal to a written transaction (§7.2)."""
    pending = request.app.state.pending

    if not body.approved:
        pending.discard(body.action_id)
        return {"status": "cancelled"}

    action = pending.take(body.action_id)
    if action is None:
        # Either it expired after 5 minutes, or this is a replayed confirm.
        # Both must be refused: taking it is single-use, so a retried request
        # cannot double-book a purchase (§11 idempotency).
        raise HTTPException(
            status_code=410,
            detail="That confirmation has expired or was already used. Ask again to redo it.",
        )

    new = engine.NewTransaction(
        symbol=action.symbol,
        side="BUY" if action.kind == "add" else "SELL",
        quantity=action.quantity,
        price=action.price,
        executed_at=action.executed_at or datetime.now(UTC).isoformat(timespec="seconds"),
        note=action.note,
        source="chat",
    )
    try:
        tx_id = await asyncio.to_thread(engine.record_transaction, db, new)
    except engine.OversellError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except engine.PortfolioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Record the outcome in the conversation so a reload shows what happened.
    await asyncio.to_thread(
        _save,
        db,
        "assistant",
        [
            {
                "type": "text",
                "text": (f"Recorded: {new.side} {new.quantity:g} {new.symbol} at {new.price:.2f}."),
            }
        ],
    )
    return {"status": "recorded", "transaction_id": tx_id}


@router.get("/history")
async def history(db: Annotated[sqlite3.Connection, Depends(get_db)]) -> dict:
    rows = await asyncio.to_thread(
        lambda: db.execute(
            "SELECT id, role, content, created_at FROM chat_messages ORDER BY id"
        ).fetchall()
    )
    messages = []
    for row in rows:
        try:
            content = json.loads(row["content"])
        except ValueError:
            continue
        messages.append(
            {
                "id": row["id"],
                "role": row["role"],
                "content": content,
                "created_at": row["created_at"],
            }
        )
    return {"messages": messages}


@router.delete("/history", status_code=204)
async def clear_history(db: Annotated[sqlite3.Connection, Depends(get_db)]) -> None:
    await asyncio.to_thread(lambda: db.execute("DELETE FROM chat_messages"))
