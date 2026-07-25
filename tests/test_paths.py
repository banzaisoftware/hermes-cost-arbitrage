import sys
from pathlib import Path
from unittest.mock import MagicMock

from hermes_cost_arbitrage_dashboard.paths import hermes_home

# hermes_home() is the single copy of the three-tier $HERMES_HOME resolution
# chain that used to be duplicated in store.default_state_db_path(),
# plugin_config.config_path() and plugin_api._models_dev_path(). It is
# covered here once, properly; each of those three call sites gets its own
# much narrower test (in test_store.py, test_plugin_config.py and
# test_plugin_api.py respectively) that only checks it joins the right
# filename onto whatever hermes_home() returns.


def test_hermes_home_prefers_hermes_constants(monkeypatch, tmp_path):
    # Tier 1: When hermes_constants is importable, use its get_hermes_home()
    fake_hermes_constants = MagicMock()
    fake_hermes_constants.get_hermes_home = MagicMock(return_value=str(tmp_path / "custom_home"))

    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hermes_constants)

    assert hermes_home() == Path(tmp_path / "custom_home")


def test_hermes_home_uses_hermes_home_env_var(monkeypatch):
    # Tier 2: When hermes_constants is not importable, fall back to HERMES_HOME env var
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/opt/data")

    assert hermes_home() == Path("/opt/data")


def test_hermes_home_falls_back_to_home_hermes(monkeypatch):
    # Tier 3: When neither hermes_constants nor HERMES_HOME exist, use ~/.hermes
    monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert hermes_home() == Path.home() / ".hermes"
