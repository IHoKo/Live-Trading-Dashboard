# Ticker — Live Stock Dashboard, Portfolio & AI Analyst

A single-container web app: live price streaming, a personal portfolio you can edit by talking to it, and a Claude-powered chat that answers questions using both your holdings and current information from the web. Deployed on Fly.io.

> **Not investment advice.** This app reports data and answers questions. It must never be built or prompted to recommend trades. Every AI response ships with a disclaimer, and the chat system prompt forbids buy/sell recommendations. See §11.

---

## 1. Scope

**In scope (v1)**
- Live/near-live quotes for a watchlist, streamed to the browser over WebSocket
- Portfolio: positions, cost basis, unrealized P/L, day change, allocation
- Chat that can answer "how is my portfolio doing", "why is NVDA down today", "what did AAPL report"
- Chat-driven mutations: "add 10 shares of MSFT at 412.50", "sell 5 AAPL" — with an explicit confirm step
- Manual add/edit/remove in the UI (chat is a convenience, not the only path)
- Single-user auth (passphrase), everything server-side

**Explicitly out of scope (v1)**
- Real brokerage connections or order execution
- Options, crypto, FX, multi-currency
- Multi-user accounts and sharing
- Backtesting

**Definition of done**: deployed on Fly, prices tick without refresh, portfolio survives a machine restart, chat can read the portfolio and the web, and a mutation through chat is reflected in the UI within a second.

---

## 2. Architecture

```
                    ┌───────────────────────── Browser (SPA) ─────────────────────────┐
                    │  Watchlist   Portfolio   Chart   Chat                            │
                    └───┬──────────────────┬──────────────────┬────────────────────────┘
                        │ WS /ws/prices    │ REST /api/*      │ SSE /api/chat/stream
                        ▼                  ▼                  ▼
        ┌───────────────────────────── FastAPI (one Fly Machine) ─────────────────────┐
        │                                                                             │
        │  PriceHub ──── single upstream WS ────────────────► market data provider     │
        │    │  in-memory last-price cache, fan-out to N browser sockets               │
        │    │                                                                         │
        │  REST layer ── quotes / candles / portfolio CRUD                             │
        │    │                                                                         │
        │  ChatService ── Anthropic Messages API (tool use + server-side web search)    │
        │       tools: get_quote, get_candles, get_portfolio, portfolio_performance,    │
        │              propose_add_shares, propose_remove_shares                        │
        │                                                                             │
        │  SQLite (WAL) on Fly Volume /data ── holdings, transactions, watchlist, chat  │
        └─────────────────────────────────────────────────────────────────────────────┘
```

**One process, one machine.** This matters more than it looks: a market-data WebSocket is stateful and most providers cap concurrent connections per key. If Fly scales you to three machines you get three upstream connections and three divergent caches. v1 pins to a single machine (§9.4). The scale-out path is a separate `market-data` app publishing to Redis pub/sub, with web machines subscribing.

---

## 3. Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12 + FastAPI + Uvicorn | Native async WS/SSE, and the Anthropic SDK's tool loop is cleanest here |
| Python deps | **uv** (`pyproject.toml` + committed `uv.lock`) | Fast, lockfile-based, and the Docker layer caching pattern in §9.1 keeps rebuilds short |
| Frontend | React 18 + Vite + TypeScript | Built to static files, served by the same FastAPI app — one image, one deploy |
| Charts | `lightweight-charts` (TradingView, Apache-2.0) | Purpose-built for candles/OHLC; far lighter than Recharts or D3 for this |
| State | Zustand + TanStack Query | Zustand for the live price map (high-frequency), Query for REST |
| DB | SQLite + WAL on a Fly Volume | Single user, single machine. Migrate to Fly Postgres only if you go multi-user |
| Migrations | Alembic (or plain versioned `.sql` files) | Don't hand-edit prod schema |
| AI | Anthropic Messages API, `claude-sonnet-5` | Tool use + server-side web search in one call |
| Container | Multi-stage Dockerfile (node build → python runtime) | ~200 MB final image |

