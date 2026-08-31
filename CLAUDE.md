# CLAUDE.md — Ticker

Decisions already made. Do not re-litigate these in later sessions; if one genuinely
needs to change, say so explicitly and update this file in the same change.

The full rationale lives in `plan.md`. Section references below point there.

---

## Stack — locked (§3)

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Backend | Python 3.12 + FastAPI + Uvicorn | Native async WebSocket/SSE; the Anthropic SDK tool loop is cleanest here |
| Python deps | **uv** — `pyproject.toml` + committed `uv.lock` | Fast, lockfile-based, and the Docker layer-cache pattern in §9.1 keeps rebuilds short |
| Frontend | React 18 + Vite + TypeScript | Builds to static files served by the same FastAPI app — one image, one deploy |
| Charts | `lightweight-charts` (TradingView, Apache-2.0) | Purpose-built for candles/OHLC; far lighter than Recharts or D3 here |
| State | Zustand + TanStack Query | Zustand for the high-frequency live price map; Query for REST |
| DB | SQLite + WAL on a Fly Volume | Single user, single machine. Postgres only if this goes multi-user |
| Migrations | Alembic (or plain versioned `.sql` files) | Never hand-edit the prod schema |
| AI | Anthropic Messages API, `claude-sonnet-5` | Tool use + server-side web search in one call |
| Container | Multi-stage Dockerfile (node build → python runtime) | ~200 MB final image |

Rejected: **Next.js full-stack.** It deploys to Fly fine, but long-lived WebSocket
fan-out and a blocking tool-use loop are more awkward in Node route handlers than in
FastAPI — and the price hub is the riskiest part of this build.

Market data: **Finnhub** for quotes + trades WS, behind a `MarketDataProvider` Protocol
so swapping providers is a one-file change (§4). Free-tier terms change quietly — verify
limits and redisplay rights before making anything public.

---

## Python packaging: uv only

- Dependencies live in `backend/pyproject.toml`. Change them with `uv add` / `uv remove`,
  never by hand-editing a dependency list and hoping.
- `backend/uv.lock` is **committed** and must stay committed. It must not be added to
  `.dockerignore` — the image builds with `uv sync --frozen`, which fails hard if the
  lockfile is missing. That failure is the desired behavior: a dependency set silently
  re-resolved on the deploy machine defeats the point of locking.
- `.venv` **is** in `.dockerignore`. It is platform-specific; the image builds its own.
- **Never `pip install`. Never create a `requirements.txt`.** If you see either in a diff,
  that diff is wrong.
- Local workflow: `uv sync` to set up, `uv add <pkg>` to add, `uv run pytest` to test.
  No manual venv activation.
- In the runtime image, put `/app/.venv/bin` on `PATH` and exec `uvicorn` directly rather
  than wrapping it in `uv run`. `uv run` inserts a process between Fly's `kill_signal` and
  uvicorn, which breaks the graceful shutdown (§9.1).
- The uv version in the Dockerfile is pinned to an exact tag. `:latest` makes builds
  non-reproducible.

---

## One process, one machine (§2, §9.4)

v1 runs on exactly **one** Fly Machine. This is a correctness constraint, not a cost
optimization:

- A market-data WebSocket is stateful, and providers cap concurrent connections per key.
  Three machines means three upstream connections and three divergent in-memory caches.
- SQLite lives on a Fly Volume. Volumes are pinned to one host and cannot be
  double-mounted, so a second machine means a second, different database file.

Consequences to keep in mind, all accepted:

- `fly scale count 1`, `--ha=false`. Apps with `[mounts]` already default to one machine;
  make it explicit anyway.
- `auto_stop_machines = "off"` — the **string** `"off"`, not `false`, which is a config
  error. `fly launch` writes `"stop"` into generated configs; a stopped machine kills the
  upstream feed and the in-memory cache, and you come back to a dashboard of stale prices.
  `auto_start_machines = false` to match (Fly wants both on or both off).
- Every deploy is 10–30 s of downtime — old machine stops before the new one starts.
  Acceptable. If it ever isn't, that is the moment to move to Postgres and drop the volume.
- Run migrations in a FastAPI **lifespan startup hook**, idempotently — not in
  `release_command`. The release machine runs with no volumes attached, so a migration
  there writes to an ephemeral disk and vanishes.
- `/api/health` must be exempt from auth and return 200 before the feed is up. Report feed
  state in the body, never via the status code — otherwise the health check 401s, the
  machine is marked unhealthy, and `fly deploy` rolls back with a confusing error while the
  app is actually running fine.

