"""/ws/prices — browser fan-out socket. plan.md §6, §8.3, §9.4.

Message shapes are exactly §6:

    client -> {"type": "subscribe",   "symbols": ["AAPL", "NVDA"]}
              {"type": "unsubscribe", "symbols": ["NVDA"]}
              {"type": "refresh"}   -- fetch now; the manual path
    server -> {"type": "tick",   "s": "AAPL", "p": 213.44, "t": 1754800000000, "dp": 0.83}
              {"type": "status", "provider": "finnhub", "state": "live"}

Ticks are pushed by the hub's flush loop, not from here — this module only owns
the connection: handshake, subscription bookkeeping, and the keepalive.
"""

import asyncio
import logging
from collections.abc import Coroutine

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.auth import COOKIE_NAME
from app.services.price_hub import PriceHub

logger = logging.getLogger("ticker.ws")

router = APIRouter()

# §9.4: idle sockets through the Fly proxy get dropped without app-level
# traffic. The client answers with {"type":"pong"}.
PING_INTERVAL = 30.0

MAX_SYMBOLS_PER_SOCKET = 50


class _WebSocketSink:
    """Adapts a FastAPI WebSocket to the hub's TickSink protocol."""

    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket

    async def send_json(self, payload: dict) -> None:
        await self._socket.send_json(payload)


async def _keepalive(sink: _WebSocketSink) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL)
        await sink.send_json({"type": "ping"})


@router.websocket("/ws/prices")
async def prices(socket: WebSocket) -> None:
    # The session cookie rides the WS handshake, so the socket is authenticated
    # the same way the REST routes are. Without this the price feed would be the
    # one hole in an otherwise closed API (§11).
    sessions = socket.app.state.sessions
    if sessions.configured and not sessions.valid(socket.cookies.get(COOKIE_NAME)):
        await socket.close(code=1008, reason="not authenticated")
        return

    hub: PriceHub | None = getattr(socket.app.state, "hub", None)
    if hub is None:
        # Policy-violation close: market data isn't configured on this instance.
        await socket.close(code=1011, reason="price hub unavailable")
        return

    await socket.accept()
    sink = _WebSocketSink(socket)
    sub_id = hub.add_sink(sink)
    tasks: set[asyncio.Task] = set()
    ping_task = asyncio.create_task(_keepalive(sink))
    tasks.add(ping_task)

    try:
        await sink.send_json(hub.status_payload())

        while True:
            message = await socket.receive_json()
            kind = message.get("type")

            if kind == "subscribe":
                symbols = _clean(message.get("symbols"))
                current = hub.subscribe(sub_id, symbols)
                # Page-load fetch: anything we have no price for at all. Runs in
                # the background so a slow upstream can't stall this socket, and
                # the flush loop delivers the results as they land.
                unknown = [s for s in symbols if not hub.snapshot([s])]
                if unknown:
                    _spawn(hub.refresh(unknown), tasks)
                # Send what we already know immediately, so a new tab paints
                # prices instead of empty rows while it waits for a print.
                for tick in hub.snapshot(sorted(current)):
                    await sink.send_json(
                        {
                            "type": "tick",
                            "s": tick.symbol,
                            "p": round(tick.price, 4),
                            "t": tick.timestamp_ms,
                            "dp": round(tick.change_pct, 4)
                            if tick.change_pct is not None
                            else None,
                            "stale_since": tick.stale_since,
                        }
                    )
                await sink.send_json(hub.status_payload())

            elif kind == "unsubscribe":
                hub.unsubscribe(sub_id, _clean(message.get("symbols")))

            elif kind == "refresh":
                # The manual path. With the market closed this is the only thing
                # that moves prices; during the session it just jumps the queue.
                _spawn(hub.refresh(_clean(message.get("symbols")) or None), tasks)

            elif kind == "pong":
                continue

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("price socket failed")
    finally:
        for task in tasks:
            task.cancel()
        hub.remove_sink(sub_id)


def _spawn(coro: Coroutine, tasks: set[asyncio.Task]) -> None:
    """Fire-and-forget, but keep a reference so it isn't garbage collected
    mid-flight and so the socket's teardown can cancel it."""
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _clean(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            symbol = item.strip().upper()
            if symbol not in out:
                out.append(symbol)
    return out[:MAX_SYMBOLS_PER_SOCKET]
