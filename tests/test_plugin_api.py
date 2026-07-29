import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from hermes_cost_arbitrage_dashboard.cost_engine import UsageVector
from plugin_api import PACKAGE_NAME, build_catalogue, build_providers, build_summary, build_whatif
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
    # hide_free=False: this test is about the fixture's four *priced* entries
    # all surfacing, not about the hide_free filter (v0.2 Task 9 made the
    # builder's own hide_free default True, same as the /catalogue handler).
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, hide_free=False)

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}


def test_build_catalogue_total_matched_counts_before_truncation_returned_after():
    # hide_free=False: keeps this test about limit/offset counting, not about
    # the hide_free default (see v0.2 Task 9).
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=1, hide_free=False)

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
    # hide_free=False: this is about None-sorting across all four fixture
    # entries, not about the hide_free filter (v0.2 Task 9).
    asc = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="cache_aware", order="asc", limit=100, hide_free=False
    )
    desc = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="cache_aware", order="desc", limit=100, hide_free=False
    )

    for result in (asc, desc):
        cache_aware_values = [row["cache_aware_usd"] for row in result["candidates"]]
        none_positions = [i for i, value in enumerate(cache_aware_values) if value is None]
        assert none_positions == [2, 3]  # both None rows trail both priced rows


def test_build_catalogue_break_even_none_sorts_last_in_both_orders():
    # free/model reprices the whole usage vector to $0/month, so break_even_
    # volume_ratio is None for it (division by a falsy monthly figure is
    # deliberately skipped, same rule as build_whatif). hide_free=False so
    # free/model is still present to exercise the None-sorts-last case (v0.2
    # Task 9 flipped the builder's own hide_free default to True).
    asc = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="break_even", order="asc", limit=100, hide_free=False
    )
    desc = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="break_even", order="desc", limit=100, hide_free=False
    )

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
    # hide_free=False: this test is about the tool_call filter, not hide_free
    # (v0.2 Task 9 flipped the builder's own hide_free default to True, which
    # would otherwise also drop free/model here for an unrelated reason).
    result = build_catalogue(USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, limit=100, hide_free=False)

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "qwen/qwen3-32b", "free/model"}  # z-ai/glm-5 lacks tool_call


def test_build_catalogue_tool_call_off_imposes_no_constraint_not_require_absence():
    # The obvious bug to invert: tool_call=False must mean "no constraint",
    # never "require the model NOT be tool-capable". Pin it explicitly.
    # hide_free=False keeps this test about tool_call, not hide_free.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, limit=100, hide_free=False
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}


def test_build_catalogue_vision_filter_requires_the_capability_when_on():
    # hide_free=False keeps this test about the vision filter, not hide_free.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, vision=True, limit=100, hide_free=False
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "free/model"}


def test_build_catalogue_vision_filter_off_imposes_no_constraint():
    # hide_free=False keeps this test about the vision filter, not hide_free.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, vision=False, limit=100, hide_free=False
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}


def test_build_catalogue_reasoning_filter_requires_the_capability_when_on():
    # hide_free=False keeps this test about the reasoning filter, not hide_free.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, reasoning=True, limit=100, hide_free=False
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
    # hide_free=False keeps this test about min_context, not hide_free.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, min_context=200_000, limit=100,
        hide_free=False,
    )

    models = {row["model"] for row in result["candidates"]}
    # z-ai/glm-5 (128 000) is below threshold; qwen/qwen3-32b has an unknown
    # (missing) context_limit and must not silently pass a positive threshold.
    assert models == {"gpt-5.5", "free/model"}


def test_build_catalogue_min_context_zero_imposes_no_constraint_including_unknown_limits():
    # min_context defaults to 0, i.e. off. A model with an unknown context
    # limit must still appear when the filter isn't actually constraining
    # anything — 0 must behave like every other "off" filter. hide_free=False
    # keeps this test about min_context, not hide_free.
    result = build_catalogue(
        USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, min_context=0, limit=100,
        hide_free=False,
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
    # v0.2 Task 7 added providers / providers_mode / hide_free to the same
    # envelope key. This call doesn't pass any of the three, so they show up
    # here at build_catalogue's own defaults: providers=[], mode="include",
    # and (since v0.2 Task 9 unified the builder's own hide_free default with
    # the /catalogue handler's) hide_free=True.
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
        "providers": [],
        "providers_mode": "include",
        "hide_free": True,
        "credentialed_only": True,
    }


def test_build_catalogue_defaults_the_filters_envelope_with_tool_call_and_hide_free_true():
    # Renamed twice now: first from ..._to_tool_call_only (the envelope grew
    # providers/providers_mode/hide_free keys), and again here for v0.2 Task 9,
    # which unified build_catalogue's own hide_free default with the
    # /catalogue handler's (both True). Before Task 9, tool_call was the only
    # filter defaulting to a constraining state at the builder level; now
    # hide_free does too, so "the only true flag" stopped being accurate.
    result = build_catalogue(USAGE, CAPABILITY_MODELS_DEV, subscription_usd=23.0, days=30)

    assert result["filters"] == {
        "tool_call": True,
        "vision": False,
        "reasoning": False,
        "open_weights": False,
        "min_context": 0,
        "providers": [],
        "providers_mode": "include",
        "hide_free": True,
        "credentialed_only": True,
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


# --- build_catalogue hide_free -----------------------------------------------


def test_build_catalogue_hide_free_true_excludes_zero_priced_models():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, hide_free=True, limit=100)

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b"}
    assert result["total_matched"] == 3


def test_build_catalogue_hide_free_defaults_to_true_on_the_builder():
    # v0.2 Task 9: build_catalogue's own default is now True (drop free
    # models), matching the /catalogue *handler*'s default -- they used to
    # disagree, which meant a future direct caller of build_catalogue would
    # silently get free models back despite the product's stated intent.
    # Every other filter here already had matching builder/handler defaults;
    # this closes the sole exception. Still fully switchable: passing
    # hide_free=False explicitly restores the "no constraint" behaviour, as
    # pinned by the second assertion below.
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=100)
    models = {row["model"] for row in result["candidates"]}
    assert "free/model" not in models

    unfiltered = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=100, hide_free=False
    )
    unfiltered_models = {row["model"] for row in unfiltered["candidates"]}
    assert "free/model" in unfiltered_models


