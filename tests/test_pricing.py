import json
from decimal import Decimal

from pricing import ghost_provider, load_models_dev, resolve_grid

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
