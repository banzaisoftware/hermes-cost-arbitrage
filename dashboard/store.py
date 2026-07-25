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
