"""The chat layer — plan.md §7. CLAUDE.md: the model never writes to the DB.

Verified against the live API on 2026-08-31, because §7.1 and this file's
training priors were both stale:

  * `web_search_20260209` is the current server-tool version. §7.1 shows
    `web_search_20250305`, which still works but is superseded.
  * `thinking: {type: "enabled", budget_tokens: N}` is **rejected with a 400**
    on claude-sonnet-5. Adaptive thinking replaced fixed budgets.

**How the propose/confirm rule is enforced.** The tool dispatcher is handed a
`PortfolioReader` — a narrow read-only facade — and never a database
connection. It cannot write to the ledger because it holds no object capable of
writing. `propose_*` puts a `PendingAction` in memory; only
`POST /api/chat/confirm` reaches the lot engine. This is structural rather than
a matter of discipline, because "sell everything" typed at 2am should hit a
confirm dialog like any other destructive action.
"""

import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anthropic

from app.providers.base import MarketDataProvider, ProviderError, SymbolNotFound
from app.services import portfolio as engine
from app.services.market_hours import market_status
from app.services.pending import PendingActionStore

logger = logging.getLogger("ticker.chat")

MAX_ITERATIONS = 6  # §7.4
MAX_HISTORY_TURNS = 20  # §7.4: cap what we resend upstream
MAX_TOKENS = 8192  # answers render in a narrow panel; §7.3 asks for brevity


# --- the read-only capability ----------------------------------------------


class PortfolioReader:
    """Everything the tool dispatcher may learn about the ledger.

    Deliberately not a `sqlite3.Connection`. Handing the dispatcher a connection
    would make the propose/confirm rule a convention; handing it this makes the
    rule structural.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def positions(self) -> list[engine.Position]:
        return engine.positions(self._conn)

    def realized_total(self) -> float:
        return engine.realized_total(self._conn)

    def shares_held(self, symbol: str) -> float:
        return engine.shares_held(self._conn, symbol)

    def transactions(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT symbol, side, quantity, price, fees, executed_at FROM transactions"
        params: list[Any] = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol.upper())
        sql += " ORDER BY executed_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self._conn.execute(sql, params)]


# --- tool definitions (§7.1) ------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_quote",
        "description": "Current price and day change for one or more symbols.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker symbols, e.g. ['AAPL', 'NVDA'].",
                }
            },
            "required": ["symbols"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_candles",
        "description": (
            "Historical OHLCV bars. Use for 'how has X performed' questions. "
            "May be unavailable on the current market-data plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "range": {"type": "string", "enum": ["1D", "5D", "1M", "6M", "1Y", "5Y"]},
            },
            "required": ["symbol", "range"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_portfolio",
        "description": (
            "The user's current positions, cost basis, and unrealized P/L. "
            "Call this first whenever the user asks about their money."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_transactions",
        "description": "The user's buy/sell history, optionally filtered by symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_add_shares",
        "description": (
            "Propose recording a purchase. Returns a pending action for the user to "
            "confirm in the UI. Does NOT execute. If price is omitted the server "
            "fills in the current quote and says so on the confirm card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "quantity": {"type": "number", "exclusiveMinimum": 0},
                "price": {"type": "number", "minimum": 0},
                "executed_at": {"type": "string", "description": "ISO8601 UTC."},
                "note": {"type": "string"},
            },
            "required": ["symbol", "quantity"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_remove_shares",
        "description": (
            "Propose recording a sale. Returns a pending action for the user to "
            "confirm in the UI. Does NOT execute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "quantity": {"type": "number", "exclusiveMinimum": 0},
                "price": {"type": "number", "minimum": 0},
                "executed_at": {"type": "string", "description": "ISO8601 UTC."},
                "note": {"type": "string"},
            },
            "required": ["symbol", "quantity"],
            "additionalProperties": False,
        },
    },
    # Server-side tool. Version verified against the live API — §7.1's
    # web_search_20250305 still works but is superseded.
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
]

WRITE_TOOLS = {"propose_add_shares", "propose_remove_shares"}
READ_TOOLS = {"get_quote", "get_candles", "get_portfolio", "get_transactions"}


def system_prompt(now: datetime | None = None) -> str:
    """§7.3. The 'never recommend' paragraph is not decorative — it is the line
    between this being a data tool and being unlicensed advice (§11)."""
    moment = now or datetime.now(UTC)
    status = market_status(moment)
    return f"""You are the analyst inside the user's portfolio dashboard. \
Today is {moment.strftime("%A %d %B %Y")}, and the US market is currently \
{"open" if status.is_open else "closed"}.

- Prefer tools over memory. Prices, holdings, and news all come from tools; you have no
  reliable knowledge of any of them.
- When the user asks about their money, call get_portfolio first — don't guess at holdings.
- For "why is X moving", call get_quote for the magnitude and web_search for the cause.
  Say plainly when no clear cause is found rather than inventing a narrative.
- Cite sources for anything from the web.
- Never recommend buying or selling, never give price targets, never predict.
  You describe what happened and what the data shows. Trades are the user's call.
