"""Structured logging and the nightly backup — plan.md §9.4, §10 Phase 6."""

import json
import logging
import sqlite3
from pathlib import Path

from app.db.connection import connect, migrate, open_database
from app.logging_config import JsonFormatter, configure
from app.services.backup import backup_now


def test_log_records_are_json_lines() -> None:
    record = logging.LogRecord("ticker.db", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["msg"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "ticker.db"
    assert parsed["ts"].startswith("20")


def test_exceptions_are_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exc"]


def test_configure_routes_our_loggers_to_stdout(capsys) -> None:
    """The bug this fixes: uvicorn configures only its own loggers, so every
    logger.info in this app was discarded in the container."""
    configure("info")
    logging.getLogger("ticker.db").info("migration applied")
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1])["msg"] == "migration applied"


def test_noisy_third_party_loggers_are_quietened() -> None:
    configure("info")
    assert logging.getLogger("httpx").level == logging.WARNING


def test_backup_produces_a_readable_copy(tmp_path: Path) -> None:
    source = open_database(tmp_path / "live.db")
    source.execute(
        "INSERT INTO transactions (symbol, side, quantity, price, executed_at)"
        " VALUES ('AAPL','BUY',10,100,'2026-01-01T00:00:00Z')"
    )

    destination = backup_now(source, tmp_path / "live.backup.db")
    source.close()

    restored = sqlite3.connect(destination)
    row = restored.execute("SELECT symbol, quantity FROM transactions").fetchone()
    restored.close()
    assert row == ("AAPL", 10.0)


def test_backup_is_consistent_while_the_source_is_open(tmp_path: Path) -> None:
    """WAL is why this uses Connection.backup() and not a file copy: copying
    the .db without its -wal gives a torn database that looks fine until you
    need it."""
    source = open_database(tmp_path / "live.db")
    for i in range(50):
        source.execute(
            "INSERT INTO transactions (symbol, side, quantity, price, executed_at)"
            " VALUES (?,'BUY',1,1,'2026-01-01T00:00:00Z')",
            (f"S{i}",),
        )

    destination = backup_now(source, tmp_path / "live.backup.db")
    source.close()

    restored = sqlite3.connect(destination)
    count = restored.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
    restored.close()
    assert count == 50
    assert integrity == "ok"


def test_a_partial_backup_never_replaces_a_good_one(tmp_path: Path) -> None:
    source = open_database(tmp_path / "live.db")
    destination = backup_now(source, tmp_path / "live.backup.db")
    source.close()
    assert not destination.with_suffix(".partial").exists(), "staging file is cleaned up"


def test_migrations_survive_a_backup_restore(tmp_path: Path) -> None:
    source = open_database(tmp_path / "live.db")
    destination = backup_now(source, tmp_path / "live.backup.db")
    source.close()

    restored = connect(destination)
    ran = migrate(restored)  # already applied in the copy
    tables = {
        r["name"] for r in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    restored.close()
    assert ran == [], "the backup carries its migration history"
    assert {"transactions", "lots", "realized_pnl"} <= tables
