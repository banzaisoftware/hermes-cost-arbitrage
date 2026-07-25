import sqlite3
import time
from pathlib import Path

import pytest
from hermes_cost_arbitrage_dashboard.store import ModelUsage, default_state_db_path, read_usage_window
from hermes_cost_arbitrage_dashboard import store

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    model TEXT,
    started_at REAL NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    billing_provider TEXT
);
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    now = time.time()
    rows = [
        ("s1", "cli", "gpt-5.5", now - 86400, 1000, 100, 5000, 10, "openai-codex"),
        ("s2", "cli", "gpt-5.5", now - 172800, 2000, 200, 6000, 0, "openai-codex"),
        ("s3", "cron", "claude-sonnet-5", now - 86400, 500, 50, 0, 0, "anthropic"),
        # Outside a 7-day window:
        ("s4", "cli", "gpt-5.5", now - 40 * 86400, 9999, 9999, 9999, 9999, "openai-codex"),
        # Sessions with no model must be ignored, not grouped under "".
        ("s5", "cli", None, now - 3600, 7, 7, 7, 7, None),
    ]
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    conn.close()
    return path


def test_aggregates_by_model_and_provider_within_the_window(db):
    result = read_usage_window(db, days=7)

    by_model = {r.model: r for r in result}
    assert set(by_model) == {"gpt-5.5", "claude-sonnet-5"}

    gpt = by_model["gpt-5.5"]
    assert gpt.provider == "openai-codex"
    assert gpt.sessions == 2
    assert gpt.usage.input_tokens == 3000
    assert gpt.usage.output_tokens == 300
    assert gpt.usage.cache_read_tokens == 11000
    assert gpt.usage.cache_write_tokens == 10


def test_window_excludes_older_sessions(db):
    seven = {r.model: r for r in read_usage_window(db, days=7)}
    ninety = {r.model: r for r in read_usage_window(db, days=90)}

    assert seven["gpt-5.5"].usage.input_tokens == 3000
    assert ninety["gpt-5.5"].usage.input_tokens == 3000 + 9999


def test_results_are_sorted_by_total_tokens_descending(db):
    result = read_usage_window(db, days=90)

    assert result[0].model == "gpt-5.5"


def test_missing_database_returns_empty_not_an_exception(tmp_path):
    assert read_usage_window(tmp_path / "nope.db", days=30) == []


def test_database_is_opened_read_only(db):
    # A read-only connection must refuse writes — proof the live 442 MB
    # production database can never be mutated or locked by this plugin.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM sessions")
    conn.close()


def test_read_usage_window_opens_database_with_mode_ro(db, monkeypatch):
    # Verify that read_usage_window() calls sqlite3.connect with mode=ro
    # in the URI and uri=True as an argument. This pins store.py's own
    # connection opening, not just the stdlib's read-only mode capability.
    # If someone deletes mode=ro or uri=True from store.py, this fails.
    connect_calls = []

    original_connect = sqlite3.connect

    def spy_connect(database, **kwargs):
        connect_calls.append({"database": database, "kwargs": kwargs})
        return original_connect(database, **kwargs)

    monkeypatch.setattr(store.sqlite3, "connect", spy_connect)

    result = read_usage_window(db, days=7)
    assert len(result) > 0  # Verify query succeeded

    # Check that connect was called with a URI containing mode=ro and uri=True
    assert len(connect_calls) >= 1
    call = connect_calls[0]
    assert "mode=ro" in call["database"], f"Expected mode=ro in database URI, got: {call['database']}"
    assert call["kwargs"].get("uri") is True, f"Expected uri=True, got: {call['kwargs']}"


def test_default_state_db_path_joins_state_db_onto_hermes_home(monkeypatch, tmp_path):
    # The three-tier $HERMES_HOME resolution itself is covered once, properly,
    # in test_paths.py::test_hermes_home_*. This only pins that
    # default_state_db_path() asks paths.hermes_home() and appends state.db.
    monkeypatch.setattr(store, "hermes_home", lambda: tmp_path / "custom_home")

    result = default_state_db_path()
    assert result == tmp_path / "custom_home" / "state.db"
