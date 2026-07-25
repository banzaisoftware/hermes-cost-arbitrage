import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from plugin_config import DEFAULT_CONFIG, config_path, load_config, save_config


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


def test_config_path_prefers_hermes_constants(monkeypatch, tmp_path):
    # Tier 1: When hermes_constants is importable, use its get_hermes_home()
    fake_hermes_constants = MagicMock()
    fake_hermes_constants.get_hermes_home = MagicMock(return_value=str(tmp_path / "custom_home"))

    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hermes_constants)

    result = config_path()
    assert result == Path(tmp_path / "custom_home" / "cost_arbitrage_config.json")


def test_config_path_uses_hermes_home_env_var(monkeypatch):
    # Tier 2: When hermes_constants is not importable, fall back to HERMES_HOME env var
    # Remove hermes_constants from sys.modules if it exists
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)

    monkeypatch.setenv("HERMES_HOME", "/opt/data")

    result = config_path()
    assert result == Path("/opt/data/cost_arbitrage_config.json")


def test_config_path_falls_back_to_home_hermes(monkeypatch):
    # Tier 3: When neither hermes_constants nor HERMES_HOME exist, use ~/.hermes
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    result = config_path()
    assert result == Path.home() / ".hermes" / "cost_arbitrage_config.json"
