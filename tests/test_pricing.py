import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cost_arbitrage_dashboard.pricing import (
    CatalogueCapabilities,
    CatalogueEntry,
    ghost_provider,
    iter_catalogue,
    load_models_dev,
    models_dev_freshness,
    resolve_grid,
)

MODELS_DEV_FIXTURE = {
    "openai": {
        "models": {
            "gpt-5.5": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}},
        }
    },
    "openrouter": {
        "models": {
            "z-ai/glm-5": {"cost": {"input": 0.95, "output": 2.55, "cache_read": 0.2}},
            "qwen/qwen3-32b": {"cost": {"input": 0.08, "output": 0.28}},
        }
    },
}


def test_subscription_provider_is_rewritten_to_its_paid_equivalent():
    # The whole point: openai-codex is a subscription route and Hermes prices
    # it at zero. Ghost costing must ask the paid API's rates instead.
    assert ghost_provider("openai-codex") == "openai"
    assert ghost_provider("OpenAI-Codex") == "openai"


def test_non_subscription_providers_pass_through_unchanged():
    assert ghost_provider("anthropic") == "anthropic"
    assert ghost_provider("openrouter") == "openrouter"
    assert ghost_provider(None) == ""


def test_resolve_grid_never_returns_a_zero_grid_for_a_subscription_model():
    grid = resolve_grid("gpt-5.5", "openai-codex", MODELS_DEV_FIXTURE)

    assert grid.input_per_million == Decimal("5")
    assert grid.output_per_million == Decimal("30")
    assert grid.cache_read_per_million == Decimal("0.5")
    assert grid.is_priced


def test_resolve_grid_reads_openrouter_models_offline():
    grid = resolve_grid("z-ai/glm-5", "openrouter", MODELS_DEV_FIXTURE)

    assert grid.input_per_million == Decimal("0.95")
    assert grid.cache_read_per_million == Decimal("0.2")


def test_model_without_cache_rate_yields_a_grid_flagged_as_uncached():
    grid = resolve_grid("qwen/qwen3-32b", "openrouter", MODELS_DEV_FIXTURE)

    assert grid.is_priced
    assert not grid.has_cache_pricing


def test_unknown_model_yields_an_unpriced_grid_not_an_exception():
    grid = resolve_grid("does-not-exist", "openrouter", MODELS_DEV_FIXTURE)

    assert not grid.is_priced
    assert grid.source == "unknown"


def test_load_models_dev_is_fail_open(tmp_path):
    assert load_models_dev(tmp_path / "absent.json") == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert load_models_dev(broken) == {}

    good = tmp_path / "good.json"
    good.write_text(json.dumps(MODELS_DEV_FIXTURE))
    assert "openai" in load_models_dev(good)


def test_malformed_models_dev_cache_degrades_gracefully():
    # First malformed shape: provider data is a list instead of dict
    grid = resolve_grid("gpt-5.5", "openai", {"openai": ["not", "a", "dict"]})
    assert not grid.is_priced
    assert grid.source == "unknown"

    # Second malformed shape: cost is a list instead of dict
    grid = resolve_grid(
        "gpt-5.5",
        "openai",
        {"openai": {"models": {"gpt-5.5": {"cost": [5, 30]}}}},
    )
    assert not grid.is_priced
    assert grid.source == "unknown"


def test_resolve_grid_with_zero_hermes_entry_falls_back_to_models_dev(monkeypatch):
    # Inject a fake agent.usage_pricing module with an all-zero entry.
    # This tests the guard that stops a subscription route's zero grid from
    # leaking through Hermes' own pricing table.
    fake_agent = MagicMock()
    fake_usage_pricing = MagicMock()

    fake_entry = MagicMock()
    fake_entry.input_cost_per_million = Decimal("0")
    fake_entry.output_cost_per_million = Decimal("0")
    fake_entry.cache_read_cost_per_million = None
    fake_entry.cache_write_cost_per_million = None
    fake_entry.source = "hermes"

    fake_usage_pricing.get_pricing_entry = MagicMock(return_value=fake_entry)

    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", fake_usage_pricing)

    # Call resolve_grid with openai-codex — should rewrite to openai and
    # fall back to models.dev because Hermes returns zero.
    grid = resolve_grid("gpt-5.5", "openai-codex", MODELS_DEV_FIXTURE)

    # Should return models.dev rates, not zero
    assert grid.input_per_million == Decimal("5")
    assert grid.output_per_million == Decimal("30")
    assert grid.source == "models.dev"
    assert grid.is_priced


