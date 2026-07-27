import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from hermes_cost_arbitrage_dashboard.cost_engine import UsageVector
from plugin_api import PACKAGE_NAME, build_catalogue, build_summary, build_whatif
from hermes_cost_arbitrage_dashboard.store import ModelUsage

MODELS_DEV = {
    "openai": {"models": {"gpt-5.5": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}}}},
    "openrouter": {"models": {"z-ai/glm-5": {"cost": {"input": 0.95, "output": 2.55, "cache_read": 0.2}}}},
}

#: A slightly larger cache for exercising catalogue search/sort/limit — one
#: model with no cache-read rate (cache_aware_usd is None for it) and one
#: free model (monthly_usd is 0.0, so break_even_volume_ratio is None for it).
#:
#: Every entry carries "tool_call": True. That is not incidental: v0.2 Task 5
#: made build_catalogue filter on `tool_call` (capability, not the presence of
#: the key) with the filter ON by default, and a model with no `tool_call` key
#: fails open to capabilities.tool_call == False, i.e. excluded by default.
#: These entries predate that filter and this test file's search/sort/limit
#: assertions are about those axes, not capabilities — so the fixture is kept
#: passing the default filter deliberately, rather than adding tool_call
#: toggles to every one of those unrelated tests.
CATALOGUE_MODELS_DEV = {
    "openai": {"models": {"gpt-5.5": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}, "tool_call": True}}},
    "openrouter": {
        "models": {
            "z-ai/glm-5": {"cost": {"input": 0.95, "output": 2.55, "cache_read": 0.2}, "tool_call": True},
            "qwen/qwen3-32b": {"cost": {"input": 0.08, "output": 0.28}, "tool_call": True},
            "free/model": {"cost": {"input": 0, "output": 0}, "tool_call": True},
        }
    },
}

#: Dedicated fixture for capability-filter tests: deliberately varied across
#: tool_call, vision (via modalities.input), reasoning, open_weights and
#: context_limit so each filter can be exercised and distinguished from the
#: others.
#:
#:                  tool_call  vision  reasoning  open_weights  context_limit
#:   gpt-5.5        True       True    True       False         400000
#:   z-ai/glm-5     False      False   False       True          128000
#:   qwen/qwen3-32b True       False   False       True          None (absent)
#:   free/model     True       True    True        False         1000000
CAPABILITY_MODELS_DEV = {
    "openai": {
        "models": {
            "gpt-5.5": {
                "cost": {"input": 5, "output": 30, "cache_read": 0.5},
                "tool_call": True,
                "reasoning": True,
                "open_weights": False,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 400000, "input": 300000, "output": 100000},
            },
        }
    },
    "openrouter": {
        "models": {
            "z-ai/glm-5": {
                "cost": {"input": 0.95, "output": 2.55, "cache_read": 0.2},
                "tool_call": False,
                "reasoning": False,
                "open_weights": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 128000},
            },
            "qwen/qwen3-32b": {
                "cost": {"input": 0.08, "output": 0.28},
                "tool_call": True,
                "reasoning": False,
                "open_weights": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                # No "limit" key at all: context_limit must resolve to None.
            },
            "free/model": {
                "cost": {"input": 0, "output": 0},
                "tool_call": True,
                "reasoning": True,
                "open_weights": False,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 1000000},
            },
        }
    },
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


def test_build_summary_defaults_to_usage_available_so_existing_callers_are_unaffected():
    summary = build_summary(USAGE, MODELS_DEV, subscription_usd=23.0, days=30)

    assert summary["usage_available"] is True
    assert summary["usage_unavailable_reason"] is None
    assert summary["models_dev_available"] is True


def test_build_summary_reports_usage_unavailable_instead_of_a_ghost_zero():
    summary = build_summary(
        [],
        MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        usage_available=False,
        usage_unavailable_reason="No database found at /opt/data/state.db",
    )

    assert summary["usage_available"] is False
    assert summary["usage_unavailable_reason"] == "No database found at /opt/data/state.db"
    # The ghost figure is still 0.0 for an empty usage list — it is the
    # caller's (the UI's) job to hide it behind usage_available, not this
    # pure builder's.
    assert summary["ghost_cost_usd"] == 0.0