The scale-out path, when it comes, is a separate `market-data` app publishing to Redis
pub/sub with web machines subscribing. Not v1.

---

## The model never writes to the database (§7.2)

Chat tools split in two:

- **Read tools** (`get_quote`, `get_candles`, `get_portfolio`, `portfolio_performance`,
  `get_transactions`, `web_search`) execute immediately.
- **Write tools are `propose_*` only** (`propose_add_shares`, `propose_remove_shares`).
  They create a pending action with a UUID and return the resolved details as the tool
  result. They do not touch `transactions`.

The flow: propose → server validates (symbol resolves, quantity > 0, sells ≤ shares held)
→ UI renders a confirm card with Confirm / Edit / Cancel → `POST /api/chat/confirm` writes
the transaction, rebuilds lots, and broadcasts the portfolio to all open tabs. Pending
actions expire after 5 minutes.

This is not distrust of the model. It is that "sell everything" typed at 2am should hit a
confirm dialog, the same as any other destructive action. Do not add a tool that writes
directly, and do not add an "auto-confirm" or "skip confirmation" path.

Every mutation originating from chat carries an idempotency key (`idempotency_keys`
table). A retried tool call must not double-book a purchase.

---

## Transactions are the source of truth (§5)

- `transactions` is **append-only**. Never edit a row. A correction is a new row, or a
  delete that reverses and rebuilds.
- `lots` and `realized_pnl` are **derived** and fully rebuildable from `transactions`
  alone. There must always be a working rebuild-from-scratch function.
- **Positions are computed, never stored** — one query over `lots` grouped by symbol.
  There is deliberately no mutable `holdings.shares` column: average cost silently loses
  information the moment you sell part of a position.
- Cost basis is **FIFO** by default (configurable to average-cost). A SELL consumes the
  oldest open lots and writes a `realized_pnl` row.
- Money is `REAL` for v1: round to 4 dp on write, format with `Decimal` at the
  presentation layer. If this ever handles real accounting, switch to integer minor units.
- The lot engine is the part that must be unit-tested hard, and tests come first
  (`backend/tests/test_lots.py`): partial sells, sells crossing multiple lots, oversell
  rejection, delete-and-rebuild consistency, and a hand-computed expected P/L over a seeded
  transaction set.

---

## Never recommend trades (§11, §7.3)

This app **reports data and answers questions**. It must never be built or prompted to
recommend trades.

- The chat system prompt forbids buy/sell recommendations, price targets, and predictions.
  It describes what happened and what the data shows. Trades are the user's call.
- Prefer tools over model memory: prices, holdings, and news all come from tools.
- When no clear cause for a move is found, say so plainly rather than inventing a
  narrative. Cite sources for anything from the web.
- Every AI response ships with a disclaimer; there is also a persistent footer disclaimer.
- Label data honestly in the UI: `LIVE` / `DELAYED` / `STALE 4m` / `CLOSED`. Never render a
  stale number as if it were live.

---

## API keys are server-side only (§11)

- `ANTHROPIC_API_KEY`, `FINNHUB_API_KEY`, `SESSION_SECRET`, `APP_PASSPHRASE` are read
  server-side via pydantic-settings and **never** reach the browser bundle.
- No key may appear in any `VITE_*` variable, in frontend source, or in a client-side
  fetch — **not even "just for local dev."** Vite inlines `VITE_*` into the built JS.
- All provider and Anthropic calls go through the backend. In production, secrets come from
  `fly secrets set`; locally from `.env`, which is gitignored and dockerignored.
- Auth is a passphrase → signed HttpOnly, `Secure`, `SameSite=Lax` session cookie, with a
  rate limit on the login endpoint. This is a single-user app on the public internet;
  assume it will be found.
- Rate-limit `/api/chat/stream` (token bucket, ~20 messages/hour) so a stuck retry loop
  cannot run up an API bill.
- Pydantic validation on every input. Symbols normalized and validated against the
  provider's search before storage.

---

## Repo layout (§14)

