"""SQLite connection and boot-time migrations — plan.md §5, §9.4.

Migrations run in the FastAPI lifespan hook, not a Fly `release_command`: the
release machine has no volume attached, so a migration there writes to a disk
that is thrown away. Everything here is therefore idempotent — it runs on every
boot, forever.

The engine is deliberately synchronous. Units of work are wrapped in
`asyncio.to_thread` at the router boundary, which means the lot engine stays a
plain function that tests can drive directly. For a single-user app on one
machine this is simpler than an async driver and easier to test hard, which
§11 asks for.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("ticker.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas this app depends on."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL: readers don't block the writer, which matters once the price hub and
    # a request touch the DB at the same time (§3).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every unapplied migration, in filename order. Returns what ran."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          filename    TEXT PRIMARY KEY,
          applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    applied = {row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")}

    ran: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        logger.info("applying migration %s", path.name)
        # Deliberately not wrapped in `transaction()`: sqlite3.executescript
        # issues an implicit COMMIT before it runs, which would tear down the
        # surrounding transaction and then fail on the way out. That is safe
        # here because every migration is written to be idempotent (CREATE
        # TABLE IF NOT EXISTS), and the bookkeeping row is only written after
        # the script succeeds — so a failed migration is simply retried on the
        # next boot, which §9.4 requires anyway.
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,))
        ran.append(path.name)
    return ran


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing. A partly-applied sell would corrupt the lot ledger."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def open_database(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    ran = migrate(conn)
    logger.info("database ready at %s (%d migration(s) applied)", path, len(ran))
    return conn