def test_build_summary_reports_models_dev_unavailable_when_the_cache_is_empty():
    summary = build_summary(USAGE, {}, subscription_usd=23.0, days=30)

    assert summary["models_dev_available"] is False


def test_build_whatif_defaults_to_usage_available_so_existing_callers_are_unaffected():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    whatif = build_whatif(USAGE, pinned, MODELS_DEV, subscription_usd=23.0, days=30)

    assert whatif["usage_available"] is True
    assert whatif["usage_unavailable_reason"] is None
    assert whatif["models_dev_available"] is True


def test_build_whatif_reports_usage_unavailable():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    whatif = build_whatif(
        [],
        pinned,
        MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        usage_available=False,
        usage_unavailable_reason="Database is present but unreadable: disk error",
    )

    assert whatif["usage_available"] is False
    assert whatif["usage_unavailable_reason"] == "Database is present but unreadable: disk error"


def test_build_whatif_reports_models_dev_unavailable_when_the_cache_is_empty():
    pinned = [{"provider": "openrouter", "model": "z-ai/glm-5"}]
    whatif = build_whatif(USAGE, pinned, {}, subscription_usd=23.0, days=30)

    assert whatif["models_dev_available"] is False


def _patch_context_paths(monkeypatch, plugin_api, tmp_path):
    """Point every $HERMES_HOME-derived path at an empty tmp_path.

    Keeps the handler tests deterministic regardless of what (if anything)
    actually lives under this machine's real Hermes home.
    """
    monkeypatch.setattr(plugin_api.store, "default_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(plugin_api.plugin_config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(plugin_api, "_models_dev_path", lambda: tmp_path / "models_dev_cache.json")


def test_summary_handler_reports_usage_unavailable_for_a_missing_database(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.summary(days=30)
    assert result["usage_available"] is False
    assert result["usage_unavailable_reason"] is not None


def test_summary_handler_clamps_an_oversized_days_value(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.summary(days=99999999)
    assert result["days"] == 365


def test_summary_handler_clamps_a_non_positive_days_value(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.summary(days=0)
    assert result["days"] == 1


def test_whatif_handler_clamps_an_oversized_days_value(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.whatif(days=99999999)
    assert result["days"] == 365


# --- build_catalogue -------------------------------------------------------


def test_build_catalogue_prices_every_priced_entry_in_the_cache():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30)

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}


def test_build_catalogue_total_matched_counts_before_truncation_returned_after():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=1)

    assert result["total_matched"] == 4
    assert result["returned"] == 1
    assert len(result["candidates"]) == 1


def test_build_catalogue_query_filters_case_insensitively_across_provider_and_model():
    by_model = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, query="GLM")
    assert [row["model"] for row in by_model["candidates"]] == ["z-ai/glm-5"]
    assert by_model["total_matched"] == 1

    by_provider = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, query="OPENAI")
    assert [row["model"] for row in by_provider["candidates"]] == ["gpt-5.5"]


def test_build_catalogue_query_with_no_matches_yields_an_empty_but_valid_result():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, query="nope")

    assert result["candidates"] == []
    assert result["total_matched"] == 0
    assert result["returned"] == 0


def test_build_catalogue_sorts_by_monthly_ascending_by_default():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=100)

    monthly_values = [row["monthly_usd"] for row in result["candidates"]]
    assert monthly_values == sorted(monthly_values)


def test_build_catalogue_order_desc_reverses_a_recognised_sort_key():
    asc = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="model", order="asc", limit=100)
    desc = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="model", order="desc", limit=100)

    asc_models = [row["model"] for row in asc["candidates"]]
    desc_models = [row["model"] for row in desc["candidates"]]
    assert asc_models == list(reversed(desc_models))


def test_build_catalogue_unrecognised_sort_key_falls_back_to_monthly_without_raising():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="not-a-real-key", limit=100)

    assert result["sort"] == "monthly"
    monthly_values = [row["monthly_usd"] for row in result["candidates"]]
    assert monthly_values == sorted(monthly_values)


