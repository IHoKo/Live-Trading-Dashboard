---
name: run-ticker
description: Launch the Ticker dashboard locally and drive it — the FastAPI backend serving the built React frontend, with live prices, portfolio, charts and chat. Use when asked to run, start, serve, or manually test the app, or to check a change in the real UI rather than in tests.
---

# Running Ticker locally

One FastAPI process serves everything: the API, the WebSocket price feed, and
the built frontend as static files. There is no separate backend/frontend pair
to start unless you specifically want hot reload (see Mode B).

## Mode A — one process, production-like (default)

Use this to test anything end to end: auth, the tape, charts, chat.

```bash
cd "$(git rev-parse --show-toplevel)"

# 1. The server serves the BUILD, not source. Skipping this silently shows
#    whatever was built last.
npm --prefix frontend run build

# 2. Secrets from .env, exported so pydantic-settings picks them up.
set -a; . ./.env; set +a

# 3. STATIC_DIR must be absolute. It defaults to ./static, which only lines up
#    inside the container (the Dockerfile copies the build to /app/static).
#    Without this the API works fine and every page returns
#    503 "Frontend not built" — a confusing failure that looks like a bad build.
export STATIC_DIR="$PWD/frontend/dist"

# 4. A local database, so production data is never touched.
export DB_PATH="$PWD/ticker.db"

backend/.venv/bin/uvicorn app.main:app --app-dir backend \
  --host 127.0.0.1 --port 8080 --log-level warning
```

Run it in the background if the user wants to keep testing across turns.

Open **http://127.0.0.1:8080**.

## Mode B — hot reload for frontend work

Only worth it when iterating on UI. Two processes: the backend from Mode A on
8080, plus Vite on 5173. `frontend/vite.config.ts` already proxies `/api` and
`/ws` to 8080, so the browser talks to one origin.

```bash
npm --prefix frontend run dev     # http://127.0.0.1:5173
```

Mode A is still required for the backend half.

## You have to log in

Phase 6 added a passphrase gate. Every `/api/*` route except `/api/health`
returns 401 without a session, and the price socket is closed with code 1008.

The passphrase is the `APP_PASSPHRASE=` line in `.env`. **Never copy it into a
file under `.claude/` — that directory is tracked by git.** Read it at the point
of use:

```bash
PASS=$(grep '^APP_PASSPHRASE=' .env | cut -d= -f2-)
```

`curl` will not replay the session cookie over plain http, because the cookie is
`Secure`. For scripted checks, capture the header and send it back by hand:

```bash
COOKIE=$(curl -sS -D- -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d "{\"passphrase\":\"$PASS\"}" http://127.0.0.1:8080/api/auth/login \
  | grep -i '^set-cookie' | sed 's/^[Ss]et-[Cc]ookie: //;s/;.*//')

curl -sS -H "Cookie: $COOKIE" http://127.0.0.1:8080/api/watchlist | jq
```

## Drive it, don't just launch it

Launching only proves the entrypoint resolves. Hit the thing that changed:

```bash
curl -sf http://127.0.0.1:8080/api/health | jq        # no auth needed
curl -sS -H "Cookie: $COOKIE" 'http://127.0.0.1:8080/api/quotes?symbols=AAPL' | jq
curl -sS -H "Cookie: $COOKIE" 'http://127.0.0.1:8080/api/candles/AAPL?range=1M' | jq '.candles|length'
curl -sS -H "Cookie: $COOKIE" http://127.0.0.1:8080/api/portfolio | jq
curl -sS -H "Cookie: $COOKIE" http://127.0.0.1:8080/api/watchlist | jq
```

The WebSocket needs the cookie on the handshake:

```python
async with websockets.connect(
    "ws://127.0.0.1:8080/ws/prices", additional_headers={"Cookie": COOKIE}
) as s:
    await s.send(json.dumps({"type": "subscribe", "symbols": ["AAPL"]}))
```

## What the market being closed changes

Check `market_open` in `/api/health` before concluding anything is broken:

- **Closed** — `feed` reads `idle` and **nothing fetches on a timer**, by design
  (CLAUDE.md, "Fetch policy"). Prices appear on page load and when REFRESH is
  pressed. The split-flap will not animate, because no value changes.
- **Open** (weekday 09:30–16:00 ET) — `feed` goes `live`, the upstream socket
  connects, and prices stream. This is the only way to test the tape animation,
  the row tint, and the coalescing.

## Things that will bite you

- **`STATIC_DIR`** — see step 3. The single most common way to "break" a local run.
- **Rebuild the frontend** after any change under `frontend/src`. The server has
  no idea your source changed.
- **`/stock/candle` needs `TWELVEDATA_API_KEY`** — Finnhub's free tier 403s on
  history. Without it charts return a 502 that names the fix; everything else
  works.
- **Missing keys degrade, they don't crash.** No `FINNHUB_API_KEY` → market data
  503s. No `ANTHROPIC_API_KEY` → chat 503s. `/api/health` stays 200 either way,
  deliberately (plan.md §9.3).
- **Port 8080 already bound** — `lsof -ti :8080 | xargs kill -9`.

## Stopping it

```bash
lsof -ti :8080 | xargs kill
```

## Tests and checks (not the app, but adjacent)

```bash
uv run --directory backend pytest -q      # note --directory; rootdir matters
uv run --project backend ruff check backend/
npm --prefix frontend run lint            # tsc -b --noEmit
docker build -t ticker-dash:local .
```