def test_build_catalogue_hide_free_is_defined_by_published_rates_not_computed_cost():
    # An empty usage window prices every model to $0.00/month (see
    # test_empty_usage_produces_zeroes_without_dividing_by_zero) -- hide_free
    # must not key off that computed monthly figure, or it would hide the
    # entire catalogue whenever the usage window is empty. "Free" must be a
    # fact about the grid's own published rates (input_per_million ==
    # output_per_million == Decimal(0)), independent of what usage happens
    # to be priced against it.
    result = build_catalogue([], CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, hide_free=True, limit=100)

    models = {row["model"] for row in result["candidates"]}
    assert "gpt-5.5" in models  # priced at $5 / $30 per million -- not free
    assert "free/model" not in models  # priced at $0 / $0 per million -- genuinely free
    gpt_row = next(row for row in result["candidates"] if row["model"] == "gpt-5.5")
    assert gpt_row["monthly_usd"] == 0.0  # confirms the $0 here is from empty usage, not a free grid


def test_build_catalogue_echoes_hide_free_in_the_envelope():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, hide_free=True)

    assert result["filters"]["hide_free"] is True


# --- build_catalogue credentialed_only (v0.2 Task 9) -------------------------


def test_build_catalogue_credentialed_only_defaults_true_but_imposes_no_constraint_when_status_unavailable():
    # THE dangerous failure mode this filter must never trigger: when
    # credential status could not be determined (credential_status_available
    # defaults False, matching pricing.credentialed_provider_slugs()'s own
    # fail-open contract), credentialed_only=True must impose NO constraint
    # rather than silently emptying the catalogue. hide_free=False and
    # tool_call=False isolate this from the other two filters' own defaults.
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, tool_call=False, hide_free=False, limit=100
    )

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b", "free/model"}
    assert result["credential_status_available"] is False


def test_build_catalogue_credentialed_only_filters_when_status_is_available():
    result = build_catalogue(
        USAGE,
        CATALOGUE_MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        tool_call=False,
        hide_free=False,
        limit=100,
        credentialed_provider_slugs={"openai"},
        credential_status_available=True,
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai"}
    assert result["credential_status_available"] is True


def test_build_catalogue_credentialed_only_off_imposes_no_constraint_even_when_status_is_available():
    # The obvious bug to invert: credentialed_only=False must mean "no
    # constraint", never "require the provider NOT be credentialed". Pin it
    # explicitly, same pattern as tool_call's own off-state test.
    result = build_catalogue(
        USAGE,
        CATALOGUE_MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        tool_call=False,
        hide_free=False,
        limit=100,
        credentialed_only=False,
        credentialed_provider_slugs={"openai"},
        credential_status_available=True,
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai", "openrouter"}


def test_build_catalogue_credentialed_provider_slugs_matched_case_insensitively():
    # credentialed_provider_slugs is contractually pre-lowercased by
    # pricing.credentialed_provider_slugs(), but build_catalogue normalizes
    # defensively anyway (same posture as _parse_providers for the `providers`
    # filter) so a caller passing mixed case still gets the right answer.
    result = build_catalogue(
        USAGE,
        CATALOGUE_MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        tool_call=False,
        hide_free=False,
        limit=100,
        credentialed_provider_slugs={"OpenAI"},
        credential_status_available=True,
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai"}


def test_build_catalogue_echoes_credentialed_only_in_the_envelope():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, credentialed_only=False)

    assert result["filters"]["credentialed_only"] is False


def test_build_catalogue_exposes_credential_sources_checked_so_false_never_reads_as_verified_absent():
    # The payload must never let a consumer mistake credential_present:
    # False for "verified absent from every store" -- this list names
    # exactly which local stores were actually consulted (see
    # pricing.CREDENTIAL_SOURCES_CHECKED and pricing.credentialed_provider_slugs's
    # own "Coverage this deliberately excludes" paragraph for what is NOT
    # here, e.g. the AWS SDK chain for bedrock).
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30)

    assert result["credential_sources_checked"] == [
        "env_vars",
        "auth_store.credential_pool",
        "auth_store.providers",
    ]


# --- build_catalogue provider include/exclude -------------------------------


def test_build_catalogue_providers_include_mode_keeps_only_listed_providers():
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, providers="openai", providers_mode="include", limit=100
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai"}


def test_build_catalogue_providers_exclude_mode_drops_listed_providers():
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, providers="openai", providers_mode="exclude", limit=100
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openrouter"}


def test_build_catalogue_empty_providers_list_imposes_no_constraint_in_include_mode():
    # The obvious bug to pin: an empty include list must not mean "show
    # nothing" -- it must mean "no constraint", exactly like every other
    # filter's off-state.
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, providers="", providers_mode="include", limit=100
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai", "openrouter"}


def test_build_catalogue_empty_providers_list_imposes_no_constraint_in_exclude_mode():
    # Same pin as above, for exclude mode: an empty exclude list must not be
    # misread as "exclude everything".
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, providers="", providers_mode="exclude", limit=100
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai", "openrouter"}


def test_build_catalogue_providers_matched_case_insensitively_and_trimmed():
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, providers="  OpenAI  ", providers_mode="include", limit=100
    )

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai"}


def test_build_catalogue_unknown_provider_name_matches_nothing_without_raising():
    result = build_catalogue(
        USAGE,
        CATALOGUE_MODELS_DEV,
        subscription_usd=23.0,
        days=30,
        providers="not-a-real-provider",
        providers_mode="include",
        limit=100,
    )

    assert result["candidates"] == []
    assert result["total_matched"] == 0


def test_build_catalogue_echoes_providers_and_providers_mode_normalized():
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, providers=" OpenAI , OPENROUTER ", providers_mode="exclude"
    )

    assert result["filters"]["providers"] == ["openai", "openrouter"]
    assert result["filters"]["providers_mode"] == "exclude"


def test_build_catalogue_providers_mode_invalid_value_falls_back_to_include():
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, providers="openai", providers_mode="sideways", limit=100
    )

    assert result["filters"]["providers_mode"] == "include"
    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai"}


# --- build_catalogue offset pagination ---------------------------------------


