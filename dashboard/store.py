"""Read token usage out of the Hermes session store.

Strictly read-only. The production ``state.db`` is 442 MB and is written by a
live agent; this module must never lock or mutate it.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .cost_engine import UsageVector
from .paths import hermes_home

_QUERY = """
SELECT model,
       COALESCE(billing_provider, '')      AS provider,
       COUNT(*)                            AS sessions,
       COALESCE(SUM(input_tokens), 0)      AS input_tokens,
       COALESCE(SUM(output_tokens), 0)     AS output_tokens,
       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
FROM sessions
WHERE started_at > ?
  AND model IS NOT NULL
  AND model != ''
GROUP BY model, provider
"""


@dataclass(frozen=True)
class ModelUsage:
    model: str
    provider: str
    sessions: int
    usage: UsageVector

    @property
    def total_tokens(self) -> int:
        return (
            self.usage.input_tokens
            + self.usage.output_tokens
            + self.usage.cache_read_tokens
            + self.usage.cache_write_tokens
        )


def default_state_db_path() -> Path:
    """``$HERMES_HOME/state.db`` — ``/opt/data`` in the target deployment."""
    return hermes_home() / "state.db"


def state_db_status(db_path: Path | str) -> tuple[bool, str | None]:
    """Report whether ``state.db`` is present, openable read-only, and queryable.

    A dashboard tab must never confuse "no usage" with "could not read the
    database" — the latter is exactly the misleading ``$0`` this plugin
    exists to replace. This check is deliberately fail-open, the same as
    :func:`read_usage_window`: it always returns a verdict, never raises,
    and it never writes to or locks the database (a read-only connection,
    at most a single ``SELECT``).

    Returns ``(True, None)`` when healthy, or ``(False, reason)`` with a
    short human-readable explanation otherwise.
    """
    path = Path(db_path)
    try:
        if not path.exists():
            return False, f"No database found at {path}"

        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("SELECT 1 FROM sessions LIMIT 1")
        finally:
            conn.close()
        return True, None
    except sqlite3.Error as exc:
        return False, f"Database is present but unreadable: {exc}"
    except Exception as exc:  # pragma: no cover - belt-and-braces fail-open
        return False, f"Database status check failed: {exc}"


def read_usage_window(db_path: Path | str, days: int) -> list[ModelUsage]:
    """Aggregate usage per (model, provider) over the last *days*.

    Returns ``[]`` rather than raising when the database is missing or
    unreadable — a dashboard tab must degrade, not crash.
    """
    path = Path(db_path)
    if not path.exists():
        return []

    cutoff = time.time() - (days * 86400)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []

    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_QUERY, (cutoff,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    results = [
        ModelUsage(
            model=row["model"],
            provider=row["provider"],
            sessions=row["sessions"],
            usage=UsageVector(
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
            ),
        )
        for row in rows
    ]
    results.sort(key=lambda item: item.total_tokens, reverse=True)
    return results
