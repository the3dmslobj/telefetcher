"""A small SQLite ledger so re-running a fetch skips what's already on disk."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    chat_id      INTEGER NOT NULL,
    msg_id       INTEGER NOT NULL,
    path         TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    completed_at TEXT    NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);
"""


class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def record(self, chat_id: int, msg_id: int, path: Path, size: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO downloads VALUES (?, ?, ?, ?, ?)",
            (chat_id, msg_id, str(path), size, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def done(self, chat_id: int, msg_id: int) -> Path | None:
        """Only counts as done if the file is still where we left it."""
        row = self.conn.execute(
            "SELECT path, size FROM downloads WHERE chat_id = ? AND msg_id = ?",
            (chat_id, msg_id),
        ).fetchone()
        if row is None:
            return None
        path = Path(row[0])
        if path.exists() and path.stat().st_size == row[1]:
            return path
        return None

    def max_msg_id(self, chat_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(msg_id) FROM downloads WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def close(self) -> None:
        self.conn.close()
