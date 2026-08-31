"""Nightly SQLite backup — plan.md §9.4.

"The volume is not backed up by default. Add a nightly `sqlite3 .backup` to a
second file on the volume, and pull it down periodically."

`Connection.backup()` is the online backup API: it copies a consistent snapshot
while the app keeps writing, which `cp` cannot do with WAL — copying the .db
without its -wal gives you a torn database that looks fine until you need it.

This is a second line of defence on the same disk, not a real offsite backup.
Pull it down with `fly ssh sftp get /data/ticker.backup.db`.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("ticker.backup")

INTERVAL_SECONDS = 60 * 60 * 24


def backup_now(conn: sqlite3.Connection, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file and rename, so an interrupted backup can never
    # replace a good one with a half-written file.
    staging = destination.with_suffix(".partial")
    target = sqlite3.connect(str(staging))
    try:
        conn.backup(target)
    finally:
        target.close()
    staging.replace(destination)
    logger.info("database backed up to %s (%d bytes)", destination, destination.stat().st_size)
    return destination


async def backup_loop(
    conn: sqlite3.Connection, destination: Path, interval: float = INTERVAL_SECONDS
) -> None:
    """Runs for the life of the process. Cancelled at shutdown."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(backup_now, conn, destination)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed backup must never take the app down with it.
            logger.exception("backup failed")