def test_build_catalogue_cache_aware_none_sorts_last_in_both_orders():
    # qwen/qwen3-32b and free/model both have no cache-read rate, so their
    # cache_aware_usd is None; gpt-5.5 and z-ai/glm-5 both have one.
    asc = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="cache_aware", order="asc", limit=100)
    desc = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="cache_aware", order="desc", limit=100)

    for result in (asc, desc):
        cache_aware_values = [row["cache_aware_usd"] for row in result["candidates"]]
        none_positions = [i for i, value in enumerate(cache_aware_values) if value is None]
        assert none_positions == [2, 3]  # both None rows trail both priced rows


def test_build_catalogue_break_even_none_sorts_last_in_both_orders():
    # free/model reprices the whole usage vector to $0/month, so break_even_
    # volume_ratio is None for it (division by a falsy monthly figure is
    # deliberately skipped, same rule as build_whatif).
    asc = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="break_even", order="asc", limit=100)
    desc = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="break_even", order="desc", limit=100)

    assert asc["candidates"][-1]["model"] == "free/model"
    assert desc["candidates"][-1]["model"] == "free/model"
    assert asc["candidates"][-1]["break_even_volume_ratio"] is None


def test_build_catalogue_row_shape_matches_build_whatif():
    whatif_row = build_whatif(USAGE, [{"provider": "openai", "model": "gpt-5.5"}], MODELS_DEV, subscription_usd=23.0, days=30)[
        "candidates"
    ][0]
    catalogue_row = next(
        row
        for row in build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=100)["candidates"]
        if row["model"] == "gpt-5.5"
    )

    assert set(whatif_row.keys()) <= set(catalogue_row.keys())
    assert catalogue_row["monthly_usd"] == whatif_row["monthly_usd"]
    assert catalogue_row["cache_aware_usd"] == whatif_row["cache_aware_usd"]
    assert catalogue_row["no_cache_usd"] == whatif_row["no_cache_usd"]
    assert catalogue_row["break_even_volume_ratio"] == whatif_row["break_even_volume_ratio"]


def test_build_catalogue_envelope_echoes_the_effective_sort_order_limit_and_query():
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="model", order="desc", limit=10, query="glm"
    )

    assert result["sort"] == "model"
    assert result["order"] == "desc"
    assert result["limit"] == 10
    assert result["query"] == "glm"


def test_build_catalogue_on_empty_cache_yields_an_empty_catalogue_not_an_exception():
    result = build_catalogue(USAGE, {}, subscription_usd=23.0, days=30)

    assert result["candidates"] == []
    assert result["total_matched"] == 0
    assert result["models_dev_available"] is False


def test_build_catalogue_defaults_usage_available_like_build_whatif():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30)

    assert result["usage_available"] is True
    assert result["usage_unavailable_reason"] is None


# --- build_catalogue capability filters -------------------------------------


def test_build_catalogue_tool_call_filters_on_by_default():
    # 1 137 of 5 754 real models cannot call a tool at all, so they cannot
    # run the agent; comparing them on price is meaningless. tool_call must
    # therefore require the capability unless the caller turns it off.
    result = build_catalogue(USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, limit=100)

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "qwen/qwen3-32b", "free/model"}  # z-ai/glm-5 lacks tool_call


def test_build_catalogue_tool_call_off_imposes_no_constraint_not_require_absence():
    # The obvious bug to invert: tool_call=False must mean "no constraint",
    # never "require the model NOT be tool-capable". Pin it explicitly.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}


def test_build_catalogue_vision_filter_requires_the_capability_when_on():
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, vision=True, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "free/model"}


def test_build_catalogue_vision_filter_off_imposes_no_constraint():
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, vision=False, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}


def test_build_catalogue_reasoning_filter_requires_the_capability_when_on():
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, reasoning=True, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "free/model"}


def test_build_catalogue_open_weights_filter_requires_the_capability_when_on():
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, open_weights=True, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"z-ai/glm-5", "qwen/qwen3-32b"}


def test_build_catalogue_min_context_requires_a_known_limit_at_or_above_the_threshold():
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, min_context=200_000, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    # z-ai/glm-5 (128 000) is below threshold; qwen/qwen3-32b has an unknown
    # (missing) context_limit and must not silently pass a positive threshold.
    assert models == {"gpt-5.5", "free/model"}


