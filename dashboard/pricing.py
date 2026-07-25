"""Resolve a :class:`PricingGrid` for a (model, provider) pair.

The crux of this plugin lives here. Hermes' own pricing layer deliberately
short-circuits subscription routes:

    resolve_billing_route("gpt-5.5", provider="openai-codex")
        -> BillingRoute(billing_mode="subscription_included")
    get_pricing_entry(...)   -> PricingEntry(all rates = Decimal("0"))
    estimate_usage_cost(...) -> CostResult(amount_usd=0, status="included")

That is correct accounting — a subscription call has no marginal cost — and it
is exactly why the native dashboard reads $0. To answer "what would this cost
on the paid API?", the provider is rewritten to its pay-as-you-go equivalent
*before* pricing.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from cost_engine import PricingGrid

#: Subscription routes mapped to the paid API that serves the same models.
GHOST_PROVIDER_REWRITE: dict[str, str] = {
    "openai-codex": "openai",
}

#: Providers whose rates Hermes resolves from its offline snapshot table.
#: Everything else is read from the local models.dev cache, so that pricing a
#: candidate never performs network I/O inside a dashboard request.
_OFFLINE_HERMES_PROVIDERS = {"openai", "anthropic", "minimax", "minimax-cn"}


def ghost_provider(provider: Optional[str]) -> str:
    """Map a billing provider to the paid provider used for ghost costing."""
    name = (provider or "").strip().lower()
    return GHOST_PROVIDER_REWRITE.get(name, name)


def load_models_dev(path: Path | str) -> dict[str, Any]:
    """Load the local models.dev cache. Returns ``{}`` on any failure."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _grid_from_models_dev(model: str, provider: str, models_dev: dict[str, Any]) -> Optional[PricingGrid]:
    try:
        entry = ((models_dev.get(provider) or {}).get("models") or {}).get(model)
        if not isinstance(entry, dict):
            return None
        cost = entry.get("cost") or {}
        grid = PricingGrid(
            input_per_million=_decimal(cost.get("input")),
            output_per_million=_decimal(cost.get("output")),
            cache_read_per_million=_decimal(cost.get("cache_read")),
            cache_write_per_million=_decimal(cost.get("cache_write")),
            source="models.dev",
        )
        return grid if grid.is_priced else None
    except (AttributeError, TypeError):
        return None


def _grid_from_hermes(model: str, provider: str) -> Optional[PricingGrid]:
    """Ask Hermes' own pricing table. Returns ``None`` when unavailable."""
    if provider not in _OFFLINE_HERMES_PROVIDERS:
        return None
    try:
        from agent.usage_pricing import get_pricing_entry
    except Exception:
        return None
    try:
        entry = get_pricing_entry(model, provider=provider)
    except Exception:
        return None
    if entry is None:
        return None
    grid = PricingGrid(
        input_per_million=entry.input_cost_per_million,
        output_per_million=entry.output_cost_per_million,
        cache_read_per_million=entry.cache_read_cost_per_million,
        cache_write_per_million=entry.cache_write_cost_per_million,
        source=entry.source or "hermes",
    )
    # A subscription route would slip through as an all-zero grid; treat that
    # as "no usable pricing" so the models.dev fallback gets its turn.
    if not grid.is_priced or grid.input_per_million == Decimal(0):
        return None
    return grid


def resolve_grid(model: str, provider: Optional[str], models_dev: dict[str, Any]) -> PricingGrid:
    """Best available paid-API pricing for *model*, never a subscription zero."""
    paid_provider = ghost_provider(provider)
    return (
        _grid_from_hermes(model, paid_provider)
        or _grid_from_models_dev(model, paid_provider, models_dev)
        or PricingGrid(source="unknown")
    )