def test_build_catalogue_offset_slices_the_sorted_set_without_overlap_or_gaps():
    # hide_free=False: this test is about pagination across all four fixture
    # entries, not about hide_free (v0.2 Task 9 flipped the builder's own
    # hide_free default to True).
    page_one = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="model", limit=2, offset=0, hide_free=False
    )
    page_two = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, sort="model", limit=2, offset=2, hide_free=False
    )

    first_models = [row["model"] for row in page_one["candidates"]]
    second_models = [row["model"] for row in page_two["candidates"]]
    assert len(first_models) == len(second_models) == 2
    combined = first_models + second_models
    assert len(combined) == len(set(combined)) == 4  # no overlap, no gaps across the two pages


def test_build_catalogue_offset_beyond_the_end_returns_empty_never_wraps():
    # hide_free=False: this test is about total_matched/page/pages arithmetic
    # over all four fixture entries, not about hide_free (v0.2 Task 9 flipped
    # the builder's own hide_free default to True).
    result = build_catalogue(
        USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=10, offset=1000, hide_free=False
    )

    assert result["candidates"] == []
    assert result["total_matched"] == 4
    assert result["returned"] == 0
    assert result["pages"] == 1  # ceil(4 / 10)
    assert result["page"] == 101  # reports where the offset landed rather than erroring or wrapping


def test_build_catalogue_page_and_pages_reflect_offset_and_limit():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=2, offset=2)

    assert result["pages"] == 2  # ceil(4 / 2)
    assert result["page"] == 2  # offset 2 // limit 2 + 1


def test_build_catalogue_pages_guards_division_when_limit_is_zero():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, limit=0)

    assert result["candidates"] == []
    assert result["pages"] == 0
    assert result["page"] == 1


def test_build_catalogue_pages_guards_division_when_total_matched_is_zero():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, query="nope", limit=25)

    assert result["total_matched"] == 0
    assert result["pages"] == 0
    assert result["page"] == 1


def test_build_catalogue_envelope_echoes_offset():
    result = build_catalogue(USAGE, CATALOGUE_MODELS_DEV, subscription_usd=23.0, days=30, offset=5)

    assert result["offset"] == 5


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


def test_catalogue_handler_defaults_tool_call_and_hide_free_true_others_off(monkeypatch, tmp_path):
    # Renamed from ..._tool_call_true_and_other_filters_off: v0.2 Task 7 gave
    # the /catalogue *handler* (not build_catalogue itself) a hide_free
    # default of True, so "other filters off" stopped being accurate the
    # moment that shipped -- hide_free is on by default here too.
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30)
    assert result["filters"] == {
        "tool_call": True,
        "vision": False,
        "reasoning": False,
        "open_weights": False,
        "min_context": 0,
        "providers": [],
        "providers_mode": "include",
        "hide_free": True,
        "credentialed_only": True,
    }