- For share changes, use the propose_* tools. Confirm quantity and symbol back to the user.
  You cannot execute a trade or edit the ledger; the user confirms every change.
- Format numbers to 2 dp with the currency symbol. Be brief — this renders in a narrow panel."""


# --- tool dispatch ----------------------------------------------------------


@dataclass
class ChatDeps:
    """The dispatcher's whole world. No connection, so no writes."""

    reader: PortfolioReader
    provider: MarketDataProvider | None
    pending: PendingActionStore
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


async def dispatch(name: str, payload: dict, deps: ChatDeps) -> tuple[str, dict | None]:
    """Run one tool. Returns (result text for the model, pending action payload).

    Every failure is returned to the model as text rather than raised: a tool
    that errors should let Claude explain the problem, not kill the turn.
    """
    try:
        if name == "get_quote":
            return await _get_quote(payload, deps), None
        if name == "get_candles":
            return await _get_candles(payload, deps), None
        if name == "get_portfolio":
            return _get_portfolio(deps), None
        if name == "get_transactions":
            return _get_transactions(payload, deps), None
        if name in WRITE_TOOLS:
            return await _propose(name, payload, deps)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, never to a 500
        logger.warning("tool %s failed: %s", name, exc)
        return f"The {name} tool failed: {exc}", None
    return f"Unknown tool {name!r}.", None


async def _get_quote(payload: dict, deps: ChatDeps) -> str:
    if deps.provider is None:
        return "Market data is not configured, so live prices are unavailable."
    symbols = [s.strip().upper() for s in payload.get("symbols", []) if s.strip()][:10]
    if not symbols:
        return "No symbols given."

    lines = []
    for symbol in symbols:
        try:
            quote = await deps.provider.quote(symbol)
        except SymbolNotFound:
            lines.append(f"{symbol}: no such symbol.")
            continue
        except ProviderError as exc:
            lines.append(f"{symbol}: price unavailable ({exc}).")
            continue
        age = int(deps.now().timestamp()) - quote.as_of if quote.as_of else None
        staleness = f", as of {age // 60}m ago" if age and age > 120 else ""
        lines.append(
            f"{symbol}: {quote.price:.2f} ({quote.change_pct:+.2f}% today){staleness}"
            if quote.change_pct is not None
            else f"{symbol}: {quote.price:.2f}{staleness}"
        )
    return "\n".join(lines)


async def _get_candles(payload: dict, deps: ChatDeps) -> str:
    if deps.provider is None:
        return "Market data is not configured."
    from app.routers.quotes import candle_window

    symbol = str(payload["symbol"]).strip().upper()
    resolution, frm, to = candle_window(payload["range"])
    try:
        candles = await deps.provider.candles(symbol, resolution, frm, to)
    except ProviderError as exc:
        return (
            f"Historical bars for {symbol} are unavailable: {exc}. "
            "Say so plainly rather than estimating."
        )
    if not candles:
        return f"No bars returned for {symbol} over {payload['range']}."
    first, last = candles[0], candles[-1]
    change = (last.close - first.open) / first.open * 100 if first.open else 0.0
    return (
        f"{symbol} {payload['range']}: {len(candles)} bars, "
        f"open {first.open:.2f} -> close {last.close:.2f} ({change:+.2f}%), "
        f"high {max(c.high for c in candles):.2f}, low {min(c.low for c in candles):.2f}"
    )


def _get_portfolio(deps: ChatDeps) -> str:
    positions = deps.reader.positions()
    if not positions:
        return "The portfolio is empty — no open positions."
    lines = [
        f"{p.symbol}: {p.quantity:g} shares, average cost {p.average_cost:.2f}, "
        f"cost basis {p.cost_basis:.2f}"
        for p in positions
    ]
    lines.append(f"Realized P/L to date: {deps.reader.realized_total():.2f}")
    return "\n".join(lines)


def _get_transactions(payload: dict, deps: ChatDeps) -> str:
    rows = deps.reader.transactions(
        symbol=payload.get("symbol"), limit=min(int(payload.get("limit", 20)), 100)
    )
    if not rows:
        return "No transactions recorded."
    return "\n".join(
        f"{r['executed_at'][:10]} {r['side']} {r['quantity']:g} {r['symbol']} @ {r['price']:.2f}"
        for r in rows
    )


