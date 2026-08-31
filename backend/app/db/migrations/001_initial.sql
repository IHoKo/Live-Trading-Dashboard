-- plan.md §5. Applied at boot by app/db/connection.py, never in a Fly
-- release_command: the release machine runs with no volume attached, so a
-- migration there writes to an ephemeral disk and vanishes (§9.4).

-- Every buy and sell. This table is the source of truth; never edit rows, only append.
CREATE TABLE IF NOT EXISTS transactions (
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
CREATE INDEX IF NOT EXISTS idx_tx_symbol ON transactions(symbol, executed_at);

-- Open tax lots, derived from transactions. Rebuildable from scratch.
CREATE TABLE IF NOT EXISTS lots (
  id             INTEGER PRIMARY KEY,
  symbol         TEXT NOT NULL,
  quantity_open  REAL NOT NULL,
  cost_per_share REAL NOT NULL,
  opened_at      TEXT NOT NULL,
  tx_id          INTEGER NOT NULL REFERENCES transactions(id)
);
CREATE INDEX IF NOT EXISTS idx_lots_symbol ON lots(symbol, opened_at, id);

CREATE TABLE IF NOT EXISTS realized_pnl (
  id          INTEGER PRIMARY KEY,
  symbol      TEXT NOT NULL,
  quantity    REAL NOT NULL,
  proceeds    REAL NOT NULL,
  cost_basis  REAL NOT NULL,
  closed_at   TEXT NOT NULL,
  sell_tx_id  INTEGER NOT NULL REFERENCES transactions(id)
);
CREATE INDEX IF NOT EXISTS idx_realized_symbol ON realized_pnl(symbol, closed_at);

CREATE TABLE IF NOT EXISTS watchlist (
  symbol     TEXT PRIMARY KEY,
  added_at   TEXT NOT NULL DEFAULT (datetime('now')),
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id         INTEGER PRIMARY KEY,
  role       TEXT NOT NULL,   -- user | assistant
  content    TEXT NOT NULL,   -- JSON content blocks, so tool calls survive reloads
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Guards against a retried chat tool call double-booking a purchase.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key        TEXT PRIMARY KEY,
  tx_id      INTEGER REFERENCES transactions(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
