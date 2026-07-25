"""Backend for the hermes-cost-arbitrage dashboard plugin.

Mounted by the Hermes dashboard at ``/api/plugins/hermes-cost-arbitrage/``.

Sibling modules are loaded through an explicit path loader rather than package
imports: the host's plugin loading mechanism makes no ``sys.path`` guarantee.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent


def _load_sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cost_engine = _load_sibling("cost_engine")
pricing = _load_sibling("pricing")
store = _load_sibling("store")
plugin_config = _load_sibling("plugin_config")

UsageVector = cost_engine.UsageVector
price_usage = cost_engine.price_usage

try:
    from fastapi import APIRouter, Body, HTTPException
except Exception:  # Allows unit tests without dashboard dependencies.

    class APIRouter:  # type: ignore
        def get(self, *_args, **_kwargs):
            return lambda fn: fn

        def put(self, *_args, **_kwargs):
            return lambda fn: fn

    def Body(default=None, **_kwargs):  # type: ignore
        return default

    HTTPException = None  # type: ignore


router = APIRouter()

DAYS_IN_MONTH = 30

#: Repeated on every surface that shows money. Hermes gates its own token
#: analytics for this reason: local counts exclude auxiliary calls and provider
#: retries, so they sit below real provider billing.
FLOOR_NOTICE = (
    "Local token counts exclude auxiliary calls and provider retries, so every "
    "figure here is a floor, not a bill. The error runs against the "
    "pay-as-you-go option, keeping the comparison conservative in favour of the "
    "subscription."
)


def _models_dev_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        base = Path(get_hermes_home())
    except Exception:
        home = (os.environ.get("HERMES_HOME") or "").strip()
        base = Path(home) if home else Path.home() / ".hermes"
    return base / "models_dev_cache.json"


def _usd(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(round(value, 2))


def _aggregate(usage_rows: list) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "sessions": 0,
    }
    for row in usage_rows:
        totals["input_tokens"] += row.usage.input_tokens
        totals["output_tokens"] += row.usage.output_tokens
        totals["cache_read_tokens"] += row.usage.cache_read_tokens
        totals["cache_write_tokens"] += row.usage.cache_write_tokens
        totals["sessions"] += row.sessions
    return totals


def build_summary(
    usage_rows: list,
    models_dev: dict[str, Any],
    subscription_usd: float,
    days: int,
) -> dict[str, Any]:
    """Price real consumption per model actually used."""
    models: list[dict[str, Any]] = []
    ghost_total = Decimal(0)

    for row in usage_rows:
        grid = pricing.resolve_grid(row.model, row.provider, models_dev)
        cost = price_usage(row.usage, grid)
        if cost.headline_usd is not None:
            ghost_total += cost.headline_usd
        models.append(
            {
                "model": row.model,
                "billing_provider": row.provider,
                "priced_as_provider": pricing.ghost_provider(row.provider),
                "sessions": row.sessions,
                "input_tokens": row.usage.input_tokens,
                "output_tokens": row.usage.output_tokens,
                "cache_read_tokens": row.usage.cache_read_tokens,
                "cache_write_tokens": row.usage.cache_write_tokens,
                "cache_aware_usd": _usd(cost.cache_aware_usd),
                "no_cache_usd": _usd(cost.no_cache_usd),
                "headline_usd": _usd(cost.headline_usd),
                "cache_status": cost.cache_status,
                "status": cost.status,
                "pricing_source": cost.source,
            }
        )

    ghost = float(round(ghost_total, 2))
    projection = ghost * DAYS_IN_MONTH / days if days else 0.0
    return {
        "days": days,
        "totals": _aggregate(usage_rows),
        "ghost_cost_usd": ghost,
        "monthly_projection_usd": round(projection, 2),
        "subscription_usd_per_month": float(subscription_usd),
        "models": models,
        "notice": FLOOR_NOTICE,
    }


def build_whatif(
    usage_rows: list,
    pinned: list[dict[str, str]],
    models_dev: dict[str, Any],
    subscription_usd: float,
    days: int,
) -> dict[str, Any]:
    """Price the whole measured usage vector against each pinned candidate."""
    totals = _aggregate(usage_rows)
    combined = UsageVector(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
    )

    candidates: list[dict[str, Any]] = []
    for entry in pinned:
        provider = entry.get("provider", "")
        model = entry.get("model", "")
        grid = pricing.resolve_grid(model, provider, models_dev)
        cost = price_usage(combined, grid)

        monthly: Optional[float] = None
        if cost.headline_usd is not None:
            monthly = float(round(cost.headline_usd * Decimal(DAYS_IN_MONTH) / Decimal(days), 2)) if days else 0.0

        break_even: Optional[float] = None
        if monthly:
            # Cost scales with volume: the subscription's flat price buys this
            # fraction of the current monthly volume on that model.
            break_even = subscription_usd / monthly

        candidates.append(
            {
                "provider": provider,
                "model": model,
                "monthly_usd": monthly,
                "cache_aware_usd": _usd(cost.cache_aware_usd),
                "no_cache_usd": _usd(cost.no_cache_usd),
                "cache_status": cost.cache_status,
                "status": cost.status,
                "pricing_source": cost.source,
                "break_even_volume_ratio": break_even,
                "cheaper_than_subscription": bool(monthly is not None and monthly < subscription_usd),
            }
        )

    candidates.sort(key=lambda row: (row["monthly_usd"] is None, row["monthly_usd"] or 0.0))
    return {
        "days": days,
        "subscription_usd_per_month": float(subscription_usd),
        "candidates": candidates,
        "notice": FLOOR_NOTICE,
    }


def _context(days: int) -> tuple[list, dict[str, Any], dict[str, Any]]:
    usage_rows = store.read_usage_window(store.default_state_db_path(), days)
    models_dev = pricing.load_models_dev(_models_dev_path())
    config = plugin_config.load_config(plugin_config.config_path())
    return usage_rows, models_dev, config


@router.get("/summary")
def summary(days: int = 30) -> dict[str, Any]:
    usage_rows, models_dev, config = _context(days)
    return build_summary(usage_rows, models_dev, config["subscription_usd_per_month"], days)


@router.get("/whatif")
def whatif(days: int = 30) -> dict[str, Any]:
    usage_rows, models_dev, config = _context(days)
    return build_whatif(
        usage_rows, config["pinned"], models_dev, config["subscription_usd_per_month"], days
    )


@router.get("/config")
def get_config() -> dict[str, Any]:
    return plugin_config.load_config(plugin_config.config_path())


@router.put("/config")
def put_config(payload: dict = Body(default={})) -> dict[str, Any]:
    try:
        return plugin_config.save_config(plugin_config.config_path(), payload or {})
    except OSError as exc:
        # save_config writes atomically and deliberately does not swallow
        # write failures (full disk, permission denied, ...). Surface a clean
        # error to the client instead of an unhandled 500, without ever
        # letting the plugin take the dashboard process down with it.
        if HTTPException is not None:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "status": "error",
            "detail": str(exc),
            "config": plugin_config.load_config(plugin_config.config_path()),
        }
