import json
import sys
from decimal import Decimal
from unittest.mock import MagicMock

from hermes_cost_arbitrage_dashboard.pricing import ghost_provider, load_models_dev, resolve_grid

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