def test_resolve_grid_treats_a_literal_none_source_as_absent_not_as_a_label(monkeypatch):
    # PricingEntry.source defaults to the literal string "none" in Hermes'
    # own pricing table (agent.usage_pricing.PricingEntry), not to None or
    # "". That string is truthy, so `entry.source or "hermes"` would let it
    # pass straight through to the UI as a pricing source literally named
    # "none" instead of falling back to "hermes".
    fake_agent = MagicMock()
    fake_usage_pricing = MagicMock()

    fake_entry = MagicMock()
    fake_entry.input_cost_per_million = Decimal("10")
    fake_entry.output_cost_per_million = Decimal("60")
    fake_entry.cache_read_cost_per_million = Decimal("1")
    fake_entry.cache_write_cost_per_million = None
    fake_entry.source = "none"

    fake_usage_pricing.get_pricing_entry = MagicMock(return_value=fake_entry)

    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", fake_usage_pricing)

    grid = resolve_grid("gpt-5.5", "openai", MODELS_DEV_FIXTURE)

    assert grid.source == "hermes"


def test_resolve_grid_prefers_hermes_over_models_dev_when_hermes_is_priced(
    monkeypatch,
):
    # Inject a fake agent.usage_pricing module with good pricing.
    # This verifies the zero-grid guard is not disabling Hermes wholesale.
    fake_agent = MagicMock()
    fake_usage_pricing = MagicMock()

    fake_entry = MagicMock()
    fake_entry.input_cost_per_million = Decimal("10")
    fake_entry.output_cost_per_million = Decimal("60")
    fake_entry.cache_read_cost_per_million = Decimal("1")
    fake_entry.cache_write_cost_per_million = None
    fake_entry.source = "hermes"

    fake_usage_pricing.get_pricing_entry = MagicMock(return_value=fake_entry)

    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", fake_usage_pricing)

    # Call resolve_grid — Hermes should be preferred over models.dev
    grid = resolve_grid("gpt-5.5", "openai", MODELS_DEV_FIXTURE)

    # Should return Hermes rates, not models.dev
    assert grid.input_per_million == Decimal("10")
    assert grid.output_per_million == Decimal("60")
    assert grid.cache_read_per_million == Decimal("1")
    assert grid.source == "hermes"
    assert grid.is_priced


def test_iter_catalogue_yields_every_priced_provider_model_pair():
    # iter_catalogue yields CatalogueEntry(provider, model, grid, capabilities)
    # rather than a bare tuple (a deliberate v0.2 Task 5 signature change) —
    # this test's intent (every priced provider/model pair is walked) is
    # unchanged, only the access to provider/model moves from tuple
    # unpacking to attribute access.
    pairs = {(entry.provider, entry.model) for entry in iter_catalogue(MODELS_DEV_FIXTURE)}

    assert pairs == {
        ("openai", "gpt-5.5"),
        ("openrouter", "z-ai/glm-5"),
        ("openrouter", "qwen/qwen3-32b"),
    }


def test_iter_catalogue_yields_grids_matching_resolve_grid():
    grids = {(entry.provider, entry.model): entry.grid for entry in iter_catalogue(MODELS_DEV_FIXTURE)}

    grid = grids[("openai", "gpt-5.5")]
    assert grid.input_per_million == Decimal("5")
    assert grid.output_per_million == Decimal("30")
    assert grid.cache_read_per_million == Decimal("0.5")
    assert grid.is_priced


def test_iter_catalogue_entries_are_catalogue_entry_instances():
    entries = list(iter_catalogue(MODELS_DEV_FIXTURE))

    assert entries
    assert all(isinstance(entry, CatalogueEntry) for entry in entries)
    assert all(isinstance(entry.capabilities, CatalogueCapabilities) for entry in entries)


def test_iter_catalogue_skips_unpriced_models():
    data = {"openrouter": {"models": {"free/model": {"cost": {}}}}}

    assert list(iter_catalogue(data)) == []


def test_iter_catalogue_on_empty_or_non_dict_cache_yields_nothing():
    assert list(iter_catalogue({})) == []
    assert list(iter_catalogue(None)) == []  # type: ignore[arg-type]


def test_iter_catalogue_skips_malformed_branches_without_raising():
    # Mirrors the malformed shapes in test_malformed_models_dev_cache_degrades_
    # gracefully above, but across a whole cache walk: one corrupt provider or
    # model must not stop the rest of the catalogue from yielding.
    malformed = {
        "openai": {"models": {"gpt-5.5": {"cost": {"input": 5, "output": 30}}}},
        "provider-is-a-list": ["not", "a", "dict"],
        "models-is-not-a-dict": {"models": "not-a-dict"},
        "cost-is-a-list": {"models": {"bad-model": {"cost": [1, 2, 3]}}},
        "model-entry-is-not-a-dict": {"models": {"bad-model-2": ["not", "a", "dict"]}},
    }

    pairs = {(entry.provider, entry.model) for entry in iter_catalogue(malformed)}

    assert pairs == {("openai", "gpt-5.5")}


