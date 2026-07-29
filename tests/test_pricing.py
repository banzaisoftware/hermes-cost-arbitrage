import json
import os
import sys
import time
import types
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cost_arbitrage_dashboard.pricing import (
    CatalogueCapabilities,
    CatalogueEntry,
    credentialed_provider_slugs,
    ghost_provider,
    iter_catalogue,
    load_models_dev,
    models_dev_freshness,
    resolve_grid,
    resolve_tier_grid,
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


# --- long-context tier grid --------------------------------------------------

#: The real, slightly inconsistent shape dumped from the production cache:
#: `tiers[0].tier.size` (272 000) disagrees with the fixed-name key
#: `context_over_200k` (which implies 200 000). `tiers` must win.
TIERED_MODELS_DEV = {
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
            },
            # tiers alone, no context_over_200k key at all.
            "gpt-tiers-only": {
                "cost": {
                    "input": 1,
                    "output": 2,
                    "tiers": [
                        {"input": 2, "output": 4, "tier": {"type": "context", "size": 128000}}
                    ],
                }
            },
            # context_over_200k alone, no tiers key at all.
            "gpt-fixed-only": {
                "cost": {
                    "input": 1,
                    "output": 2,
                    "context_over_200k": {"input": 2, "output": 4},
                }
            },
            # Neither key: the ~93% common case, must be unaffected.
            "gpt-no-tier": {"cost": {"input": 1, "output": 2}},
            # A tiers entry whose tier.type isn't "context" must be ignored.
            "gpt-non-context-tier": {
                "cost": {
                    "input": 1,
                    "output": 2,
                    "tiers": [{"input": 2, "output": 4, "tier": {"type": "volume", "size": 1000}}],
                }
            },
            # A malformed tiers block: must fail open, never raise.
            "gpt-malformed-tiers": {
                "cost": {"input": 1, "output": 2, "tiers": "not-a-list"},
            },
        }
    }
}


def test_resolve_tier_grid_prefers_tiers_over_the_fixed_name_key():
    grid, threshold = resolve_tier_grid("gpt-5.5", "openai", TIERED_MODELS_DEV)

    assert grid is not None
    assert grid.input_per_million == Decimal("10")
    assert grid.output_per_million == Decimal("45")
    assert grid.cache_read_per_million == Decimal("1")
    # tiers[0].tier.size (272 000), not the 200 000 implied by the key name.
    assert threshold == 272000


def test_resolve_tier_grid_falls_back_to_context_over_200k_when_tiers_absent():
    grid, threshold = resolve_tier_grid("gpt-fixed-only", "openai", TIERED_MODELS_DEV)

    assert grid is not None
    assert grid.input_per_million == Decimal("2")
    assert grid.output_per_million == Decimal("4")
    assert threshold == 200000


def test_resolve_tier_grid_reads_tiers_alone_without_a_fixed_name_key():
    grid, threshold = resolve_tier_grid("gpt-tiers-only", "openai", TIERED_MODELS_DEV)

    assert grid is not None
    assert grid.input_per_million == Decimal("2")
    assert threshold == 128000


def test_resolve_tier_grid_is_none_when_the_model_publishes_no_tier():
    grid, threshold = resolve_tier_grid("gpt-no-tier", "openai", TIERED_MODELS_DEV)

    assert grid is None
    assert threshold is None


def test_resolve_tier_grid_ignores_a_non_context_tier_type():
    grid, threshold = resolve_tier_grid("gpt-non-context-tier", "openai", TIERED_MODELS_DEV)

    assert grid is None
    assert threshold is None


def test_resolve_tier_grid_fails_open_on_a_malformed_tiers_block():
    grid, threshold = resolve_tier_grid("gpt-malformed-tiers", "openai", TIERED_MODELS_DEV)

    assert grid is None
    assert threshold is None


def test_resolve_tier_grid_is_none_for_an_unknown_model():
    grid, threshold = resolve_tier_grid("does-not-exist", "openai", TIERED_MODELS_DEV)

    assert grid is None
    assert threshold is None


def test_resolve_tier_grid_rewrites_a_subscription_provider_like_resolve_grid():
    # openai-codex is a subscription route; the tier must come from the same
    # paid-API entry (openai) that resolve_grid itself rewrites to.
    grid, threshold = resolve_tier_grid("gpt-5.5", "openai-codex", TIERED_MODELS_DEV)

    assert grid is not None
    assert grid.input_per_million == Decimal("10")
    assert threshold == 272000


def test_iter_catalogue_exposes_the_tier_grid_and_threshold_per_entry():
    entries = {entry.model: entry for entry in iter_catalogue(TIERED_MODELS_DEV)}

    tiered = entries["gpt-5.5"]
    assert tiered.tier_grid is not None
    assert tiered.tier_grid.input_per_million == Decimal("10")
    assert tiered.tier_threshold_tokens == 272000

    untiered = entries["gpt-no-tier"]
    assert untiered.tier_grid is None
    assert untiered.tier_threshold_tokens is None


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