async def _propose(name: str, payload: dict, deps: ChatDeps) -> tuple[str, dict | None]:
    """Validate, then park a proposal in memory. Never writes to the ledger.

    §7.2 requires validation *before* a proposal is ever returned, so the user
    is never shown a confirm card for something that cannot be executed.
    """
    kind = "add" if name == "propose_add_shares" else "remove"
    symbol = str(payload["symbol"]).strip().upper()
    quantity = float(payload["quantity"])

    if quantity <= 0:
        return "Quantity must be greater than zero.", None

    if kind == "remove":
        held = deps.reader.shares_held(symbol)
        if quantity > held + 1e-9:
            return (
                (
                    f"Cannot propose selling {quantity:g} {symbol}: only {held:g} held. "
                    "Tell the user the actual holding."
                ),
                None,
            )

    price = payload.get("price")
    price_source = "user"
    if price is None:
        if deps.provider is None:
            return f"No price given for {symbol} and no live quote is available.", None
        try:
            price = (await deps.provider.quote(symbol)).price
            price_source = "quote"
        except SymbolNotFound:
            return f"{symbol} is not a symbol the market data provider knows.", None
        except ProviderError as exc:
            return f"Could not look up a price for {symbol}: {exc}", None
    elif deps.provider is not None:
        # §11: symbols are validated against the provider before storage.
        try:
            await deps.provider.quote(symbol)
        except SymbolNotFound:
            return f"{symbol} is not a symbol the market data provider knows.", None
        except ProviderError:
            pass  # a dead provider must not block recording a trade you made

    action = deps.pending.propose(
        kind=kind,
        symbol=symbol,
        quantity=quantity,
        price=round(float(price), 4),
        price_source=price_source,  # type: ignore[arg-type]
        executed_at=payload.get("executed_at") or deps.now().isoformat(timespec="seconds"),
        note=payload.get("note"),
    )
    verb = "buy" if kind == "add" else "sell"
    priced = "current price" if price_source == "quote" else "the price given"
    return (
        (
            f"Prepared a confirmation card to {verb} {quantity:g} {symbol} at "
            f"{action.price:.2f} ({priced}), total {action.quantity * action.price:.2f}. "
            "The user must confirm it; nothing has been recorded. "
            "Tell them what is on the card."
        ),
        action.to_payload(),
    )


# --- the agentic loop (§7.4) ------------------------------------------------


@dataclass
class ChatEvent:
    type: str
    data: dict


async def run_turn(
    client: anthropic.AsyncAnthropic,
    *,
    model: str,
    history: list[dict],
    deps: ChatDeps,
) -> AsyncIterator[ChatEvent]:
    """Stream one user turn: text deltas, tool activity, confirm cards.

    Yields events; the router turns them into SSE frames. History is mutated in
    place so the caller can persist the full content blocks afterwards (§7.4),
    which is what lets tool calls survive a page reload.
    """
    messages = history

    for iteration in range(MAX_ITERATIONS):
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system_prompt(deps.now()),
                tools=TOOLS,
                messages=_trim(messages),
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield ChatEvent("text", {"delta": event.delta.text})
                    elif event.type == "content_block_start":
                        # Server-side tools (web_search) run on Anthropic's side
                        # and never reach `dispatch`, so without this the panel
                        # shows no activity at all while a search is running.
                        block = event.content_block
                        if getattr(block, "type", None) == "server_tool_use":
                            yield ChatEvent(
                                "tool",
                                {
                                    "name": getattr(block, "name", "server_tool"),
                                    "status": "running",
                                    "server": True,
                                },
                            )
                response = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            yield ChatEvent("error", {"message": f"Model API error {exc.status_code}."})
            return
        except anthropic.APIConnectionError:
            yield ChatEvent("error", {"message": "Could not reach the model API."})
            return

        messages.append({"role": "assistant", "content": _blocks(response.content)})

        for citation in _citations(response.content):
            yield ChatEvent("citation", citation)

        if response.stop_reason == "pause_turn":
            # A long server-tool turn paused; resend to let it continue.
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            yield ChatEvent("done", {"stop_reason": response.stop_reason or "end_turn"})
            return

        results = []
        for block in tool_uses:
            yield ChatEvent("tool", {"name": block.name, "status": "running"})
            text, pending = await dispatch(block.name, dict(block.input), deps)
            if pending is not None:
                yield ChatEvent("pending_action", pending)
            yield ChatEvent("tool", {"name": block.name, "status": "done"})
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text})

        # All results in ONE user message — splitting them teaches the model to
        # stop making parallel calls.
        messages.append({"role": "user", "content": results})

    yield ChatEvent("done", {"stop_reason": "max_iterations"})


def _trim(messages: list[dict]) -> list[dict]:
    """§7.4: cap history sent upstream at ~20 turns, without splitting a
    tool_use from its tool_result."""
    if len(messages) <= MAX_HISTORY_TURNS:
        return messages
    trimmed = messages[-MAX_HISTORY_TURNS:]
    while trimmed and _starts_with_tool_result(trimmed[0]):
        trimmed = trimmed[1:]
    return trimmed


def _starts_with_tool_result(message: dict) -> bool:
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and isinstance(content[0], dict)
        and content[0].get("type") == "tool_result"
    )


def _blocks(content: list) -> list[dict]:
    """SDK blocks -> plain dicts, so history round-trips through SQLite."""
    return [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in content]


def _citations(content: list) -> list[dict]:
    """Web search citations, for rendering as links (§10 Phase 5)."""
    out: list[dict] = []
    for block in content:
        for citation in getattr(block, "citations", None) or []:
            url = getattr(citation, "url", None)
            if url:
                out.append({"url": url, "title": getattr(citation, "title", None) or url})
    # De-duplicate, preserving order.
    seen: set[str] = set()
    return [c for c in out if not (c["url"] in seen or seen.add(c["url"]))]


def serialise(messages: list[dict]) -> str:
    return json.dumps(messages)
