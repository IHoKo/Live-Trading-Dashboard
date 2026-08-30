"""/ws/prices — browser fan-out socket. plan.md §6, §8.3, §9.4.

Message shapes are exactly §6:

    client -> {"type": "subscribe",   "symbols": ["AAPL", "NVDA"]}
              {"type": "unsubscribe", "symbols": ["NVDA"]}
    server -> {"type": "tick",   "s": "AAPL", "p": 213.44, "t": 1754800000000, "dp": 0.83}
              {"type": "status", "provider": "finnhub", "state": "live"}

Ticks are pushed by the hub's flush loop, not from here — this module only owns
the connection: handshake, subscription bookkeeping, and the keepalive.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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
    hub: PriceHub | None = getattr(socket.app.state, "hub", None)
    if hub is None:
        # Policy-violation close: market data isn't configured on this instance.
        await socket.close(code=1011, reason="price hub unavailable")
        return

    await socket.accept()
    sink = _WebSocketSink(socket)
    sub_id = hub.add_sink(sink)
    ping_task = asyncio.create_task(_keepalive(sink))

    try:
        await sink.send_json(hub.status_payload())

        while True:
            message = await socket.receive_json()
            kind = message.get("type")

            if kind == "subscribe":
                symbols = _clean(message.get("symbols"))
                current = hub.subscribe(sub_id, symbols)
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

            elif kind == "pong":
                continue

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("price socket failed")
    finally:
        ping_task.cancel()
        hub.remove_sink(sub_id)


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
