from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Optional


class SqliteExternalIdGenerator:
    """Generate unique identifiers using a SQLite AUTOINCREMENT column.

    SQLite's built-in locking makes the increments safe even when accessed
    by multiple processes concurrently.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ids (value INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
            conn.commit()

    def next_id(
        self, prefix: str = "", side: Optional[str] = None, idx: Optional[int] = None
    ) -> str:
        """Return a new sequential identifier string."""
        with sqlite3.connect(self.db_path) as conn:
            # BEGIN IMMEDIATE obtains a reserved lock to serialize writers
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("INSERT INTO ids DEFAULT VALUES")
            value = cur.lastrowid
            conn.commit()

        parts = [prefix.rstrip("_")]
        if side:
            parts.append(side)
        if idx is not None:
            parts.append(str(idx))
        parts.append(str(value))
        return "_".join(filter(None, parts))


def uuid_external_id(
    prefix: str = "", side: Optional[str] = None, idx: Optional[int] = None
) -> str:
    """Return a unique identifier based on ``uuid.uuid4``."""
    parts = [prefix.rstrip("_")]
    if side:
        parts.append(side)
    if idx is not None:
        parts.append(str(idx))
    parts.append(uuid.uuid4().hex)
    return "_".join(filter(None, parts))