# --- credentialed_provider_slugs (v0.2 Task 9) -------------------------------
#
# hermes_cli and agent are host-only packages, not importable on this
# development machine (confirmed: `python -c "import hermes_cli"` and
# `python -c "import agent"` both raise ModuleNotFoundError here). Every test
# below that needs them present injects a fake module via
# monkeypatch.setitem(sys.modules, ...), the same pattern already established
# for agent.usage_pricing elsewhere in this file. Tests that want to exercise
# the *real* absence (fail-open on this dev machine) call the function with no
# monkeypatching at all.


def _fake_provider_config(auth_type, api_key_env_vars=()):
    return types.SimpleNamespace(auth_type=auth_type, api_key_env_vars=tuple(api_key_env_vars))


def _install_fake_hermes_cli_auth(monkeypatch, *, provider_registry, auth_store=None, load_raises=False):
    fake_hermes_cli = MagicMock()
    fake_auth = MagicMock()
    fake_auth.PROVIDER_REGISTRY = provider_registry
    if load_raises:
        fake_auth._load_auth_store = MagicMock(side_effect=RuntimeError("auth store boom"))
    else:
        fake_auth._load_auth_store = MagicMock(return_value=auth_store if auth_store is not None else {})
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)


def _install_fake_agent_models_dev(monkeypatch, *, provider_to_models_dev):
    fake_agent = MagicMock()
    fake_models_dev = MagicMock()
    fake_models_dev.PROVIDER_TO_MODELS_DEV = provider_to_models_dev
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.models_dev", fake_models_dev)


def test_credentialed_provider_slugs_fails_open_when_hermes_cli_is_not_importable():
    # The real, unmocked case on this development machine: hermes_cli simply
    # isn't installed. Must degrade to "could not determine", never raise.
    slugs, determined = credentialed_provider_slugs()

    assert determined is False
    assert slugs == set()


def test_credentialed_provider_slugs_detects_api_key_present_via_env_var(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value")
    registry = {"openai-api": _fake_provider_config("api_key", ("OPENAI_API_KEY",))}
    _install_fake_hermes_cli_auth(monkeypatch, provider_registry=registry, auth_store={"providers": {}, "credential_pool": {}})
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={"openai-api": "openai"})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == {"openai"}


def test_credentialed_provider_slugs_api_key_absent_everywhere_is_not_present(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = {"openai-api": _fake_provider_config("api_key", ("OPENAI_API_KEY",))}
    _install_fake_hermes_cli_auth(monkeypatch, provider_registry=registry, auth_store={"providers": {}, "credential_pool": {}})
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={"openai-api": "openai"})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == set()


def test_credentialed_provider_slugs_api_key_provider_present_via_credential_pool(monkeypatch):
    # No env var set at all, but the auth store's credential_pool carries an
    # entry for this provider id -- mirrors hermes_cli/model_switch.py:1472's
    # `store.get("credential_pool", {}).get(hermes_id)` check.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = {"openai-api": _fake_provider_config("api_key", ("OPENAI_API_KEY",))}
    _install_fake_hermes_cli_auth(
        monkeypatch,
        provider_registry=registry,
        auth_store={"providers": {}, "credential_pool": {"openai-api": [{"source": "pool"}]}},
    )
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={"openai-api": "openai"})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == {"openai"}


def test_credentialed_provider_slugs_oauth_provider_present_via_providers_store(monkeypatch):
    # Non-api_key providers (oauth_device_code, oauth_external, ...) have no
    # env var to check; presence is read from the auth store's own
    # "providers" section -- mirrors model_switch.py:1548-1550's
    # `pid in providers_store` check.
    registry = {"nous": _fake_provider_config("oauth_device_code")}
    _install_fake_hermes_cli_auth(
        monkeypatch,
        provider_registry=registry,
        auth_store={"providers": {"nous": {"access_token": "..."}}, "credential_pool": {}},
    )
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == {"nous"}  # not in the mapping table -> falls back to its own id


def test_credentialed_provider_slugs_oauth_provider_absent_everywhere_is_not_present(monkeypatch):
    registry = {"nous": _fake_provider_config("oauth_device_code")}
    _install_fake_hermes_cli_auth(
        monkeypatch, provider_registry=registry, auth_store={"providers": {}, "credential_pool": {}}
    )
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == set()


def test_credentialed_provider_slugs_an_oauth_providers_env_var_is_never_consulted(monkeypatch):
    # Pin the auth_type gate itself: even if a non-api_key entry somehow
    # carried api_key_env_vars, it must not be treated as a credential source
    # -- mirrors model_switch.py:1457's explicit `auth_type != "api_key"` skip.
    monkeypatch.setenv("SOME_STRAY_ENV_VAR", "set")
    registry = {"nous": _fake_provider_config("oauth_device_code", ("SOME_STRAY_ENV_VAR",))}
    _install_fake_hermes_cli_auth(
        monkeypatch, provider_registry=registry, auth_store={"providers": {}, "credential_pool": {}}
    )
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == set()