# --- capability extraction --------------------------------------------------

CAPABLE_MODELS_DEV = {
    "openai": {
        "models": {
            "gpt-5.5": {
                "cost": {"input": 5, "output": 30, "cache_read": 0.5},
                "tool_call": True,
                "reasoning": True,
                "attachment": True,
                "open_weights": False,
                "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                "limit": {"context": 1050000, "input": 922000, "output": 128000},
            },
            # No capability fields at all: every one must fail open to
            # False/None rather than raise.
            "gpt-bare": {"cost": {"input": 1, "output": 2}},
            # Malformed shapes: wrong types at every capability field.
            "gpt-malformed": {
                "cost": {"input": 1, "output": 2},
                "tool_call": "yes",  # not a bool
                "reasoning": None,
                "modalities": ["not", "a", "dict"],
                "limit": "not-a-dict",
            },
            "gpt-bad-context": {
                "cost": {"input": 1, "output": 2},
                "limit": {"context": "not-a-number"},
            },
            "gpt-text-only": {
                "cost": {"input": 1, "output": 2},
                "modalities": {"input": ["text"], "output": ["text"]},
            },
        }
    }
}


def test_iter_catalogue_extracts_all_capability_fields_for_a_fully_populated_entry():
    entries = {entry.model: entry.capabilities for entry in iter_catalogue(CAPABLE_MODELS_DEV)}

    caps = entries["gpt-5.5"]
    assert caps.tool_call is True
    assert caps.reasoning is True
    assert caps.open_weights is False
    assert caps.vision is True  # "image" in modalities.input
    assert caps.context_limit == 1050000


def test_iter_catalogue_vision_is_derived_from_modalities_input_not_a_vision_field():
    entries = {entry.model: entry.capabilities for entry in iter_catalogue(CAPABLE_MODELS_DEV)}

    assert entries["gpt-text-only"].vision is False
    assert entries["gpt-5.5"].vision is True


def test_iter_catalogue_capabilities_fail_open_on_missing_fields():
    entries = {entry.model: entry.capabilities for entry in iter_catalogue(CAPABLE_MODELS_DEV)}

    bare = entries["gpt-bare"]
    assert bare.tool_call is False
    assert bare.reasoning is False
    assert bare.open_weights is False
    assert bare.vision is False
    assert bare.context_limit is None


def test_iter_catalogue_capabilities_fail_open_on_malformed_fields_never_raises():
    entries = {entry.model: entry.capabilities for entry in iter_catalogue(CAPABLE_MODELS_DEV)}

    malformed = entries["gpt-malformed"]
    assert malformed.tool_call is False
    assert malformed.reasoning is False
    assert malformed.vision is False
    assert malformed.context_limit is None


def test_iter_catalogue_capabilities_fail_open_on_a_non_numeric_context_limit():
    entries = {entry.model: entry.capabilities for entry in iter_catalogue(CAPABLE_MODELS_DEV)}

    assert entries["gpt-bad-context"].context_limit is None


# --- models_dev_freshness ---------------------------------------------------


def test_models_dev_freshness_on_a_missing_file_is_fail_open(tmp_path):
    result = models_dev_freshness(tmp_path / "absent.json")

    assert result == {"updated_at": None, "age_hours": None, "available": False}


def test_models_dev_freshness_reads_the_cache_files_mtime(tmp_path):
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text("{}")
    one_hour_ago = time.time() - 3600
    os.utime(cache, (one_hour_ago, one_hour_ago))

    result = models_dev_freshness(cache)

    assert result["available"] is True
    assert result["age_hours"] == pytest.approx(1.0, abs=0.01)
    assert result["updated_at"] is not None
    # Must be a genuine ISO 8601 string, not just any truthy value.
    datetime.fromisoformat(result["updated_at"])


def test_models_dev_freshness_is_fail_open_on_a_permission_error(tmp_path, monkeypatch):
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text("{}")

    def _raise(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "stat", _raise)

    result = models_dev_freshness(cache)

    assert result == {"updated_at": None, "age_hours": None, "available": False}


def test_models_dev_freshness_treats_a_future_mtime_as_a_clock_anomaly(tmp_path):
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text("{}")
    far_future = time.time() + 100_000
    os.utime(cache, (far_future, far_future))

    result = models_dev_freshness(cache)

    assert result == {"updated_at": None, "age_hours": None, "available": False}


def test_models_dev_freshness_never_raises_on_a_non_path_like_input():
    # A caller passing something that isn't a valid path (e.g. an object
    # Path() can't coerce) must still degrade rather than crash.
    result = models_dev_freshness(object())  # type: ignore[arg-type]

    assert result == {"updated_at": None, "age_hours": None, "available": False}