def test_catalogue_handler_forwards_explicit_filter_values(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(
        days=30,
        tool_call=False,
        vision=True,
        reasoning=True,
        open_weights=True,
        min_context=100_000,
        providers="Anthropic, OPENAI",
        providers_mode="exclude",
        hide_free=False,
        credentialed_only=False,
    )
    assert result["filters"] == {
        "tool_call": False,
        "vision": True,
        "reasoning": True,
        "open_weights": True,
        "min_context": 100_000,
        "providers": ["anthropic", "openai"],
        "providers_mode": "exclude",
        "hide_free": False,
        "credentialed_only": False,
    }


def test_catalogue_handler_clamps_a_negative_min_context_to_zero(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30, min_context=-500)
    assert result["filters"]["min_context"] == 0


def test_catalogue_handler_clamps_a_negative_offset_to_zero(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30, offset=-50)
    assert result["offset"] == 0


def test_catalogue_handler_whitelists_providers_mode_to_include_or_exclude(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30, providers_mode="sideways")
    assert result["filters"]["providers_mode"] == "include"


def test_catalogue_handler_hide_free_defaults_true(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30)
    assert result["filters"]["hide_free"] is True


# --- GET /catalogue credentialed_only (v0.2 Task 9) --------------------------


def test_catalogue_handler_credentialed_only_imposes_no_constraint_when_status_undeterminable(monkeypatch, tmp_path):
    # No mocking of pricing.credentialed_provider_slugs here: on this
    # development machine hermes_cli.auth genuinely isn't importable, so the
    # real function genuinely returns (set(), False) -- exercising the actual
    # fail-open path end to end, not a simulation of it. credentialed_only
    # defaults True but must impose NO constraint when status is
    # undeterminable, so only hide_free (also on by default) should have
    # removed anything from this fixture.
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text(json.dumps(CATALOGUE_MODELS_DEV))

    result = plugin_api.catalogue(days=30, tool_call=False, limit=100)

    models = {row["model"] for row in result["candidates"]}
    assert models == {"gpt-5.5", "z-ai/glm-5", "qwen/qwen3-32b"}  # free/model dropped by hide_free, not credentials
    assert result["credential_status_available"] is False


def test_catalogue_handler_applies_credentialed_only_using_the_computed_slugs(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text(json.dumps(CATALOGUE_MODELS_DEV))
    monkeypatch.setattr(plugin_api.pricing, "credentialed_provider_slugs", lambda: ({"openai"}, True))

    result = plugin_api.catalogue(days=30, tool_call=False, hide_free=False, limit=100)

    providers_seen = {row["provider"] for row in result["candidates"]}
    assert providers_seen == {"openai"}
    assert result["credential_status_available"] is True


def test_catalogue_handler_exposes_credential_sources_checked(monkeypatch, tmp_path):
    # Static regardless of credential_status_available -- names which local
    # stores the check consults, so credential_present: False (per-row, or
    # implicitly via credentialed_only) is never mistaken for "verified
    # absent from every possible credential store."
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.catalogue(days=30)

    assert result["credential_sources_checked"] == [
        "env_vars",
        "auth_store.credential_pool",
        "auth_store.providers",
    ]


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


# --- GET /providers facet (v0.2 Task 7) --------------------------------------

PROVIDERS_MODELS_DEV = {
    "openai": {
        "models": {
            "gpt-5.5": {"cost": {"input": 5, "output": 30}},
            "gpt-5.6": {"cost": {"input": 3, "output": 15}},
        }
    },
    "openrouter": {"models": {"z-ai/glm-5": {"cost": {"input": 0.95, "output": 2.55}}}},
    "anthropic": {"models": {"claude-x": {"cost": {"input": 2, "output": 10}}}},
}

PROVIDERS_USAGE = [
    ModelUsage(
        model="gpt-5.5",
        provider="openai-codex",
        sessions=10,
        usage=UsageVector(input_tokens=100, output_tokens=50),
    ),
    # A row with no billing_provider recorded: ghost_provider("") == "", and
    # it must never end up in the pinned list.
    ModelUsage(
        model="mystery",
        provider="",
        sessions=1,
        usage=UsageVector(),
    ),
]


def test_build_providers_counts_priced_models_per_provider():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    counts = {row["provider"]: row["model_count"] for row in result["providers"]}
    assert counts == {"openai": 2, "openrouter": 1, "anthropic": 1}


def test_build_providers_always_pins_openrouter_even_without_usage():
    # The user asked for this explicitly: openrouter is where the interesting
    # cheap alternatives live even though this usage window has no sessions
    # billed there.
    result = build_providers([], PROVIDERS_MODELS_DEV)

    assert "openrouter" in result["pinned"]


def test_build_providers_pins_billing_providers_via_ghost_provider():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    # "openai-codex" (the subscription billing route) maps through
    # pricing.ghost_provider to "openai", the paid API that actually serves
    # it -- pinned reflects the paid-API name, not the raw billing_provider.
    assert result["pinned"] == ["openai", "openrouter"]


def test_build_providers_drops_empty_provider_names_from_pinned():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    assert "" not in result["pinned"]


def test_build_providers_marks_the_pinned_flag_per_row():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    pinned_flags = {row["provider"]: row["pinned"] for row in result["providers"]}
    assert pinned_flags == {"openai": True, "openrouter": True, "anthropic": False}


def test_build_providers_sorts_pinned_first_then_by_count_desc_then_name():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    ordered = [row["provider"] for row in result["providers"]]
    # openai (pinned, count 2) before openrouter (pinned, count 1) before
    # anthropic (not pinned, count 1).
    assert ordered == ["openai", "openrouter", "anthropic"]


def test_build_providers_row_shape():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    for row in result["providers"]:
        assert set(row.keys()) == {"provider", "model_count", "pinned", "credential_present"}


# --- build_providers credential_present (v0.2 Task 9) ------------------------


def test_build_providers_marks_credential_present_per_row_when_status_is_available():
    result = build_providers(
        PROVIDERS_USAGE,
        PROVIDERS_MODELS_DEV,
        credentialed_provider_slugs={"openai"},
        credential_status_available=True,
    )

    flags = {row["provider"]: row["credential_present"] for row in result["providers"]}
    assert flags == {"openai": True, "openrouter": False, "anthropic": False}
    assert result["credential_status_available"] is True


def test_build_providers_credential_present_defaults_false_when_status_unavailable():
    # Mirrors build_catalogue's dangerous-failure-mode guard: when credential
    # status could not be determined, every row must read credential_present
    # False (never fabricate a "yes"), and the top-level flag makes clear
    # that False means "unknown", not "verified absent".
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    flags = {row["credential_present"] for row in result["providers"]}
    assert flags == {False}
    assert result["credential_status_available"] is False


def test_build_providers_credential_present_matched_case_insensitively():
    result = build_providers(
        PROVIDERS_USAGE,
        PROVIDERS_MODELS_DEV,
        credentialed_provider_slugs={"OpenAI"},
        credential_status_available=True,
    )

    flags = {row["provider"]: row["credential_present"] for row in result["providers"]}
    assert flags["openai"] is True


def test_build_providers_exposes_credential_sources_checked_so_false_never_reads_as_verified_absent():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    assert result["credential_sources_checked"] == [
        "env_vars",
        "auth_store.credential_pool",
        "auth_store.providers",
    ]


def test_build_providers_defaults_pricing_data_to_an_unavailable_placeholder():
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV)

    assert result["pricing_data"] == {"updated_at": None, "age_hours": None, "available": False}


def test_build_providers_passes_through_a_supplied_pricing_data():
    freshness = {"updated_at": "2026-07-27T00:00:00+00:00", "age_hours": 0.4, "available": True}
    result = build_providers(PROVIDERS_USAGE, PROVIDERS_MODELS_DEV, pricing_data=freshness)

    assert result["pricing_data"] == freshness


def test_build_providers_on_empty_cache_yields_no_providers_but_still_pins_openrouter():
    result = build_providers([], {})

    assert result["providers"] == []
    assert result["pinned"] == ["openrouter"]


def test_providers_handler_is_a_sync_def_not_async_def():
    import inspect

    import plugin_api

    assert not inspect.iscoroutinefunction(plugin_api.providers)


def test_providers_handler_clamps_an_oversized_days_value(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    captured = {}
    original_read_usage_window = plugin_api.store.read_usage_window

    def spy(db_path, days):
        captured["days"] = days
        return original_read_usage_window(db_path, days)

    monkeypatch.setattr(plugin_api.store, "read_usage_window", spy)

    plugin_api.providers(days=99999999)
    assert captured["days"] == 365


def test_providers_handler_computes_pricing_data_from_the_models_dev_cache_mtime(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text("{}")
    one_hour_ago = time.time() - 3600
    os.utime(cache, (one_hour_ago, one_hour_ago))

    result = plugin_api.providers(days=30)

    assert result["pricing_data"]["available"] is True
    assert result["pricing_data"]["age_hours"] == pytest.approx(1.0, abs=0.05)


def test_providers_handler_always_pins_openrouter(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.providers(days=30)
    assert "openrouter" in result["pinned"]


# --- GET /providers credential_present (v0.2 Task 9) --------------------------


def test_providers_handler_credential_present_false_and_status_unavailable_by_default(monkeypatch, tmp_path):
    # No mocking of pricing.credentialed_provider_slugs: on this development
    # machine hermes_cli.auth genuinely isn't importable, exercising the real
    # fail-open path. Every row must read credential_present False, and the
    # top-level flag must say the status is unavailable -- never let an
    # undeterminable status silently read as "verified nobody has a key".
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text(json.dumps(PROVIDERS_MODELS_DEV))

    result = plugin_api.providers(days=30)

    assert result["credential_status_available"] is False
    assert {row["credential_present"] for row in result["providers"]} == {False}


def test_providers_handler_applies_the_computed_credential_present_flags(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text(json.dumps(PROVIDERS_MODELS_DEV))
    monkeypatch.setattr(plugin_api.pricing, "credentialed_provider_slugs", lambda: ({"openai"}, True))

    result = plugin_api.providers(days=30)

    flags = {row["provider"]: row["credential_present"] for row in result["providers"]}
    assert flags == {"openai": True, "openrouter": False, "anthropic": False}
    assert result["credential_status_available"] is True


def test_providers_handler_exposes_credential_sources_checked(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)

    result = plugin_api.providers(days=30)

    assert result["credential_sources_checked"] == [
        "env_vars",
        "auth_store.credential_pool",
        "auth_store.providers",
    ]


# ---------------------------------------------------------------------------
# POST /switch-model
#
# The only endpoint that writes to the *host's* configuration. Every outcome
# below is pinned because a wrong one either changes a live agent's model
# without saying so, or reports success while nothing was written.
# ---------------------------------------------------------------------------


def _fake_switch_result(**kwargs):
    """A stand-in for hermes_cli.model_switch.ModelSwitchResult.

    Field names mirror the real dataclass at
    ``hermes_cli/model_switch.py:281-297``, verified against the host source.
    """
    result = MagicMock()
    result.success = kwargs.get("success", True)
    result.new_model = kwargs.get("new_model", "z-ai/glm-5")
    result.target_provider = kwargs.get("target_provider", "openrouter")
    result.base_url = kwargs.get("base_url", "")
    result.api_key = kwargs.get("api_key", "sk-SECRET-must-never-be-returned")
    result.error_message = kwargs.get("error_message", "")
    result.warning_message = kwargs.get("warning_message", "")
    result.model_info = kwargs.get("model_info", None)
    return result


def _install_fake_hermes_cli(monkeypatch, *, switch_result=None, switch_raises=None,
                             warning=None, guard_raises=None, raw=None, managed=False,
                             load_raises=None, save_raises=None, save_declines=False,
                             home=None):
    """Inject a fake ``hermes_cli`` tree modelling the raw config file on disk.

    ``disk`` stands in for ``config.yaml``: ``read_raw_config`` returns a copy of
    it and ``save_config`` replaces it, so the endpoint's post-write read-back is
    actually exercised. ``save_declines=True`` reproduces the managed/pinned-key
    case where the host writes nothing and raises nothing.
    """
    disk: dict = raw if raw is not None else {
        "model": {"default": "gpt-5.6-terra", "provider": "openai-codex"},
        "other": "untouched",
    }
    state = {"disk": json.loads(json.dumps(disk))}

    # The endpoint refuses to switch without first copying config.yaml aside,
    # so a fake host needs a real file on disk for that copy to succeed.
    # Tests that assert on the backup itself pass their own `home`.
    import plugin_api as _api
    import tempfile as _tempfile
    from pathlib import Path as _Path

    if home is None:
        home = _Path(_tempfile.mkdtemp())
        cfg = home / "config.yaml"
        cfg.write_text("model:\n  default: gpt-5.6-terra\n")
        cfg.chmod(0o600)
    monkeypatch.setattr(_api.paths, "hermes_home", lambda: home)

    fake_root = MagicMock()
    fake_switch = MagicMock()
    fake_config = MagicMock()
    fake_guard = MagicMock()

    if switch_raises is not None:
        fake_switch.switch_model = MagicMock(side_effect=switch_raises)
    else:
        fake_switch.switch_model = MagicMock(return_value=switch_result or _fake_switch_result())

    def _read_raw():
        return json.loads(json.dumps(state["disk"]))

    def _save(cfg):
        if save_declines:
            return None  # the host declines silently — no write, no exception
        state["disk"] = json.loads(json.dumps(cfg))

    if load_raises is not None:
        fake_config.read_raw_config = MagicMock(side_effect=load_raises)
    else:
        fake_config.read_raw_config = MagicMock(side_effect=_read_raw)
    fake_config.load_config = MagicMock(side_effect=lambda: {"providers": {"p": 1}})
    fake_config.get_compatible_custom_providers = MagicMock(return_value=["custom"])
    fake_config.is_managed = MagicMock(return_value=managed)
    if save_raises is not None:
        fake_config.save_config = MagicMock(side_effect=save_raises)
    else:
        fake_config.save_config = MagicMock(side_effect=_save)

    if guard_raises is not None:
        fake_guard.expensive_model_warning = MagicMock(side_effect=guard_raises)
    else:
        fake_guard.expensive_model_warning = MagicMock(return_value=warning)

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_root)
    monkeypatch.setitem(sys.modules, "hermes_cli.model_switch", fake_switch)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setitem(sys.modules, "hermes_cli.model_cost_guard", fake_guard)
    return state, fake_switch, fake_config


def test_switch_model_reports_not_importable_instead_of_crashing(monkeypatch):
    import plugin_api

    # Never let this reach a real hermes_cli: on a machine where Hermes IS
    # installed - i.e. every machine this plugin actually runs on - an
    # unguarded call would write to the developer's live config.yaml.
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is False
    assert "importable" in (result["detail"] or "")
    assert result.get("confirm_required") is not True


def test_switch_model_requires_a_model_and_writes_nothing(monkeypatch):
    import plugin_api

    state, _, fake_config = _install_fake_hermes_cli(monkeypatch)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "   "})

    assert result["ok"] is False
    assert "model" in (result["detail"] or "").lower()
    fake_config.save_config.assert_not_called()


def test_switch_model_persists_and_returns_the_previous_model_for_reversal(monkeypatch):
    import plugin_api

    state, fake_switch, fake_config = _install_fake_hermes_cli(monkeypatch)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is True
    # The change must be reversible from the response alone.
    assert result["previous"] == {"model": "gpt-5.6-terra", "provider": "openai-codex", "base_url": ""}
    assert result["current"] == {"model": "z-ai/glm-5", "provider": "openrouter"}
    # Persisted through the host's public save_config, mirroring _persist_model_switch.
    assert state["disk"]["model"]["default"] == "z-ai/glm-5"
    assert state["disk"]["model"]["provider"] == "openrouter"
    assert state["disk"]["other"] == "untouched"
    # The resolver must receive the user-defined providers, or it can re-resolve
    # from scratch and persist a base_url pointing at the wrong endpoint.
    kwargs = fake_switch.switch_model.call_args.kwargs
    assert kwargs["user_providers"] == {"p": 1}
    assert kwargs["custom_providers"] == ["custom"]
    assert kwargs["current_base_url"] == ""
    assert kwargs["is_global"] is True


def test_switch_model_never_returns_the_api_key(monkeypatch):
    import plugin_api

    _install_fake_hermes_cli(monkeypatch)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert "SECRET" not in json.dumps(result)
    assert "api_key" not in json.dumps(result)


def test_switch_model_surfaces_the_hosts_advisory_warning_verbatim(monkeypatch):
    import plugin_api

    _install_fake_hermes_cli(
        monkeypatch,
        switch_result=_fake_switch_result(warning_message="not found in the public listing"),
    )

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "who/knows"})

    assert result["ok"] is True
    # A switch the host could not confirm must not read as a plain success.
    assert result["warning"] == "not found in the public listing"