```
ticker/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app, static mount, routers
│   │   ├── config.py            # env via pydantic-settings
│   │   ├── deps.py              # auth, db session
│   │   ├── db/                  # schema.sql, migrations, connection
│   │   ├── providers/           # base.py, finnhub.py
│   │   ├── services/
│   │   │   ├── price_hub.py     # upstream WS, cache, fan-out
│   │   │   ├── portfolio.py     # lot engine, P/L
│   │   │   └── chat.py          # Anthropic loop, tool dispatch
│   │   ├── routers/             # quotes, portfolio, chat, ws, auth
│   │   └── models.py
│   ├── tests/                   # test_lots.py first
│   ├── pyproject.toml           # deps live here; uv add to change them
│   └── uv.lock                  # committed
├── frontend/
│   └── src/
│       ├── components/          # TickerTape, PositionsTable, Chart, Chat, ConfirmCard
│       ├── hooks/               # usePriceSocket, usePortfolio, useChat
│       ├── store/               # prices.ts (zustand)
│       └── styles/tokens.css    # the §8.2 palette and type scale
├── Dockerfile
├── fly.toml
└── plan.md
```

New code goes in one of these places. Don't invent a parallel structure.

---

## Verified facts that override the plan

**The Anthropic API shapes in §7.1 and §7.4 are stale.** Verified against the live
API on 2026-08-31 with the project's own key:

- `web_search_20260209` is the current server-tool version. §7.1's
  `web_search_20250305` still works but is superseded.
- `thinking: {type: "enabled", budget_tokens: N}` is **rejected with a 400** on
  `claude-sonnet-5` — "thinking.type.enabled is not supported for this model".
  Adaptive thinking replaced fixed budgets; omit the parameter or pass
  `{type: "adaptive"}`.
- Assistant prefill is removed on this model family; use the system prompt to
  shape output instead.
- Model stays `claude-sonnet-5` per §3/§7.4, configurable via `ANTHROPIC_MODEL`.

Re-verify before changing the chat call; this drifts faster than the rest of the plan.

**Finnhub's free tier does not include `/stock/candle`.** Confirmed in production
2026-08-30: the same key that serves `/quote` and `/search` gets **HTTP 403** on
candles.

**Resolved in Phase 4 by splitting providers, not by paying.** `CompositeProvider`
routes quotes, search and the trade stream to Finnhub, and candles to Twelve Data
(free tier: 800 credits/day, 8/min, full daily history — versus Alpha Vantage's
25 requests/day, which six range buttons would exhaust before lunch). This is the
one-file swap §4's interface was designed for.

- Charts and portfolio history need `TWELVEDATA_API_KEY`. Without it everything
  else still works and `/api/candles` returns a 502 naming the fix.
- Paying for a Finnhub tier remains a valid alternative and needs no code change —
  point `CompositeProvider(candles_from=...)` back at the Finnhub instance.
- Verify both providers' terms before relying on them. §4 said free tiers change
  quietly and it was right about Finnhub.

**Polling cannot literally be "every 15s for the whole watchlist" (§4 rung 2).**
The free tier allows 60 calls/minute, so N symbols every 15s breaks the limit at
N > 15. `PriceHub` spaces upstream REST calls to a budget (50/min) and lets a poll
cycle take longer than its nominal interval when the watchlist is large. The §4
interval is a target, not a guarantee. Polling also backs off to 60s when the market
is closed, because closing prices do not move.

## Fetch policy: live when open, manual when closed

Decided 2026-08-30, a deliberate narrowing of §4's fallback ladder. Do not "restore"
continuous polling as if it were a regression.

- **Market open** — the upstream trade socket streams normally (rung 1), and REST
  polling still covers a *broken socket during the session* (rung 2). Unchanged.
- **Market closed** — no background fetching at all. Feed state is `idle`, a fifth
  state beyond §6's `live | polling | down`. The last price is the closing price;
  re-reading it on a timer spends quota to learn nothing.
- **On demand** — `PriceHub.refresh()`, reached by `{"type": "refresh"}` on the price
  socket and by the REFRESH button on the tape. Also runs once per page load for
  symbols with no cached price, so a fresh tab paints numbers instead of dashes.
- Concurrent refreshes collapse to one upstream fetch, so button spam costs nothing.

Markets are closed roughly 75% of the week, and this is a single-user app on a
60 calls/min free tier. The old behaviour spent ~9,000 calls/day re-reading static
closing prices; it now spends a handful per page load and per click.

## Database decisions (Phase 3)

§3 allows "Alembic (or plain versioned `.sql` files)". The choice made was:

- **Plain versioned `.sql` migrations** in `app/db/migrations/`, applied in filename
  order at boot and recorded in `schema_migrations`. Do not add Alembic — the
  `alembic` dependency in `pyproject.toml` is now unused and can go.
- Every migration is written idempotent (`CREATE TABLE IF NOT EXISTS`) because it
  runs on every boot, forever (§9.4).
