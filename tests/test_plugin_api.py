import sys
from decimal import Decimal

from hermes_cost_arbitrage_dashboard.cost_engine import UsageVector
from plugin_api import PACKAGE_NAME, build_summary, build_whatif
from hermes_cost_arbitrage_dashboard.store import ModelUsage

MODELS_DEV = {
    "openai": {"models": {"gpt-5.5": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}}}},
    "openrouter": {"models": {"z-ai/glm-5": {"cost": {"input": 0.95, "output": 2.55, "cache_read": 0.2}}}},
}

USAGE = [
    ModelUsage(
        model="gpt-5.5",
        provider="openai-codex",
        sessions=227,
        usage=UsageVector(
            input_tokens=59_614_755,
            output_tokens=2_135_048,
            cache_read_tokens=377_920_000,
            cache_write_tokens=24_368,
        ),
    )
]


def test_summary_prices_a_subscription_month_at_its_paid_api_value():
    summary = build_summary(USAGE, MODELS_DEV, subscription_usd=23.0, days=30)

    assert summary["days"] == 30
    assert summary["subscription_usd_per_month"] == 23.0
    # The number the native dashboard cannot produce.
    assert summary["ghost_cost_usd"] == 551.21
    assert summary["totals"]["cache_read_tokens"] == 377_920_000

    row = summary["models"][0]
    assert row["model"] == "gpt-5.5"
    assert row["billing_provider"] == "openai-codex"
    assert row["priced_as_provider"] == "openai"
    assert row["cache_status"] == "priced"


def test_summary_projects_a_short_window_to_a_month():
    seven_days = build_summary(USAGE, MODELS_DEV, subscription_usd=23.0, days=7)

    # Same tokens over 7 days project to a larger month.
    assert seven_days["monthly_projection_usd"] > seven_days["ghost_cost_usd"]
    assert round(seven_days["monthly_projection_usd"], 2) == round(seven_days["ghost_cost_usd"] * 30 / 7, 2)


def test_whatif_ranks_candidates_cheapest_first():
    pinned = [
        {"provider": "openai", "model": "gpt-5.5"},
        {"provider": "openrouter", "model": "z-ai/glm-5"},
    ]

    result = build_whatif(USAGE, pinned, MODELS_DEV, subscription_usd=23.0, days=30)

    assert [row["model"] for row in result["candidates"]] == ["z-ai/glm-5", "gpt-5.5"]
    assert result["candidates"][0]["monthly_usd"] < result["candidates"][1]["monthly_usd"]


def test_whatif_exposes_both_cache_scenarios_and_a_break_even_ratio():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]

    row = build_whatif(USAGE, pinned, MODELS_DEV, subscription_usd=23.0, days=30)["candidates"][0]

    assert row["cache_aware_usd"] == 551.21
    assert row["no_cache_usd"] == 2251.85
    # The subscription buys 23/551.21 of the current volume.
    assert round(row["break_even_volume_ratio"], 4) == round(23.0 / 551.21, 4)


def test_unpriced_candidate_is_reported_not_dropped():
    pinned = [{"provider": "openrouter", "model": "unknown/model"}]

    row = build_whatif(USAGE, pinned, MODELS_DEV, subscription_usd=23.0, days=30)["candidates"][0]

    assert row["status"] == "no_pricing"
    assert row["monthly_usd"] is None


def test_empty_usage_produces_zeroes_without_dividing_by_zero():
    summary = build_summary([], MODELS_DEV, subscription_usd=23.0, days=30)
    whatif = build_whatif([], [{"provider": "openai", "model": "gpt-5.5"}], MODELS_DEV, subscription_usd=23.0, days=30)

    assert summary["ghost_cost_usd"] == 0.0
    assert whatif["candidates"][0]["monthly_usd"] == 0.0
    assert whatif["candidates"][0]["break_even_volume_ratio"] is None


def test_build_summary_returns_a_models_row_per_usage_row_including_unpriced():
    usage = USAGE + [
        ModelUsage(
            model="unknown/model",
            provider="openrouter",
            sessions=3,
            usage=UsageVector(input_tokens=1_000, output_tokens=500),
        )
    ]

    summary = build_summary(usage, MODELS_DEV, subscription_usd=23.0, days=30)

    assert len(summary["models"]) == len(usage)
    unpriced_row = next(row for row in summary["models"] if row["model"] == "unknown/model")
    assert unpriced_row["status"] == "no_pricing"


def test_put_config_handler_returns_a_clean_error_instead_of_a_bare_oserror(monkeypatch):
    import plugin_api

    def _raise_oserror(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(plugin_api.plugin_config, "save_config", _raise_oserror)

    try:
        result = plugin_api.put_config({"subscription_usd_per_month": 42.0})
    except Exception as exc:
        # Whichever strategy was chosen, it must not be a bare OSError.
        assert not isinstance(exc, OSError)
        assert type(exc).__name__ != "OSError"
    else:
        assert result["status"] == "error"
        assert "disk full" in result["detail"]


def test_sibling_modules_are_bootstrapped_under_the_namespaced_package_only():
    import plugin_api

    # The whole point: no bare, collision-prone module names in the
    # process-global sys.modules for this plugin's sibling modules.
    assert f"{PACKAGE_NAME}.cost_engine" in sys.modules
    assert f"{PACKAGE_NAME}.pricing" in sys.modules
    assert f"{PACKAGE_NAME}.store" in sys.modules
    assert f"{PACKAGE_NAME}.plugin_config" in sys.modules
    assert "cost_engine" not in sys.modules
    assert "pricing" not in sys.modules
    assert "store" not in sys.modules
    assert "plugin_config" not in sys.modules

    # The bootstrap is idempotent: calling it again returns the very same
    # package and submodule objects rather than re-executing them.
    package_before = sys.modules[PACKAGE_NAME]
    cost_engine_before = plugin_api.cost_engine
    pricing_before = plugin_api.pricing
    store_before = plugin_api.store
    plugin_config_before = plugin_api.plugin_config

    package_after = plugin_api._bootstrap_package()
    import importlib

    cost_engine_after = importlib.import_module(f"{PACKAGE_NAME}.cost_engine")
    pricing_after = importlib.import_module(f"{PACKAGE_NAME}.pricing")
    store_after = importlib.import_module(f"{PACKAGE_NAME}.store")
    plugin_config_after = importlib.import_module(f"{PACKAGE_NAME}.plugin_config")

    assert package_after is package_before
    assert cost_engine_after is cost_engine_before
    assert pricing_after is pricing_before
    assert store_after is store_before
    assert plugin_config_after is plugin_config_before


def test_models_dev_path_joins_the_filename_onto_hermes_home(monkeypatch, tmp_path):
    import plugin_api

    monkeypatch.setattr(plugin_api.paths, "hermes_home", lambda: tmp_path / "custom_home")

    result = plugin_api._models_dev_path()
    assert result == tmp_path / "custom_home" / "models_dev_cache.json"