def test_switch_model_refuses_when_the_host_refuses(monkeypatch):
    import plugin_api

    state, _, fake_config = _install_fake_hermes_cli(
        monkeypatch,
        switch_result=_fake_switch_result(success=False, error_message="unknown provider"),
    )

    result = plugin_api.switch_model_endpoint({"provider": "nope", "model": "x"})

    assert result["ok"] is False
    assert result["detail"] == "unknown provider"
    fake_config.save_config.assert_not_called()


def test_switch_model_honours_the_hosts_expensive_model_guard_and_writes_nothing(monkeypatch):
    import plugin_api

    guard = MagicMock()
    guard.message = "gpt-9 costs $150/M output"
    state, _, fake_config = _install_fake_hermes_cli(monkeypatch, warning=guard)

    result = plugin_api.switch_model_endpoint({"provider": "openai", "model": "gpt-9"})

    assert result["confirm_required"] is True
    assert result["ok"] is False
    assert result["confirm_message"] == "gpt-9 costs $150/M output"
    fake_config.save_config.assert_not_called()


def test_switch_model_confirm_expensive_proceeds_past_the_guard(monkeypatch):
    import plugin_api

    guard = MagicMock()
    guard.message = "expensive"
    state, _, fake_config = _install_fake_hermes_cli(monkeypatch, warning=guard)

    result = plugin_api.switch_model_endpoint(
        {"provider": "openai", "model": "gpt-9", "confirm_expensive": True}
    )

    assert result["ok"] is True
    fake_config.save_config.assert_called_once()