def test_build_catalogue_min_context_zero_imposes_no_constraint_including_unknown_limits():
    # min_context defaults to 0, i.e. off. A model with an unknown context
    # limit must still appear when the filter isn't actually constraining
    # anything — 0 must behave like every other "off" filter.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, min_context=0, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}


def test_build_catalogue_combines_multiple_capability_filters_with_and_semantics():
    result = build_catalogue(
        USAGE,
        CAPABILITY_MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        tool_call=True,
        open_weights=True,
        limit=100,
    )

    models = {row["model"] for row in result["candidates"]}
    # tool_call=True excludes z-ai/glm-5; open_weights=True further excludes
    # gpt-5.5 and free/model (both open_weights False). Only qwen survives.
    assert models == {"qwen/qwen3-32b"}


def test_build_catalogue_echoes_the_applied_filters_in_the_envelope():
    result = build_catalogue(
        USAGE,
        CAPABILITY_MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        tool_call=False,
        vision=True,
        reasoning=True,
        open_weights=False,
        min_context=50_000,
    )

    assert result["filters"] == {
        "tool_call": False,
        "vision": True,
        "reasoning": True,
        "open_weights": False,
        "min_context": 50_000,
    }


def test_build_catalogue_defaults_the_filters_envelope_to_tool_call_only():
    result = build_catalogue(USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30)

    assert result["filters"] == {
        "tool_call": True,
        "vision": False,
        "reasoning": False,
        "open_weights": False,
        "min_context": 0,
    }


def test_build_catalogue_exposes_each_candidates_capabilities_for_per_row_badges():
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, limit=100
    )

    by_model = {row["model"]: row["capabilities"] for row in result["candidates"]}

    assert by_model["gpt-5.5"] == {
        "tool_call": True,
        "vision": True,
        "reasoning": True,
        "open_weights": False,
        "context_limit": 400_000,
    }
    assert by_model["qwen/qwen3-32b"] == {
        "tool_call": True,
        "vision": False,
        "reasoning": False,
        "open_weights": True,
        "context_limit": None,
    }


# --- GET /catalogue handler -------------------------------------------------


