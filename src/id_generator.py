from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional


class SqliteExternalIdGenerator:
    """Generate unique identifiers.

    This class historically relied on a SQLite AUTOINCREMENT column to produce
    sequential identifiers. Such identifiers could be reused if the backing
    database file was ever deleted or reset.  The generator now delegates to
    ``uuid_external_id`` which relies solely on ``uuid.uuid4`` and therefore
    guarantees uniqueness regardless of any local state.
    """

    def __init__(self, db_path: Path | None = None):
        # ``db_path`` is retained for backwards compatibility; it is no longer
        # used directly by the UUID-based generator.
        self.db_path = Path(db_path) if db_path is not None else None

    def next_id(
        self, prefix: str = "", side: Optional[str] = None, idx: Optional[int] = None
    ) -> str:
        """Return a new unique identifier string."""
        return uuid_external_id(prefix, side, idx)


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