def test_credentialed_provider_slugs_maps_through_provider_to_models_dev_when_slugs_disagree(monkeypatch):
    # Confirmed real mismatch (see agent/models_dev.py:PROVIDER_TO_MODELS_DEV
    # on the host): Hermes' "copilot" is models.dev's "github-copilot".
    monkeypatch.setenv("GH_COPILOT_TOKEN", "present")
    registry = {"copilot": _fake_provider_config("api_key", ("GH_COPILOT_TOKEN",))}
    _install_fake_hermes_cli_auth(monkeypatch, provider_registry=registry, auth_store={"providers": {}, "credential_pool": {}})
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={"copilot": "github-copilot"})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == {"github-copilot"}


def test_credentialed_provider_slugs_falls_back_to_identity_when_mapping_table_unavailable(monkeypatch):
    # agent.models_dev fails to import (a narrower failure than hermes_cli.auth
    # itself being absent) -- credential presence is still determinable, just
    # with the less-precise identity-slug fallback for every id.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value")
    registry = {"openai-api": _fake_provider_config("api_key", ("OPENAI_API_KEY",))}
    _install_fake_hermes_cli_auth(monkeypatch, provider_registry=registry, auth_store={"providers": {}, "credential_pool": {}})
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    monkeypatch.delitem(sys.modules, "agent.models_dev", raising=False)

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == {"openai-api"}


def test_credentialed_provider_slugs_fails_open_when_auth_store_load_raises(monkeypatch):
    registry = {"openai-api": _fake_provider_config("api_key", ("OPENAI_API_KEY",))}
    _install_fake_hermes_cli_auth(monkeypatch, provider_registry=registry, load_raises=True)
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={})

    slugs, determined = credentialed_provider_slugs()

    assert determined is False
    assert slugs == set()


def test_credentialed_provider_slugs_result_is_lowercased_for_case_insensitive_matching(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "present")
    registry = {"SomeProvider": _fake_provider_config("api_key", ("SOME_KEY",))}
    _install_fake_hermes_cli_auth(monkeypatch, provider_registry=registry, auth_store={"providers": {}, "credential_pool": {}})
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={"SomeProvider": "SomeCatalogueKey"})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == {"somecataloguekey"}


def test_credentialed_provider_slugs_malformed_auth_store_sections_degrade_to_absent(monkeypatch):
    # providers/credential_pool present but not dicts -- treated as empty,
    # not a crash, and status is still determined (the store loaded fine;
    # its shape just wasn't the expected one).
    registry = {"nous": _fake_provider_config("oauth_device_code")}
    _install_fake_hermes_cli_auth(
        monkeypatch, provider_registry=registry, auth_store={"providers": "not-a-dict", "credential_pool": ["nope"]}
    )
    _install_fake_agent_models_dev(monkeypatch, provider_to_models_dev={})

    slugs, determined = credentialed_provider_slugs()

    assert determined is True
    assert slugs == set()


def _fake_auth(monkeypatch, *, registry=None, pool=None, providers=None):
    fake_root = MagicMock()
    fake_auth = MagicMock()
    fake_auth.PROVIDER_REGISTRY = registry if registry is not None else {}
    fake_auth._load_auth_store = MagicMock(
        return_value={"credential_pool": pool or {}, "providers": providers or {}}
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_root)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)


def test_credentialed_slugs_include_a_pool_entry_with_no_registry_entry(monkeypatch):
    from hermes_cost_arbitrage_dashboard.pricing import credentialed_provider_slugs

    # Measured on a live host: `openrouter` is in credential_pool but absent
    # from PROVIDER_REGISTRY. Iterating the registry alone missed it, and with
    # the credentials filter on by default the catalogue fell from 4003 models
    # to 57 - excluding the provider holding every cheap alternative.
    _fake_auth(monkeypatch, registry={}, pool={"openrouter": {}, "nvidia": {}})

    slugs, available = credentialed_provider_slugs()

    assert available is True
    assert "openrouter" in slugs
    assert "nvidia" in slugs


def test_credentialed_slugs_map_a_subscription_id_to_its_paid_provider(monkeypatch):
    from hermes_cost_arbitrage_dashboard.pricing import credentialed_provider_slugs

    _fake_auth(monkeypatch, registry={}, pool={"openai-codex": {}})

    slugs, available = credentialed_provider_slugs()

    # The catalogue is keyed on paid providers; a subscription route holds a
    # credential for the same underlying models.
    assert "openai" in slugs
    assert available is True


def test_credentialed_slugs_include_auth_store_providers_without_a_registry_entry(monkeypatch):
    from hermes_cost_arbitrage_dashboard.pricing import credentialed_provider_slugs

    _fake_auth(monkeypatch, registry={}, providers={"some-oauth-provider": {}})

    slugs, _ = credentialed_provider_slugs()

    assert "some-oauth-provider" in slugs