**Alternative worth knowing**: Next.js full-stack collapses front and back into one codebase and deploys to Fly fine. Chosen against because long-lived WebSocket fan-out and a blocking tool-use loop are more awkward in Node route handlers than in FastAPI, and the price hub is the riskiest part of this build.

---

## 4. Market data provider

Free tiers here change often and quietly. **Verify current limits and terms before committing** — including whether redisplaying quotes in a personal app is permitted.

| Provider | Real-time WS | Free-tier shape | Notes |
|---|---|---|---|
| **Finnhub** | Yes, US equities | Generous req/min, WS included | Best free real-time option; recommended for v1 |
| Polygon.io | Yes (paid tiers) | Free tier is end-of-day / delayed | Best data quality; the paid starter tier is the natural upgrade |
| Twelve Data | Yes (paid) | Small daily req budget | Clean REST, good fundamentals coverage |
| Alpha Vantage | No | Very low daily cap | Fine as a fundamentals fallback, not for live |
| Yahoo via `yfinance` | No (polling) | Unofficial, unstable, ToS-gray | Prototype only. Do not ship |

**Decision: Finnhub for quotes + trades WS, with a polling fallback.** Design the provider behind an interface so swapping is a one-file change:

```python
class MarketDataProvider(Protocol):
    async def quote(self, symbol: str) -> Quote: ...
    async def candles(self, symbol: str, resolution: str, frm: int, to: int) -> list[Candle]: ...
    async def search(self, query: str) -> list[SymbolMatch]: ...
    def stream(self, symbols: set[str]) -> AsyncIterator[Trade]: ...
```

**Fallback ladder** (implement all three; markets are closed most of the week):
1. Upstream WS trade prints → push immediately
2. If WS is down or the market is closed → poll REST quote every 15 s for watchlist symbols only
3. If the provider errors → serve last known price with a `stale_since` timestamp, and show it greyed in the UI

Label the data honestly in the UI: `LIVE` / `DELAYED` / `STALE 4m` / `CLOSED`. Never render a stale number as if it were live.

---

## 5. Data model

```sql
-- Every buy and sell. This table is the source of truth; never edit rows, only append.
CREATE TABLE transactions (
  id            INTEGER PRIMARY KEY,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  quantity      REAL NOT NULL CHECK (quantity > 0),
  price         REAL NOT NULL CHECK (price >= 0),
  fees          REAL NOT NULL DEFAULT 0,
  executed_at   TEXT NOT NULL,              -- ISO8601 UTC
  note          TEXT,
  source        TEXT NOT NULL DEFAULT 'ui', -- 'ui' | 'chat'
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_tx_symbol ON transactions(symbol, executed_at);

-- Open tax lots, derived from transactions. Rebuildable from scratch.
CREATE TABLE lots (
  id            INTEGER PRIMARY KEY,
  symbol        TEXT NOT NULL,
  quantity_open REAL NOT NULL,
  cost_per_share REAL NOT NULL,
  opened_at     TEXT NOT NULL,
  tx_id         INTEGER NOT NULL REFERENCES transactions(id)
);

CREATE TABLE realized_pnl (
  id            INTEGER PRIMARY KEY,
  symbol        TEXT NOT NULL,
  quantity      REAL NOT NULL,
  proceeds      REAL NOT NULL,
  cost_basis    REAL NOT NULL,
  closed_at     TEXT NOT NULL,
  sell_tx_id    INTEGER NOT NULL REFERENCES transactions(id)
);

CREATE TABLE watchlist (
  symbol     TEXT PRIMARY KEY,
  added_at   TEXT NOT NULL DEFAULT (datetime('now')),
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE chat_messages (
  id         INTEGER PRIMARY KEY,
  role       TEXT NOT NULL,   -- user | assistant
  content    TEXT NOT NULL,   -- JSON content blocks, so tool calls survive reloads
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Guards against a retried chat tool call double-booking a purchase.
CREATE TABLE idempotency_keys (
  key        TEXT PRIMARY KEY,
  tx_id      INTEGER REFERENCES transactions(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**Cost basis**: FIFO by default, configurable to average-cost. A SELL consumes the oldest open lots and writes a `realized_pnl` row. Positions are computed, never stored — one query over `lots` grouped by symbol. This is why the design uses lots instead of a mutable `holdings.shares` column: average cost silently loses information the moment you sell part of a position.

**Money**: store as REAL for v1 simplicity but round to 4 dp on write, and format with `Decimal` at the presentation layer. If this ever handles real accounting, switch to integer minor units.

---

## 6. Backend API

```
GET    /api/health                      → {status, provider, ws_connected, market_open}
POST   /api/auth/login                  → sets HttpOnly session cookie
POST   /api/auth/logout