- `migrate()` deliberately does **not** wrap `executescript` in a transaction:
  `sqlite3.executescript` issues an implicit COMMIT before running, which tears
  down the surrounding transaction and then fails on the way out. Idempotent DDL
  plus recording the filename only after success makes a retry safe.
- **The lot engine is synchronous.** Callers wrap a unit of work in
  `asyncio.to_thread`: one thread hop per operation, and the engine stays a plain
  function the tests can drive directly. That testability is the point — §11 asks
  for this to be tested harder than anything else in the app.
- One process-wide connection, WAL, `foreign_keys=ON`, `busy_timeout=5000`. One
  machine, one writer.
- Deleting a transaction clears the derived tables **before** deleting the row,
  because `lots.tx_id` and `realized_pnl.sell_tx_id` are real foreign keys.
- **There is no edit endpoint, deliberately.** `transactions` is append-only; a
  correction is a delete plus a new row. Do not add `PUT /api/transactions/{id}`.

## Auth and ops (Phase 6)

- **Auth is a signed, timestamped cookie, not a session table.** One user, one
  machine; a server-side store would add a write path and a migration for nothing.
  `HttpOnly` + `Secure` + `SameSite=Lax`; the last of those is the CSRF protection
  for every mutating route, so do not relax it.
- `require_session` is attached to the **`api` router only**. `/api/health` and the
  SPA catch-all stay public — the health check must never 401 (§9.2), and a
  logged-out browser must still be served the login page.
- **The price socket checks the same cookie.** The handshake carries it; without
  that check the feed would be the one hole in a closed API.
- With no `APP_PASSPHRASE`/`SESSION_SECRET` the app stays **open** and logs a
  warning, rather than bricking. A half-configured deploy should be reachable so
  you can fix it. `require_session` does *not* use a defensive `getattr` for the
  session manager: a fallback would let a boot failure silently unauthenticate
  everything.
- **Logging is configured by us, not uvicorn.** Uvicorn configures only its own
  loggers, so every `logger.info` in this app was discarded in the container for
  three phases — the boot migration ran invisibly. `app/logging_config.py` routes
  everything to stdout as JSON lines.
- **The nightly backup uses `Connection.backup()`, never a file copy.** With WAL,
  copying the `.db` without its `-wal` yields a torn database that looks fine
  until you need it. It writes to `.partial` and renames, so an interrupted run
  cannot replace a good backup. Same disk, so it is a second line, not an offsite
  backup: `fly ssh sftp get /data/ticker.backup.db`.

## Testing gotchas worth not rediscovering

- `TestClient(app)` uses `http://`, and httpx correctly refuses to store a
  `Secure` cookie over plaintext. Auth tests must use
  `TestClient(app, base_url="https://testserver")` or every one of them fails for
  the wrong reason.
- Starlette's `TestClient` does **not** apply its cookie jar to
  `websocket_connect`. Pass `headers={"Cookie": ...}` by hand. Browsers do send
  cookies on a same-origin WS handshake, so this is a harness limitation.

## Other standing details worth not rediscovering

- **Coalesce ticks**: buffer per symbol, flush at most every 250 ms. A liquid symbol prints
  hundreds of trades per second; this single detail is the difference between a smooth
  dashboard and a locked-up tab (§6).
- **Fallback ladder** (implement all three — markets are closed most of the week): upstream
  WS → 15 s REST polling for watchlist symbols only → last known price with a
  `stale_since` timestamp, greyed in the UI (§4).
- **Serve the SPA with a catch-all route**, not just a static mount. Mount assets at
  `/assets`, register API and WS routes, then a final route returning `index.html` for
  anything unmatched — otherwise a hard refresh at `/portfolio` 404s (§9.4).
- **Idle WS through the Fly proxy** needs application-level pings: server pings every 30 s,
  client responds. Client reconnects with exponential backoff, 1 s → 30 s cap, and
  re-subscribes on open (§8.3, §9.4).
- **The volume is not backed up by default.** Nightly `sqlite3 .backup` to a second file on
  the volume, pulled down periodically. Fly snapshots are the second line, not the first.
- **Visual direction is an exchange board**, not the default finance-dashboard look
  (near-black + acid green). Desaturated gain/loss, split-flap tape, brass accent, tabular
  numerals on every column of numbers. Full palette and type scale in §8.2 — implement it
  in `frontend/src/styles/tokens.css` and read from tokens, not hardcoded hex.
- Check https://docs.claude.com for current model IDs and tool versions (e.g. the
  `web_search` tool version) before writing the Anthropic call.
