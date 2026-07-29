"""Read token usage out of the Hermes session store.

Strictly read-only. The production ``state.db`` is 442 MB and is written by a
live agent; this module must never lock or mutate it.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .cost_engine import UsageVector
from .paths import hermes_home

_QUERY = """
SELECT model,
       COALESCE(billing_provider, '')      AS provider,
       COUNT(*)                            AS sessions,
       COALESCE(SUM(input_tokens), 0)      AS input_tokens,
       COALESCE(SUM(output_tokens), 0)     AS output_tokens,
       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
       COALESCE(SUM(api_call_count), 0)    AS api_call_count
FROM sessions
WHERE started_at > ?
  AND model IS NOT NULL
  AND model != ''
GROUP BY model, provider
"""


#: Every column ``_QUERY`` reads. Checked explicitly by :func:`state_db_status`,
#: because a missing one makes the aggregation fail-open to an empty result that
#: is indistinguishable from "no usage".
REQUIRED_SESSION_COLUMNS = {
    "model",
    "billing_provider",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "api_call_count",
    "started_at",
}


@dataclass(frozen=True)
class ModelUsage:
    model: str
    provider: str
    sessions: int
    usage: UsageVector
    #: Sum of the sessions table's ``api_call_count`` over the window. Lets
    #: the UI prefill a sensible ``min_context`` from the workload's own
    #: observed context per call, rather than a guess. Defaults to 0 so
    #: existing callers that construct a ModelUsage without it keep working.
    api_call_count: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.usage.input_tokens
            + self.usage.output_tokens
            + self.usage.cache_read_tokens
            + self.usage.cache_write_tokens
        )

    @property
    def avg_context_per_call(self) -> Optional[float]:
        """Average prompt context per API call: (input + cache_read + cache_write) / calls.

        ``None`` when ``api_call_count`` is zero — division is guarded so a
        model with no recorded calls never raises ``ZeroDivisionError``.
        """
        if not self.api_call_count:
            return None
        prompt_tokens = self.usage.input_tokens + self.usage.cache_read_tokens + self.usage.cache_write_tokens
        return prompt_tokens / self.api_call_count


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
            # A bare SELECT 1 succeeds even when a column the aggregation needs
            # has gone. read_usage_window would then fail-open to [] while this
            # check still said "healthy", and the tab would render a confident
            # $0 — the exact misleading zero this plugin exists to replace.
            # Hermes' schema migrations are additive (no DROP/RENAME COLUMN in
            # hermes_state.py), so a rename leaves the old column frozen rather
            # than absent; that case is undetectable here and is documented as a
            # known limit rather than papered over.
            present = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            missing = sorted(REQUIRED_SESSION_COLUMNS - present)
            if missing:
                return False, (
                    "The sessions table is missing "
                    + ", ".join(missing)
                    + " — this Hermes version's schema is not one this plugin can read"
                )
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
            api_call_count=row["api_call_count"],
        )
        for row in rows
    ]
    results.sort(key=lambda item: item.total_tokens, reverse=True)
    return results