GET    /api/quotes?symbols=AAPL,MSFT     → last price, change, change_pct, staleness
GET    /api/candles/{symbol}?range=1D    → OHLCV for the chart (1D/5D/1M/6M/1Y/5Y)
GET    /api/search?q=apple               → symbol lookup for the add-ticker box

GET    /api/portfolio                   → positions + totals + allocation
GET    /api/portfolio/performance?range= → time series of portfolio value
GET    /api/transactions                → paginated history
POST   /api/transactions                → {symbol, side, quantity, price, executed_at, fees}
DELETE /api/transactions/{id}           → reverses and rebuilds lots

GET    /api/watchlist  POST  /api/watchlist  DELETE /api/watchlist/{symbol}

POST   /api/chat/stream                 → SSE: text deltas, tool events, confirm cards
POST   /api/chat/confirm                → {action_id, approved: bool} executes a proposed mutation
DELETE /api/chat/history

WS     /ws/prices                       → client sends {subscribe:[...]}, server pushes ticks
```

**WS message shapes**
```jsonc
// client → server
{"type": "subscribe",   "symbols": ["AAPL", "NVDA"]}
{"type": "unsubscribe", "symbols": ["NVDA"]}
// server → client
{"type": "tick",   "s": "AAPL", "p": 213.44, "t": 1754800000000, "dp": 0.83}
{"type": "status", "provider": "finnhub", "state": "live" | "polling" | "down"}
```

Coalesce ticks: buffer per symbol and flush at most every 250 ms. A liquid symbol prints hundreds of trades per second and the browser cannot usefully render that — this single detail is the difference between a smooth dashboard and a locked-up tab.

---

## 7. The chat layer

### 7.1 Tools

Read tools execute immediately. Write tools **propose only** — they return a pending action that the user confirms in the UI.

```python
TOOLS = [
  {"name": "get_quote",             "description": "Current price and day change for one or more symbols."},
  {"name": "get_candles",           "description": "Historical OHLCV. Use for 'how has X performed' questions."},
  {"name": "get_portfolio",         "description": "The user's current positions, cost basis, and unrealized P/L."},
  {"name": "portfolio_performance", "description": "Portfolio value over a time range, plus best/worst contributors."},
  {"name": "get_transactions",      "description": "The user's buy/sell history, optionally filtered by symbol."},
  {"name": "propose_add_shares",    "description": "Propose recording a purchase. Returns a pending action for user confirmation. Does NOT execute."},
  {"name": "propose_remove_shares", "description": "Propose recording a sale. Returns a pending action for user confirmation. Does NOT execute."},
  {"type": "web_search_20250305", "name": "web_search"},   # verify current tool version in the docs
]
```

`propose_add_shares` input schema: `{symbol, quantity, price?, executed_at?, note?}`. If `price` is omitted, the server fills it with the current quote and says so in the confirm card. Server-side validation before a proposal is ever returned: symbol resolves, quantity > 0, and for sells, quantity ≤ shares held.

### 7.2 Confirmation flow

1. User: "add 10 shares of MSFT"
2. Claude calls `propose_add_shares` → server creates `pending_action` with a UUID, returns the resolved details as the tool result
3. Claude narrates: "Ready to record 10 MSFT at $412.50 (current price), total $4,125."
4. UI renders a confirm card with **Confirm** / **Edit** / **Cancel**
5. Confirm → `POST /api/chat/confirm` → transaction written, lots rebuilt, portfolio broadcast to all open tabs
6. Pending actions expire after 5 minutes

Never let the model write to the database directly. It's not about the model misbehaving — it's that "sell everything" typed at 2am should hit a confirm dialog, same as any other destructive action.

### 7.3 System prompt sketch

```
You are the analyst inside <user>'s portfolio dashboard. Today is {date}, market is {open|closed}.