def test_switch_model_reports_a_save_failure_rather_than_claiming_success(monkeypatch):
    import plugin_api

    _install_fake_hermes_cli(monkeypatch, save_raises=OSError("read-only filesystem"))

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is False
    assert "read-only filesystem" in (result["detail"] or "")


def test_switch_model_is_a_sync_def_not_async_def():
    import inspect

    import plugin_api

    assert inspect.iscoroutinefunction(plugin_api.switch_model_endpoint) is False


def test_switch_model_refuses_on_a_managed_install_instead_of_claiming_success(monkeypatch):
    import plugin_api

    # save_config returns None without raising under is_managed()
    # (hermes_cli/config.py:5831-5833), so writing first and trusting the
    # absence of an exception would report a switch that never happened.
    state, _, fake_config = _install_fake_hermes_cli(monkeypatch, managed=True)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is False
    assert "managed" in (result["detail"] or "")
    fake_config.save_config.assert_not_called()
    assert state["disk"]["model"]["default"] == "gpt-5.6-terra"


def test_switch_model_detects_a_silently_declined_write(monkeypatch):
    import plugin_api

    state, _, _ = _install_fake_hermes_cli(monkeypatch, save_declines=True)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is False
    assert "refused" in (result["detail"] or "")
    assert state["disk"]["model"]["default"] == "gpt-5.6-terra"


def test_switch_model_reads_the_raw_config_never_the_merged_one(monkeypatch):
    import plugin_api

    state, _, fake_config = _install_fake_hermes_cli(monkeypatch)

    plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    # load_config() deep-merges DEFAULT_CONFIG and stamps _config_version;
    # saving its result back would pin every default into the user's file and
    # permanently skip future migrations. It may only be read for providers.
    saved_cfg = fake_config.save_config.call_args.args[0]
    assert "_config_version" not in saved_cfg
    assert set(saved_cfg) == {"model", "other"}


def test_switch_model_handles_a_scalar_model_key(monkeypatch):
    import plugin_api

    # The host also accepts `model: <name>` as a bare string
    # (tui_gateway/server.py:2321-2322); collapsing it to {} would feed
    # switch_model an empty current model and lose the previous value.
    state, fake_switch, _ = _install_fake_hermes_cli(
        monkeypatch, raw={"model": "gpt-5.6-terra", "other": "untouched"}
    )

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["previous"]["model"] == "gpt-5.6-terra"
    assert fake_switch.switch_model.call_args.args[2] == "gpt-5.6-terra"
    assert result["ok"] is True


def test_switch_model_sets_base_url_when_the_resolver_returns_one(monkeypatch):
    import plugin_api

    state, _, _ = _install_fake_hermes_cli(
        monkeypatch, switch_result=_fake_switch_result(base_url="https://example.invalid/v1")
    )

    plugin_api.switch_model_endpoint({"provider": "custom", "model": "z-ai/glm-5"})

    assert state["disk"]["model"]["base_url"] == "https://example.invalid/v1"


def test_switch_model_pops_a_stale_base_url_when_the_resolver_returns_none(monkeypatch):
    import plugin_api

    # A base_url left over from the previous provider would silently point the
    # new model at the wrong endpoint - a 401 much later, far from the cause.
    state, _, _ = _install_fake_hermes_cli(
        monkeypatch,
        raw={"model": {"default": "old", "provider": "lmstudio", "base_url": "http://127.0.0.1:1234/v1"}},
    )

    plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert "base_url" not in state["disk"]["model"]