def test_catalogue_handler_clamps_limit_to_the_allowed_set(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(limit=999)
    assert result["limit"] == 25


def test_catalogue_handler_whitelists_sort_and_order(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(sort="hacked", order="sideways")
    assert result["sort"] == "monthly"
    assert result["order"] == "asc"


def test_catalogue_handler_clamps_an_oversized_days_value(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=99999999)
    assert result["days"] == 365


def test_catalogue_handler_reports_usage_unavailable_for_a_missing_database(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30)
    assert result["usage_available"] is False
    assert result["usage_unavailable_reason"] is not None


def test_catalogue_handler_defaults_tool_call_true_and_other_filters_off(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30)
    assert result["filters"] == {
        "tool_call": True,
        "vision": False,
        "reasoning": False,
        "open_weights": False,
        "min_context": 0,
    }


def test_catalogue_handler_forwards_explicit_filter_values(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(
        days=30, tool_call=False, vision=True, reasoning=True, open_weights=True, min_context=100_000
    )
    assert result["filters"] == {
        "tool_call": False,
        "vision": True,
        "reasoning": True,
        "open_weights": True,
        "min_context": 100_000,
    }


def test_catalogue_handler_clamps_a_negative_min_context_to_zero(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30, min_context=-500)
    assert result["filters"]["min_context"] == 0


# --- pricing_data (freshness) on build_summary / build_whatif / build_catalogue ---


def test_build_summary_defaults_pricing_data_to_an_unavailable_placeholder():
    summary = build_summary(USAGE, MODELS_DEV, subscription_usd=23.0, days=30)

    assert summary["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


def test_build_summary_passes_through_a_supplied_pricing_data():
    freshness = {"updated_at": "2026-07-27T00:00:00+00:00", "age_hours": 0.4, "available": True}
    summary = build_summary(USAGE, MODELS_DEV, subscription_usd=23.0, days=30, pricing_data=freshness)

    assert summary["pricing_data"] == freshness


def test_build_whatif_defaults_pricing_data_to_an_unavailable_placeholder():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    whatif = build_whatif(USAGE, pinned, MODELS_DEV, subscription_usd=23.0, days=30)

    assert whatif["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


def test_build_whatif_passes_through_a_supplied_pricing_data():
    freshness = {"updated_at": "2026-07-27T00:00:00+00:00", "age_hours": 0.4, "available": True}
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    whatif = build_whatif(USAGE, pinned, MODELS_DEV, subscription_usd=23.0, days=30, pricing_data=freshness)

    assert whatif["pricing_data"] == freshness


def test_build_catalogue_defaults_pricing_data_to_an_unavailable_placeholder():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30)

    assert result["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


def test_build_catalogue_passes_through_a_supplied_pricing_data():
    freshness = {"updated_at": "2026-07-27T00:00:00+00:00", "age_hours": 0.4, "available": True}
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, pricing_data=freshness)

    assert result["pricing_data"] == freshness


# --- pricing_data wired through the /summary, /whatif and /catalogue handlers ---


def test_summary_handler_computes_pricing_data_from_the_models_dev_cache_mtime(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text("{}")
    one_hour_ago = time.time() - 3600
    os.utime(cache, (one_hour_ago, one_hour_ago))

    result = plugin_api.summary(days=30)

    assert result["pricing_data"]["available"] is True
    assert result["pricing_data"]["age_hours"] == pytest.approx(1.0, abs=0.05)


def test_whatif_handler_computes_pricing_data_from_the_models_dev_cache_mtime(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.whatif(days=30)

    # No cache file exists in this tmp_path, so the honest answer is unavailable.
    assert result["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


def test_catalogue_handler_computes_pricing_data_from_the_models_dev_cache_mtime(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30)

    assert result["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


# --- POST /refresh-pricing ---------------------------------------------------


def test_refresh_pricing_handler_reports_ok_false_when_agent_models_dev_is_not_importable(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    # agent.models_dev is genuinely absent on this development machine; make
    # sure no earlier test left a fake behind in sys.modules.
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    monkeypatch.delitem(sys.modules, "agent.models_dev", raising=False)

    result = plugin_api.refresh_pricing()

    assert result["ok"] is False
    assert result["detail"]
    assert "pricing_data" in result
    assert result["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


def test_refresh_pricing_handler_calls_fetch_models_dev_and_reports_ok_true(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"

    fake_agent = MagicMock()
    fake_models_dev = MagicMock()

    def _fake_fetch(force_refresh=False):
        assert force_refresh is True
        cache.write_text("{}")  # simulate the refresh writing a fresh cache
        return {}

    fake_models_dev.fetch_models_dev = MagicMock(side_effect=_fake_fetch)

    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.models_dev", fake_models_dev)

    result = plugin_api.refresh_pricing()

    fake_models_dev.fetch_models_dev.assert_called_once_with(force_refresh=True)
    assert result["ok"] is True
    assert result["pricing_data"]["available"] is True


def test_refresh_pricing_handler_is_fail_open_when_fetch_raises(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    fake_agent = MagicMock()
    fake_models_dev = MagicMock()
    fake_models_dev.fetch_models_dev = MagicMock(side_effect=RuntimeError("network unreachable"))

    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.models_dev", fake_models_dev)

    result = plugin_api.refresh_pricing()

    assert result["ok"] is False
    assert "network unreachable" in result["detail"]
    # No cache file was written by the failed fetch: still fail-open, not a 500.
    assert result["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


# --- long-context upper bound (v0.2 Task 4) ---------------------------------

#: gpt-5.5 with the real, slightly inconsistent tier shape dumped from the
#: production cache: `tiers[0].tier.size` (272 000) disagrees with the
#: fixed-name key `context_over_200k` (which implies 200 000).
MODELS_DEV_WITH_TIER = {
    "openai": {
        "models": {
            "gpt-5.5": {
                "cost": {
                    "input": 5,
                    "output": 30,
                    "cache_read": 0.5,
                    "tiers": [
                        {
                            "input": 10,
                            "output": 45,
                            "cache_read": 1,
                            "tier": {"type": "context", "size": 272000},
                        }
                    ],
                    "context_over_200k": {"input": 10, "output": 45, "cache_read": 1},
                }
            }
        }
    },
    "openrouter": {"models": {"z-ai/glm-5": {"cost": {"input": 0.95, "output": 2.55, "cache_read": 0.2}}}},
}

CATALOGUE_MODELS_DEV_WITH_TIER = {
    "openai": {
        "models": {
            "gpt-5.5": {
                "cost": {
                    "input": 5,
                    "output": 30,
                    "cache_read": 0.5,
                    "tiers": [
                        {
                            "input": 10,
                            "output": 45,
                            "cache_read": 1,
                            "tier": {"type": "context", "size": 272000},
                        }
                    ],
                },
                "tool_call": True,
            }
        }
    },
    "openrouter": {
        "models": {
            "z-ai/glm-5": {"cost": {"input": 0.95, "output": 2.55, "cache_read": 0.2}, "tool_call": True},
        }
    },
}

#: Two models, each with api_call_count set, so the observed average context
#: per call is a clean, hand-checkable number: combined prompt tokens
#: (input + cache_read + cache_write) = 500 000 + 100 000 = 600 000 over
#: 10 + 10 = 20 calls -> 30 000 average. Per-model: gpt-5.5 alone is
#: 500 000 / 10 = 50 000; z-ai/glm-5 alone is 100 000 / 10 = 10 000.
USAGE_WITH_CALLS = [
    ModelUsage(
        model="gpt-5.5",
        provider="openai-codex",
        sessions=10,
        usage=UsageVector(input_tokens=100_000, output_tokens=10_000, cache_read_tokens=400_000),
        api_call_count=10,
    ),
    ModelUsage(
        model="z-ai/glm-5",
        provider="openrouter",
        sessions=5,
        usage=UsageVector(input_tokens=50_000, output_tokens=5_000, cache_read_tokens=50_000),
        api_call_count=10,
    ),
]


def test_build_summary_adds_the_long_context_bound_and_threshold_to_a_tiered_model_row():
    summary = build_summary(USAGE_WITH_CALLS, MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30)

    row = next(r for r in summary["models"] if r["model"] == "gpt-5.5")
    assert row["long_context_usd"] == 1.85
    assert row["tier_threshold_tokens"] == 272000


def test_build_summary_long_context_bound_is_none_not_zero_without_a_tier():
    summary = build_summary(USAGE_WITH_CALLS, MODELS_DEV, subscription_usd=23.0, days=30)

    row = next(r for r in summary["models"] if r["model"] == "gpt-5.5")
    assert row["long_context_usd"] is None
    assert row["tier_threshold_tokens"] is None


def test_build_summary_headline_and_ghost_cost_are_unaffected_by_a_published_tier():
    # The whole honesty constraint: adding tier data must never change the
    # headline figure or the ghost total. Same usage, same base rates, only
    # a tier block added — headline_usd and ghost_cost_usd must match exactly.
    untiered = build_summary(USAGE_WITH_CALLS, MODELS_DEV, subscription_usd=23.0, days=30)
    tiered = build_summary(USAGE_WITH_CALLS, MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30)

    assert tiered["ghost_cost_usd"] == untiered["ghost_cost_usd"]
    untiered_row = next(r for r in untiered["models"] if r["model"] == "gpt-5.5")
    tiered_row = next(r for r in tiered["models"] if r["model"] == "gpt-5.5")
    assert tiered_row["headline_usd"] == untiered_row["headline_usd"]
    assert tiered_row["cache_aware_usd"] == untiered_row["cache_aware_usd"]


def test_build_summary_exposes_avg_context_per_call_per_model_row():
    summary = build_summary(USAGE_WITH_CALLS, MODELS_DEV, subscription_usd=23.0, days=30)

    by_model = {r["model"]: r["avg_context_per_call"] for r in summary["models"]}
    assert by_model["gpt-5.5"] == pytest.approx(50_000.0)
    assert by_model["z-ai/glm-5"] == pytest.approx(10_000.0)


def test_build_whatif_adds_the_long_context_bound_and_threshold_to_a_candidate_row():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    row = build_whatif(USAGE_WITH_CALLS, pinned, MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30)["candidates"][0]

    assert row["long_context_usd"] == 2.62
    assert row["tier_threshold_tokens"] == 272000


def test_build_whatif_long_context_bound_is_none_not_zero_without_a_tier():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    row = build_whatif(USAGE_WITH_CALLS, pinned, MODELS_DEV, subscription_usd=23.0, days=30)["candidates"][0]

    assert row["long_context_usd"] is None
    assert row["tier_threshold_tokens"] is None


def test_build_whatif_monthly_and_cache_scenarios_are_unaffected_by_a_published_tier():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    untiered = build_whatif(USAGE_WITH_CALLS, pinned, MODELS_DEV, subscription_usd=23.0, days=30)["candidates"][0]
    tiered = build_whatif(USAGE_WITH_CALLS, pinned, MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30)["candidates"][0]

    assert tiered["monthly_usd"] == untiered["monthly_usd"]
    assert tiered["cache_aware_usd"] == untiered["cache_aware_usd"]
    assert tiered["no_cache_usd"] == untiered["no_cache_usd"]


def test_build_whatif_exposes_the_blended_avg_context_per_call():
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    result = build_whatif(USAGE_WITH_CALLS, pinned, MODELS_DEV, subscription_usd=23.0, days=30)

    assert result["avg_context_per_call"] == pytest.approx(30_000.0)


def test_build_catalogue_adds_the_long_context_bound_and_threshold_to_a_tiered_row():
    result = build_catalogue(
        USAGE_WITH_CALLS, CATALOGUE_MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30, limit=100
    )

    row = next(r for r in result["candidates"] if r["model"] == "gpt-5.5")
    assert row["long_context_usd"] == 2.62
    assert row["tier_threshold_tokens"] == 272000


def test_build_catalogue_long_context_bound_is_none_not_zero_without_a_tier():
    result = build_catalogue(
        USAGE_WITH_CALLS, CATALOGUE_MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30, limit=100
    )

    row = next(r for r in result["candidates"] if r["model"] == "z-ai/glm-5")
    assert row["long_context_usd"] is None
    assert row["tier_threshold_tokens"] is None


def test_build_catalogue_monthly_and_cache_scenarios_are_unaffected_by_a_published_tier():
    untiered = build_catalogue(USAGE_WITH_CALLS, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=100)
    tiered = build_catalogue(
        USAGE_WITH_CALLS, CATALOGUE_MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30, limit=100
    )

    untiered_row = next(r for r in untiered["candidates"] if r["model"] == "gpt-5.5")
    tiered_row = next(r for r in tiered["candidates"] if r["model"] == "gpt-5.5")
    assert tiered_row["monthly_usd"] == untiered_row["monthly_usd"]
    assert tiered_row["cache_aware_usd"] == untiered_row["cache_aware_usd"]
    assert tiered_row["no_cache_usd"] == untiered_row["no_cache_usd"]


def test_build_catalogue_exposes_the_blended_avg_context_per_call():
    result = build_catalogue(
        USAGE_WITH_CALLS, CATALOGUE_MODELS_DEV_WITH_TIER, subscription_usd=23.0, days=30, limit=100
    )

    assert result["avg_context_per_call"] == pytest.approx(30_000.0)


def test_build_whatif_avg_context_per_call_is_none_with_no_recorded_api_calls():
    # USAGE (the module-level fixture) never sets api_call_count, so it
    # defaults to 0 — the aggregate must degrade to None, not divide by zero.
    pinned = [{"provider": "openai", "model": "gpt-5.5"}]
    result = build_whatif(USAGE, pinned, MODELS_DEV, subscription_usd=23.0, days=30)

    assert result["avg_context_per_call"] is None


def test_refresh_pricing_handler_is_a_sync_def_not_async_def():
    import inspect

    import plugin_api

    assert not inspect.iscoroutinefunction(plugin_api.refresh_pricing)


def test_router_fallback_shim_defines_post_when_fastapi_is_unavailable():
    import plugin_api

    # Exercises the no-FastAPI fallback shim directly, regardless of whether
    # the real fastapi package happens to be installed in this environment.
    router = plugin_api.APIRouter()
    assert hasattr(router, "post")
    decorator = router.post("/refresh-pricing")
    assert decorator(lambda: None) is not None