- Prefer tools over memory. Prices, holdings, and news all come from tools; you have no
  reliable knowledge of any of them.
- When the user asks about their money, call get_portfolio first — don't guess at holdings.
- For "why is X moving", call get_quote for the magnitude and web_search for the cause.
  Say plainly when no clear cause is found rather than inventing a narrative.
- Cite sources for anything from the web.
- Never recommend buying or selling, never give price targets, never predict.
  You describe what happened and what the data shows. Trades are the user's call.
- For share changes, use the propose_* tools. Confirm quantity and symbol back to the user.
- Format numbers to 2 dp with the currency symbol. Be brief — this renders in a narrow panel.
```

### 7.4 Loop and streaming

Standard agentic loop, max 6 iterations: send messages → if `stop_reason == "tool_use"`, run tools (in parallel where independent), append results, resend. Stream to the browser with SSE so the user sees "Checking your portfolio…" while tools run. Persist full content blocks (including `tool_use`/`tool_result`) to `chat_messages` so history survives a reload. Cap history sent upstream at ~20 turns.

Model: `claude-sonnet-5` for the balance of quality and latency; `claude-haiku-4-5-20251001` is worth trying for simple quote lookups if you add a router later. Check https://docs.claude.com for current model IDs and tool versions before you write the call.

---

## 8. Frontend

### 8.1 Layout

```
┌────────────────────────────────────────────────────────────────────┐
│  TICKER TAPE — always-on strip of watchlist symbols, flips on tick │
├──────────────────────────────┬─────────────────────────────────────┤
│  PORTFOLIO VALUE  $84,210.55 │                                     │
│  ▲ 1,204.10  (1.45%) today   │           CHAT                      │
├──────────────────────────────┤                                     │
│  POSITIONS (sortable table)  │   ┌───────────────────────────┐    │
│  SYM  QTY  LAST  DAY  P/L    │   │ Confirm card:             │    │
│  ...                         │   │ Buy 10 MSFT @ 412.50      │    │
├──────────────────────────────┤   │ [Confirm] [Edit] [Cancel] │    │
│  CHART (selected symbol)     │   └───────────────────────────┘    │
│  1D 5D 1M 6M 1Y 5Y           │                                     │
└──────────────────────────────┴─────────────────────────────────────┘
```

Mobile: chat becomes a bottom sheet, the positions table collapses to cards, the tape stays.

### 8.2 Visual direction

The trap here is that finance dashboards all converge on the same look: near-black background, one acid-green accent, glowing numbers. Aim somewhere more specific — an **exchange board**: the mechanical split-flap boards that hung above trading floors before screens.

- **Palette** — `#0E1116` ink, `#F5F2EC` board paper, `#1A2028` slate (row bands), `#2F7D6E` gain, `#A8442F` loss, `#C9A227` brass (accent, used only on the tape and active states). Gain/loss are deliberately desaturated: a portfolio full of screaming saturated red is a stress machine, and muted tones let the *magnitude* of the number do the talking.
- **Type** — Display: `Bricolage Grotesque` (weight 700, tight tracking) for headline values. Body: `Instrument Sans`. Data: `IBM Plex Mono` with `font-variant-numeric: tabular-nums` on every number in a column — non-tabular digits make a live table jitter horribly.
- **Signature** — the split-flap tape. On a price change the digits flip rather than fade, and the row gets a single 400 ms tint wash (brass for up, slate for down) that decays. No permanent color-coded blinking.
- **Restraint** — flip animation on the tape only. The positions table just tints. `prefers-reduced-motion` disables both and swaps to a plain value change.
- Dark mode is the default; the light variant inverts ink/paper and drops the brass a shade for contrast.