def test_switch_model_reports_when_the_cost_guard_could_not_run(monkeypatch):
    import plugin_api

    state, _, _ = _install_fake_hermes_cli(monkeypatch, guard_raises=RuntimeError("guard exploded"))

    result = plugin_api.switch_model_endpoint({"provider": "openai", "model": "gpt-9"})

    # The switch still proceeds, as the gateway does - but a caller must be
    # able to tell the only brake never engaged.
    assert result["ok"] is True
    assert result["guard_ran"] is False


def test_switch_model_never_returns_the_api_key_on_the_confirm_branch(monkeypatch):
    import plugin_api

    guard = MagicMock()
    guard.message = "expensive"
    _install_fake_hermes_cli(monkeypatch, warning=guard)

    result = plugin_api.switch_model_endpoint({"provider": "openai", "model": "gpt-9"})

    assert result["confirm_required"] is True
    assert "SECRET" not in json.dumps(result)


def test_switch_model_tolerates_a_non_dict_body_and_odd_types(monkeypatch):
    import plugin_api

    _install_fake_hermes_cli(monkeypatch)

    for body in (None, [], "nope", {"model": 123}, {"model": {"x": 1}}):
        result = plugin_api.switch_model_endpoint(body)
        assert result["ok"] is False
        assert result["detail"]


def test_switch_model_detects_a_pinned_provider_that_was_silently_stripped(monkeypatch):
    import plugin_api

    # Managed *scope* is per-key and is distinct from is_managed()
    # (hermes_cli/managed_scope.py:7-11 — "the two are independent and may
    # coexist"). An admin pinning model.provider while leaving model.default
    # writable is the natural "any model you like, but only through our
    # gateway" policy: save_config strips the pinned leaf and notes it to
    # stderr, which no HTTP caller ever sees. The host *deletes* the leaf
    # (config.py:5294) and writes the pruned dict wholesale; this fake keeps
    # the old value instead, which is the strictly harder case for the
    # read-back to catch.
    state, _, fake_config = _install_fake_hermes_cli(monkeypatch)

    def _save_stripping_provider(cfg):
        pinned = state["disk"]["model"].get("provider")
        state["disk"] = json.loads(json.dumps(cfg))
        state["disk"]["model"]["provider"] = pinned  # the pin survives the write

    fake_config.save_config = MagicMock(side_effect=_save_stripping_provider)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    # Reporting ok here would misstate the provider AND leave the config
    # pairing a new model id with the old endpoint — a 401 much later.
    assert result["ok"] is False
    assert "provider" in (result["detail"] or "")
    assert result["previous"]["provider"] == "openai-codex"


def test_switch_model_accepts_an_env_template_base_url_as_a_match(monkeypatch):
    import plugin_api

    # save_config legitimately restores a ${VAR} template over the expanded
    # value it was handed (_preserve_env_ref_templates), so a template on disk
    # is a match, not a refusal.
    state, _, fake_config = _install_fake_hermes_cli(
        monkeypatch, switch_result=_fake_switch_result(base_url="https://expanded.invalid/v1")
    )

    def _save_restoring_template(cfg):
        state["disk"] = json.loads(json.dumps(cfg))
        state["disk"]["model"]["base_url"] = "${MY_ENDPOINT}"

    fake_config.save_config = MagicMock(side_effect=_save_restoring_template)

    result = plugin_api.switch_model_endpoint({"provider": "custom", "model": "z-ai/glm-5"})

    assert result["ok"] is True


def test_switch_model_reports_guard_not_run_when_the_caller_skipped_it(monkeypatch):
    import plugin_api

    guard = MagicMock()
    guard.message = "expensive"
    _install_fake_hermes_cli(monkeypatch, warning=guard)

    result = plugin_api.switch_model_endpoint(
        {"provider": "openai", "model": "gpt-9", "confirm_expensive": True}
    )

    assert result["ok"] is True
    assert result["guard_ran"] is False


def test_switch_model_detects_a_pinned_base_url_that_was_silently_stripped(monkeypatch):
    import plugin_api

    # The leaf whose silent loss motivated the whole finding: a new model id
    # left pointing at the old endpoint surfaces as a 401 far from its cause.
    state, _, fake_config = _install_fake_hermes_cli(
        monkeypatch, switch_result=_fake_switch_result(base_url="https://openrouter.invalid/v1")
    )

    def _save_stripping_base_url(cfg):
        state["disk"] = json.loads(json.dumps(cfg))
        state["disk"]["model"]["base_url"] = "https://corp-gateway.invalid/v1"  # the pin survives

    fake_config.save_config = MagicMock(side_effect=_save_stripping_base_url)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is False
    assert "base_url" in (result["detail"] or "")


def test_switch_model_detects_a_whole_file_decline_the_leaf_checks_would_miss(monkeypatch):
    import plugin_api

    # When only base_url is changing and the on-disk value is a ${VAR} template,
    # the per-leaf checks all tolerate it - so the file itself is compared too.
    state, _, _ = _install_fake_hermes_cli(
        monkeypatch,
        raw={"model": {"default": "z-ai/glm-5", "provider": "openrouter", "base_url": "${OLD_GATEWAY}"}},
        switch_result=_fake_switch_result(base_url="https://openrouter.invalid/v1"),
        save_declines=True,
    )

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is False
    assert "unchanged" in (result["detail"] or "")


def test_build_summary_reports_rows_it_could_not_price_at_all():
    from hermes_cost_arbitrage_dashboard.cost_engine import UsageVector
    from hermes_cost_arbitrage_dashboard.store import ModelUsage

    import plugin_api

    # A NULL billing_provider is coalesced to "" by the store's query and
    # resolves to no grid, so this row prices to nothing while its tokens still
    # land in `totals`. Dropping it from the headline without a word is the
    # same silent $0 the plugin exists to replace - and it flatters the
    # subscription, which is the direction that matters.
    rows = [
        ModelUsage(model="gpt-5.5", provider="openai-codex", sessions=1, api_call_count=1,
                   usage=UsageVector(input_tokens=1_000_000, output_tokens=100_000)),
        ModelUsage(model="mystery", provider="", sessions=1, api_call_count=1,
                   usage=UsageVector(input_tokens=500_000, output_tokens=50_000)),
    ]
    md = {"openai": {"models": {"gpt-5.5": {"cost": {"input": 5, "output": 30}}}}}

    summary = plugin_api.build_summary(rows, md, subscription_usd=23.0, days=30)

    assert summary["unpriced"]["models"] == 1
    assert summary["unpriced"]["tokens"] == 550_000
    assert summary["unpriced"]["affects_total"] is True
    # The tokens are counted in totals but not in the headline - which is
    # exactly why the caller has to be told.
    assert summary["totals"]["input_tokens"] == 1_500_000
    assert summary["ghost_cost_usd"] == 8.0


