-- Seed the tape with the symbols it was hardcoded to from Phase 2 until now,
-- so turning the watchlist on doesn't blank anyone's dashboard.
--
-- Runs exactly once, like every migration: if you later remove a symbol it
-- stays removed, because this file is recorded in schema_migrations and never
-- replayed. INSERT OR IGNORE keeps it safe if a row already exists.
INSERT OR IGNORE INTO watchlist (symbol, sort_order) VALUES
  ('AAPL',  0),
  ('MSFT',  1),
  ('NVDA',  2),
  ('GOOGL', 3),
  ('AMZN',  4),
  ('META',  5),
  ('TSLA',  6);