### 8.3 Frontend details that matter

- One WebSocket for the whole app, in a context provider. Reconnect with exponential backoff (1s → 30s cap) and re-subscribe on open.
- Price updates go to a Zustand store keyed by symbol; components subscribe to their own symbol only, so one tick re-renders one row.
- Optimistic UI on manual add/remove, rollback on error.
- Empty states: "No positions yet — add one above, or just tell the chat what you bought."
- Skeleton rows on first load, not spinners.
- Accessibility floor: keyboard-reachable confirm cards, visible focus rings, `aria-live="polite"` on the portfolio total (not on every tick — that would flood a screen reader).

---

## 9. Deployment on Fly.io

### 9.1 Dockerfile

```dockerfile
# syntax=docker/dockerfile:1

# --- build frontend ---
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- build python env ---
FROM python:3.12-slim AS deps
# Pin the uv version; :latest makes builds non-reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.8.21 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# Lockfile first, without the project itself — this layer caches across source
# edits, so a code change doesn't reinstall every dependency.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime ---
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
COPY --from=deps /app /app
COPY --from=web /web/dist ./static
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

Notes on the uv setup:

- **`uv.lock` must be committed** and must not be in `.dockerignore`. `--frozen` fails the build if it's absent, which is the behavior you want — a silently-resolved dependency set on the deploy machine defeats the point.
- **`.venv` stays in `.dockerignore`.** It's platform-specific; the image builds its own.
- **Put `uvicorn` on `PATH` rather than wrapping the command in `uv run`.** `uv run` adds a process layer between Fly's `kill_signal` and uvicorn, which interferes with the graceful shutdown in §9.2. The venv-on-PATH approach keeps uvicorn as PID 1.
- Locally: `uv sync` to set up, `uv add fastapi` to add a dependency, `uv run pytest` to run tests. No manual venv activation.

### 9.2 fly.toml

```toml
app = "ticker-dash"
primary_region = "bos"          # near you; also near US market data endpoints

# Default is SIGINT + 5s, which is not enough to close N browser sockets and
# the upstream feed cleanly. Give the shutdown handler room.
kill_signal = "SIGTERM"
kill_timeout = 30

[build]

[env]
  PORT = "8080"
  DB_PATH = "/data/ticker.db"

[deploy]
  strategy = "immediate"   # single machine + volume: rolling just adds wait time

[mounts]
  source = "ticker_data"
  destination = "/data"
  initial_size = "1gb"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "off"     # MUST be the string "off", not false — see §9.4
  auto_start_machines = false    # docs: keep both enabled or both disabled
  min_machines_running = 1

  [http_service.concurrency]
    type = "connections"
    hard_limit = 200
    soft_limit = 150

[[http_service.checks]]
  interval = "30s"
  timeout = "5s"
  grace_period = "20s"           # covers DB init + first upstream WS connect
  method = "GET"
  path = "/api/health"

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
```

`/api/health` must be **exempt from auth** and must return 200 before the feed is up. If the session middleware wraps every route, the check gets a 401, the machine is marked unhealthy, and `fly deploy` rolls back — with a confusing error, because the app is running fine. Report feed state in the body, not the status code:

```python
@app.get("/api/health")          # registered before/outside the auth dependency
async def health():
    return {"status": "ok", "feed": hub.state, "market_open": is_market_open()}
```

### 9.3 Commands

```bash
# 1. Create the app WITHOUT letting flyctl write its own config.
#    Plain `fly launch` overwrites fly.toml and will clobber the file above.
fly launch --no-deploy --copy-config --ha=false

# 2. Volume must exist in the primary region before the first deploy.
fly volumes create ticker_data --size 1 --region bos --yes

