import json

import pytest

from hermes_cost_arbitrage_dashboard import plugin_config
from hermes_cost_arbitrage_dashboard.plugin_config import DEFAULT_CONFIG, config_path, load_config, save_config


def test_absent_file_yields_defaults(tmp_path):
    config = load_config(tmp_path / "absent.json")

    assert config["subscription_usd_per_month"] == DEFAULT_CONFIG["subscription_usd_per_month"]
    assert len(config["pinned"]) >= 5
    assert all("provider" in entry and "model" in entry for entry in config["pinned"])


def test_corrupt_file_yields_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")

    assert load_config(path)["pinned"] == DEFAULT_CONFIG["pinned"]


def test_save_round_trips(tmp_path):
    path = tmp_path / "config.json"

    saved = save_config(path, {"subscription_usd_per_month": 42, "pinned": [{"provider": "openrouter", "model": "z-ai/glm-5"}]})

    assert saved["subscription_usd_per_month"] == 42.0
    assert load_config(path)["pinned"] == [{"provider": "openrouter", "model": "z-ai/glm-5"}]


def test_partial_payload_keeps_defaults_for_missing_keys(tmp_path):
    path = tmp_path / "config.json"

    saved = save_config(path, {"subscription_usd_per_month": 30})

    assert saved["pinned"] == DEFAULT_CONFIG["pinned"]


def test_malformed_pinned_entries_are_dropped(tmp_path):
    path = tmp_path / "config.json"

    saved = save_config(path, {"pinned": [{"model": "no-provider"}, "junk", {"provider": "openai", "model": "gpt-5.5"}]})

    assert saved["pinned"] == [{"provider": "openai", "model": "gpt-5.5"}]


def test_written_file_is_valid_json(tmp_path):
    path = tmp_path / "config.json"
    save_config(path, {"subscription_usd_per_month": 23})

    assert json.loads(path.read_text())["subscription_usd_per_month"] == 23.0


def test_top_level_list_yields_defaults(tmp_path):
    # A user hand-edited the file in $HERMES_HOME into valid JSON that is
    # the wrong shape (a bare list instead of an object).
    path = tmp_path / "config.json"
    path.write_text("[]")

    assert load_config(path) == DEFAULT_CONFIG


def test_top_level_string_yields_defaults(tmp_path):
    # Same threat model: valid JSON, wrong top-level type (a bare string).
    path = tmp_path / "config.json"
    path.write_text('"just a string"')

    assert load_config(path) == DEFAULT_CONFIG


def test_save_config_is_atomic_on_write_failure(tmp_path, monkeypatch):
    # A pre-existing, valid config file must survive a save that fails
    # partway through the write. The old implementation wrote directly into
    # `target`, so a failure here would truncate it; this test would fail
    # against that code.
    path = tmp_path / "config.json"
    save_config(
        path,
        {
            "subscription_usd_per_month": 99,
            "pinned": [{"provider": "openai", "model": "gpt-5.5"}],
        },
    )

    def failing_dump(*args, **kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(plugin_config.json, "dump", failing_dump)

    with pytest.raises(RuntimeError):
        save_config(path, {"subscription_usd_per_month": 1, "pinned": []})

    # The pre-existing file must be untouched and still load correctly.
    reloaded = load_config(path)
    assert reloaded["subscription_usd_per_month"] == 99.0
    assert reloaded["pinned"] == [{"provider": "openai", "model": "gpt-5.5"}]

    # No stray temporary file left behind in the directory.
    leftovers = [entry for entry in tmp_path.iterdir() if entry != path]
    assert leftovers == []


def test_config_path_joins_the_filename_onto_hermes_home(monkeypatch, tmp_path):
    # The three-tier $HERMES_HOME resolution itself is covered once, properly,
    # in test_paths.py::test_hermes_home_*. This only pins that config_path()
    # asks paths.hermes_home() and appends the config filename.
    monkeypatch.setattr(plugin_config, "hermes_home", lambda: tmp_path / "custom_home")

    result = config_path()
    assert result == tmp_path / "custom_home" / "cost_arbitrage_config.json"


def test_non_finite_subscription_falls_back_to_the_default(tmp_path):
    # float("Infinity") and float("NaN") pass float() and json.dump writes them,
    # but Starlette renders with allow_nan=False - so persisting one would make
    # every later GET 500 forever, recoverable only by deleting this file.
    for hostile in ("Infinity", "-Infinity", "NaN", float("inf"), float("nan")):
        path = tmp_path / f"config-{str(hostile)[:4]}.json"
        saved = save_config(path, {"subscription_usd_per_month": hostile})
        assert saved["subscription_usd_per_month"] == DEFAULT_CONFIG["subscription_usd_per_month"]
        import json as _json
        assert _json.loads(path.read_text())["subscription_usd_per_month"] == \
            DEFAULT_CONFIG["subscription_usd_per_month"]


def test_absurd_subscription_values_fall_back_to_the_default(tmp_path):
    path = tmp_path / "config.json"

    assert save_config(path, {"subscription_usd_per_month": -1})["subscription_usd_per_month"] == 23.0
    assert save_config(path, {"subscription_usd_per_month": 1e12})["subscription_usd_per_month"] == 23.0


def test_pinned_list_is_bounded_in_length_and_field_size(tmp_path):
    MAX_PINNED_ENTRIES = plugin_config.MAX_PINNED_ENTRIES
    MAX_PINNED_FIELD_CHARS = plugin_config.MAX_PINNED_FIELD_CHARS

    path = tmp_path / "config.json"
    huge = [{"provider": "p" * 5000, "model": "m" * 5000} for _ in range(5000)]

    saved = save_config(path, {"pinned": huge})

    # Unbounded, one PUT persists a file every later GET reloads and prices.
    assert len(saved["pinned"]) == MAX_PINNED_ENTRIES
    assert len(saved["pinned"][0]["provider"]) == MAX_PINNED_FIELD_CHARS
    assert len(saved["pinned"][0]["model"]) == MAX_PINNED_FIELD_CHARS
    assert path.stat().st_size < 200_000