def test_build_summary_does_not_warn_for_unpriced_rows_carrying_no_tokens():
    from hermes_cost_arbitrage_dashboard.cost_engine import UsageVector
    from hermes_cost_arbitrage_dashboard.store import ModelUsage

    import plugin_api

    # Phantom session-only rows carry zero tokens and exclude nothing from the
    # headline. Warning on them would spend the caveat's credibility - measured
    # on a live host, all 3 unpriced rows were of exactly this kind.
    rows = [
        ModelUsage(model="gpt-5.5", provider="openai-codex", sessions=1, api_call_count=1,
                   usage=UsageVector(input_tokens=1_000_000)),
        ModelUsage(model="phantom", provider="", sessions=3, api_call_count=0,
                   usage=UsageVector()),
    ]
    md = {"openai": {"models": {"gpt-5.5": {"cost": {"input": 5, "output": 30}}}}}

    summary = plugin_api.build_summary(rows, md, subscription_usd=23.0, days=30)

    assert summary["unpriced"]["models"] == 1
    assert summary["unpriced"]["tokens"] == 0
    assert summary["unpriced"]["affects_total"] is False


def test_build_summary_reports_no_unpriced_rows_when_everything_resolved():
    from hermes_cost_arbitrage_dashboard.cost_engine import UsageVector
    from hermes_cost_arbitrage_dashboard.store import ModelUsage

    import plugin_api

    rows = [ModelUsage(model="gpt-5.5", provider="openai-codex", sessions=1, api_call_count=1,
                       usage=UsageVector(input_tokens=1_000_000))]
    md = {"openai": {"models": {"gpt-5.5": {"cost": {"input": 5, "output": 30}}}}}

    summary = plugin_api.build_summary(rows, md, subscription_usd=23.0, days=30)

    assert summary["unpriced"] == {"models": 0, "tokens": 0, "affects_total": False}


def test_refresh_pricing_reports_failure_when_the_cache_did_not_move(monkeypatch, tmp_path):
    import plugin_api

    _patch_context_paths(monkeypatch, plugin_api, tmp_path)
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text("{}")

    fake_agent = MagicMock()
    fake_models_dev = MagicMock()
    # fetch_models_dev swallows its own network errors and returns whatever is
    # in memory, so "it returned" is not evidence of a refresh.
    fake_models_dev.fetch_models_dev = MagicMock(return_value={})
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.models_dev", fake_models_dev)

    result = plugin_api.refresh_pricing()

    assert result["ok"] is False
    assert "did not change" in (result["detail"] or "")


def test_dashboard_bundle_loads_and_renders():
    """Evaluate dist/index.js against a stubbed SDK and render every component.

    `node --check` parses without executing, so it cannot see a `const` read
    before its declaration — valid syntax, ReferenceError at load. Two such
    errors shipped together in one commit; the first threw before
    REGISTRY.register ran and took the whole /cost tab off the dashboard.
    A third was introduced by the fix for the second and caught by this test.

    Wired into pytest rather than left as a loose script so it cannot be
    forgotten: this repo has no JavaScript test framework, and nothing else
    here executes the bundle at all.
    """
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    smoke = Path(__file__).resolve().parent / "bundle_smoke.mjs"
    result = subprocess.run([node, str(smoke)], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, (result.stdout + result.stderr)
    assert "BUNDLE SMOKE OK" in result.stdout


def test_switch_model_backs_up_the_config_before_writing(monkeypatch, tmp_path):
    import plugin_api

    # Hermes writes no backup of its own: save_config and atomic_yaml_write go
    # straight to a temp file and os.replace, and migrate_config rewrites the
    # whole file with no snapshot either. Atomicity protects an interrupted
    # write, never a successful but unwanted one.
    monkeypatch.setattr(plugin_api.paths, "hermes_home", lambda: tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: gpt-5.6-terra\n")
    cfg.chmod(0o600)

    _install_fake_hermes_cli(monkeypatch, home=tmp_path)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    backups = list(tmp_path.glob("config.yaml.bak-pre-switch-*"))
    assert result["ok"] is True
    assert len(backups) == 1
    assert backups[0].read_text() == "model:\n  default: gpt-5.6-terra\n"
    # config.yaml is 0600 and holds provider credentials; a world-readable
    # backup would be a regression rather than a protection.
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert result["backup"] == str(backups[0])


def test_switch_model_refuses_when_the_backup_cannot_be_made(monkeypatch, tmp_path):
    import plugin_api

    # The one deliberate departure from this plugin's fail-open discipline:
    # a caller who cannot get a net does not get to jump.
    monkeypatch.setattr(plugin_api.paths, "hermes_home", lambda: tmp_path)
    # No config.yaml on disk at all.
    _, _, fake_config = _install_fake_hermes_cli(monkeypatch, home=tmp_path)

    result = plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    assert result["ok"] is False
    assert "without a backup" in (result["detail"] or "")
    fake_config.save_config.assert_not_called()


def test_switch_model_prunes_old_backups(monkeypatch, tmp_path):
    import plugin_api

    monkeypatch.setattr(plugin_api.paths, "hermes_home", lambda: tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: old\n")
    for i in range(15):
        (tmp_path / f"config.yaml.bak-pre-switch-2026010{i % 10}T00000{i % 10}Z").write_text("stale")

    _install_fake_hermes_cli(monkeypatch, home=tmp_path)
    plugin_api.switch_model_endpoint({"provider": "openrouter", "model": "z-ai/glm-5"})

    # Every click writes one; without a cap a frequently-used button quietly
    # fills HERMES_HOME.
    assert len(list(tmp_path.glob("config.yaml.bak-pre-switch-*"))) == plugin_api.CONFIG_BACKUP_KEEP