# 3. Secrets. Setting these before the first deploy avoids a boot-crash loop
#    from missing env vars.
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  FINNHUB_API_KEY=... \
  SESSION_SECRET="$(openssl rand -hex 32)" \
  APP_PASSPHRASE=...

# 4. Deploy. --ha=false is belt-and-braces; apps with mounts already get one machine.
fly deploy --ha=false

fly status
fly logs
fly ssh console -C "ls -la /data"                      # volume actually mounted?
fly ssh console -C "sqlite3 /data/ticker.db .tables"   # schema actually created?
```

Add a `.dockerignore` at the repo root before the first build, or flyctl uploads `node_modules` and `.git` into the build context and the deploy crawls:

```
node_modules
frontend/node_modules
frontend/dist
.git
__pycache__
*.pyc
.venv
.env
*.db
```

Fly requires a payment method on file; there is no longer a free allowance that covers an always-on machine. Budget per §12.

### 9.4 Fly-specific gotchas

- **`auto_stop_machines` takes a string, not a boolean.** Valid values are `"off"`, `"stop"`, `"suspend"`. Writing `false` is a config error. The platform default when the key is absent is `"off"`, but `fly launch` writes `"stop"` into generated configs — so if you let flyctl author your fly.toml, your machine gets stopped after a few idle minutes, killing the upstream feed and the in-memory cache. You'd return to a dashboard showing stale prices. Set it explicitly. Fly also recommends autostop and autostart be both on or both off, hence `auto_start_machines = false` here.
- **Run migrations at boot, not in `release_command`.** The release machine runs with **no volumes attached**, so a migration written there touches an ephemeral disk and vanishes. Open the DB and apply migrations in a FastAPI lifespan startup hook instead, and make it idempotent — it runs on every boot.
- **Don't scale past 1 machine in v1.** `fly scale count 1`. Two machines = two upstream feeds, two SQLite files, divergent state. Apps with a `[mounts]` section already default to one machine, but `--ha=false` makes it explicit.
- **Every deploy is brief downtime.** One machine plus a volume means the old machine stops before the new one starts; volumes are pinned to a host and can't be double-mounted. Expect 10–30 s of unavailability per deploy. Acceptable here; if it isn't, that's the point where you move to Postgres and drop the volume.
- **Serve the SPA with a catch-all, not just a static mount.** `app.mount("/", StaticFiles(directory="static"))` returns 404 on a hard refresh at `/portfolio`. Mount assets at `/assets`, register API and WS routes, then add a final route returning `index.html` for anything unmatched.
- **The volume is not backed up by default.** Add a nightly `sqlite3 .backup` to a second file on the volume, and pull it down periodically (`fly ssh sftp get`). Fly volume snapshots exist but treat them as a second line, not the first.
- **Deploys replace the machine.** Anything in memory is lost, which is fine, but clients must reconnect — hence the backoff logic in §8.3.
- **Idle WS connections through the Fly proxy** need application-level pings; send a ping every 30 s from the server and have the client respond.
- **Region matters for the LLM call too.** `bos`/`iad` keeps latency to both market data and the Anthropic API low.

### 9.5 Verify the deploy

Run these after the first `fly deploy`. Each one catches a distinct failure that otherwise looks like "the site is just broken."

```bash
fly status                                  # 1 machine, state=started, health passing
curl -sf https://ticker-dash.fly.dev/api/health | jq

# SPA deep link — 404 here means the catch-all route is missing
curl -sI https://ticker-dash.fly.dev/portfolio | head -1

# WebSocket upgrade through the Fly proxy
npx wscat -c wss://ticker-dash.fly.dev/ws/prices

# Persistence — the real test. Write a transaction, then:
fly machine restart <id>
# ...and confirm it's still there. If it vanished, the DB is on the
# ephemeral rootfs, not the volume: check DB_PATH and the mount.
```

---

## 10. Build order

Each phase ends deployable. Deploy at the end of every phase — a plan that defers the first deploy to the end is how you discover the volume mount is wrong on day 14.

**Phase 0 — Skeleton (½ day)**
- [ ] Repo, `backend/` + `frontend/`, Dockerfile, fly.toml
- [ ] FastAPI serving the Vite build + `/api/health`
- [ ] First `fly deploy`, confirm it loads over HTTPS

**Phase 1 — Data in (1 day)**
- [ ] Provider interface + Finnhub implementation, keys in env
- [ ] `/api/quotes`, `/api/candles`, `/api/search`
- [ ] Response caching (quotes 5 s, candles 60 s) and backoff on 429s

**Phase 2 — Live prices (1–2 days)**
- [ ] PriceHub: one upstream WS, in-memory cache, subscriber registry
- [ ] `/ws/prices` fan-out with 250 ms coalescing
- [ ] Polling fallback + market-hours awareness + staleness labels
- [ ] Frontend: WS provider, Zustand store, ticker tape

**Phase 3 — Portfolio (2 days)**
- [ ] Schema + migrations, volume-backed SQLite with WAL
- [ ] Lot engine: FIFO buy/sell, rebuild-from-transactions function (write tests here first)
- [ ] Transaction + portfolio endpoints
- [ ] Positions table, add/edit/remove forms, allocation view

**Phase 4 — Charts & performance (1 day)**
- [ ] `lightweight-charts` panel with range switcher
- [ ] Portfolio value time series (reconstruct from transactions + historical closes)

**Phase 5 — Chat (2–3 days)**
- [ ] Anthropic client, tool definitions, agentic loop, SSE streaming
- [ ] Read tools wired to real services
- [ ] Web search enabled, citations rendered as links
- [ ] Propose/confirm mutation flow end to end
- [ ] History persistence

**Phase 6 — Polish & harden (1–2 days)**
- [ ] Auth, rate limits, error boundaries, empty/loading states
- [ ] Design pass against §8.2, mobile layout, reduced motion, focus states
- [ ] Backup job, structured logging, `/api/health` reporting feed state

Realistic total: **8–12 focused days.** Phases 2 and 5 are where the time actually goes.

---

## 11. Security, correctness, and honesty

- **API keys never reach the browser.** All provider and Anthropic calls are server-side. No exceptions, not even "just for local dev".
- **Auth**: passphrase → signed HttpOnly, `Secure`, `SameSite=Lax` session cookie. Rate-limit the login endpoint. This is a single-user app on the public internet; assume it will be found.
- **Rate limits**: token bucket on `/api/chat/stream` (e.g. 20 messages/hour) so a stuck retry loop can't run up an API bill.
- **Validation**: Pydantic on every input. Symbols normalized and validated against the provider's search before storage.
- **Idempotency**: every mutation from chat carries a key; replaying a tool call must not double-book.
- **Financial correctness**: unit-test the lot engine hard — partial sells, sells crossing multiple lots, oversell rejection, delete-and-rebuild consistency. Seed a known transaction set with a hand-computed expected P/L and assert on it.
- **Disclaimer**: persistent footer, plus a line under every AI answer. Prices may be delayed; nothing here is advice; verify before acting.
- **Data licensing**: check your provider's terms on redisplay before making the app public, even for personal use.

---

## 12. Running costs

| Item | Estimate |
|---|---|
| Fly `shared-cpu-1x` 512 MB, always on | ~$2–4/mo |
| Fly volume, 1 GB | ~$0.15/mo |
| Market data (Finnhub free) | $0 |
| Anthropic API | Usage-based; a few hundred chat turns/month with web search lands in the low single-digit dollars |

Budget guard: log token usage per request and expose a monthly total on `/api/health`.

---

## 13. Later

- Alerts: "tell me if NVDA drops below 100" → background watcher → web push
- Dividend tracking and income projection
- CSV import from a broker export
- Multi-user: swap SQLite for Fly Postgres, add per-user scoping, move the price hub to its own app with Redis pub/sub
- Daily digest: scheduled Claude summary of your holdings' news, emailed
- Earnings calendar overlay on charts

---

## 14. Repo layout

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
