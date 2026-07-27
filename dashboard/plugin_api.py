"""Backend for the hermes-cost-arbitrage dashboard plugin.

Mounted by the Hermes dashboard at ``/api/plugins/hermes-cost-arbitrage/``.

The host loads this module directly by file path (the manifest's ``"api"``
entry), so it has no ``__package__`` of its own and cannot use relative
imports. Its sibling modules (``cost_engine``, ``pricing``, ``store``,
``plugin_config``) are therefore bootstrapped here as members of a
uniquely-named package, ``hermes_cost_arbitrage_dashboard``, rather than
registered under their own bare names in the process-global ``sys.modules``.
A bare name would risk silent collision with another plugin — or the host
itself — owning a module of the same name: whichever loaded first would
silently win, and the other would silently get the wrong module.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent

#: Unique name under which this plugin's modules live in sys.modules, so they
#: can never collide with another plugin's or the host's module of the same
#: bare name (e.g. a module also named "store" or "pricing").
PACKAGE_NAME = "hermes_cost_arbitrage_dashboard"


def _bootstrap_package():
    """Load ``dashboard/`` as the ``PACKAGE_NAME`` package, idempotently.

    Safe to call more than once (the host may import this module more than
    once): if the package is already registered in ``sys.modules``, it is
    reused rather than re-executed.
    """
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        _HERE / "__init__.py",
        submodule_search_locations=[str(_HERE)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load package {PACKAGE_NAME}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    return package


_bootstrap_package()

cost_engine = importlib.import_module(f"{PACKAGE_NAME}.cost_engine")
pricing = importlib.import_module(f"{PACKAGE_NAME}.pricing")
store = importlib.import_module(f"{PACKAGE_NAME}.store")
plugin_config = importlib.import_module(f"{PACKAGE_NAME}.plugin_config")
paths = importlib.import_module(f"{PACKAGE_NAME}.paths")

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

        def post(self, *_args, **_kwargs):
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
    return paths.hermes_home() / "models_dev_cache.json"


#: Safe default for the ``pricing_data`` keyword-only parameter on the pure
#: builders below, so a caller that doesn't pass one (existing callers,
#: tests) gets an honest "unavailable" placeholder rather than a crash or a
#: fabricated timestamp. Mirrors the shape returned by
#: :func:`pricing.models_dev_freshness`.
_UNKNOWN_PRICING_DATA: dict[str, Any] = {
    "updated_at": None,
    "age_hours": None,
    "available": False,
}


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
    *,
    usage_available: bool = True,
    usage_unavailable_reason: str | None = None,
    pricing_data: dict[str, Any] | None = None,
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
        "usage_available": usage_available,
        "usage_unavailable_reason": usage_unavailable_reason,
        "models_dev_available": bool(models_dev),
        "pricing_data": pricing_data if pricing_data is not None else dict(_UNKNOWN_PRICING_DATA),
    }


def build_whatif(
    usage_rows: list,
    pinned: list[dict[str, str]],
    models_dev: dict[str, Any],
    subscription_usd: float,
    days: int,
    *,
    usage_available: bool = True,
    usage_unavailable_reason: str | None = None,
    pricing_data: dict[str, Any] | None = None,
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
        "usage_available": usage_available,
        "usage_unavailable_reason": usage_unavailable_reason,
        "models_dev_available": bool(models_dev),
        "pricing_data": pricing_data if pricing_data is not None else dict(_UNKNOWN_PRICING_DATA),
    }


def _context(
    days: int,
) -> tuple[list, dict[str, Any], dict[str, Any], bool, str | None, dict[str, Any]]:
    db_path = store.default_state_db_path()
    usage_available, usage_unavailable_reason = store.state_db_status(db_path)
    usage_rows = store.read_usage_window(db_path, days)
    models_dev = pricing.load_models_dev(_models_dev_path())
    config = plugin_config.load_config(plugin_config.config_path())
    pricing_data = pricing.models_dev_freshness(_models_dev_path())
    return usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data


def _clamp_days(days: int) -> int:
    """Keep an out-of-range window from aggregating the whole live table."""
    return max(1, min(days, 365))


#: Sort keys the catalogue understands, and the candidate-row field each one
#: maps to. An unrecognised key falls back to "monthly" rather than raising —
#: both here (build_catalogue) and in the /catalogue handler's whitelist.
CATALOGUE_SORT_FIELDS: dict[str, str] = {
    "model": "model",
    "provider": "provider",
    "monthly": "monthly_usd",
    "cache_aware": "cache_aware_usd",
    "no_cache": "no_cache_usd",
    "break_even": "break_even_volume_ratio",
}

#: The only limit values the catalogue UI offers. Anything else falls back
#: to 25.
CATALOGUE_LIMITS = {10, 25, 50, 100}


def build_catalogue(
    usage_rows: list,
    models_dev: dict[str, Any],
    subscription_usd: float,
    days: int,
    *,
    sort: str = "monthly",
    order: str = "asc",
    limit: int = 25,
    query: str = "",
    usage_available: bool = True,
    usage_unavailable_reason: str | None = None,
    pricing_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Price the whole measured usage vector against every catalogued model.

    Pure function, like :func:`build_summary` and :func:`build_whatif`: no
    I/O, no globals, everything arrives as an argument. Filters by *query*
    (case-insensitive substring over provider or model), sorts, then
    truncates to *limit* — search, sort and limit all happen here rather
    than in the client, because the full priced catalogue would be several
    MB per window change.
    """
    totals = _aggregate(usage_rows)
    combined = UsageVector(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
    )

    needle = query.strip().lower()
    candidates: list[dict[str, Any]] = []
    for provider, model, grid in pricing.iter_catalogue(models_dev):
        if needle and needle not in provider.lower() and needle not in model.lower():
            continue

        cost = price_usage(combined, grid)

        monthly: Optional[float] = None
        if cost.headline_usd is not None:
            monthly = float(round(cost.headline_usd * Decimal(DAYS_IN_MONTH) / Decimal(days), 2)) if days else 0.0

        break_even: Optional[float] = None
        if monthly:
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

    total_matched = len(candidates)

    effective_sort = sort if sort in CATALOGUE_SORT_FIELDS else "monthly"
    effective_order = "desc" if order == "desc" else "asc"
    field = CATALOGUE_SORT_FIELDS[effective_sort]
    reverse = effective_order == "desc"

    # None must sort last regardless of order — an unpriced candidate must
    # never top the list just because the order flipped. Splitting the list
    # rather than folding "is None" into the sort key keeps that true under
    # `reverse=True` as well as `reverse=False`.
    present = [row for row in candidates if row[field] is not None]
    absent = [row for row in candidates if row[field] is None]
    present.sort(key=lambda row: row[field], reverse=reverse)
    candidates = present + absent

    # Unlike sort/order, limit is not restricted here to the UI's allowed
    # set — that whitelist ({10, 25, 50, 100}) is the /catalogue handler's
    # job. This pure function just truncates to whatever non-negative limit
    # it is given.
    truncated = candidates[: max(0, limit)]

    return {
        "days": days,
        "subscription_usd_per_month": float(subscription_usd),
        "candidates": truncated,
        "total_matched": total_matched,
        "returned": len(truncated),
        "sort": effective_sort,
        "order": effective_order,
        "limit": limit,
        "query": query,
        "notice": FLOOR_NOTICE,
        "usage_available": usage_available,
        "usage_unavailable_reason": usage_unavailable_reason,
        "models_dev_available": bool(models_dev),
        "pricing_data": pricing_data if pricing_data is not None else dict(_UNKNOWN_PRICING_DATA),
    }


@router.get("/summary")
def summary(days: int = 30) -> dict[str, Any]:
    days = _clamp_days(days)
    usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data = _context(days)
    return build_summary(
        usage_rows,
        models_dev,
        config["subscription_usd_per_month"],
        days,
        usage_available=usage_available,
        usage_unavailable_reason=usage_unavailable_reason,
        pricing_data=pricing_data,
    )


@router.get("/whatif")
def whatif(days: int = 30) -> dict[str, Any]:
    days = _clamp_days(days)
    usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data = _context(days)
    return build_whatif(
        usage_rows,
        config["pinned"],
        models_dev,
        config["subscription_usd_per_month"],
        days,
        usage_available=usage_available,
        usage_unavailable_reason=usage_unavailable_reason,
        pricing_data=pricing_data,
    )


@router.get("/catalogue")
def catalogue(
    days: int = 30,
    sort: str = "monthly",
    order: str = "asc",
    limit: int = 25,
    query: str = "",
) -> dict[str, Any]:
    days = _clamp_days(days)
    sort = sort if sort in CATALOGUE_SORT_FIELDS else "monthly"
    order = order if order in ("asc", "desc") else "asc"
    limit = limit if limit in CATALOGUE_LIMITS else 25
    usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data = _context(days)
    return build_catalogue(
        usage_rows,
        models_dev,
        config["subscription_usd_per_month"],
        days,
        sort=sort,
        order=order,
        limit=limit,
        query=query,
        usage_available=usage_available,
        usage_unavailable_reason=usage_unavailable_reason,
        pricing_data=pricing_data,
    )


@router.post("/refresh-pricing")
def refresh_pricing() -> dict[str, Any]:
    """Force-refresh the local models.dev cache, then report its new age.

    The one place network I/O is allowed in this plugin, and only because
    this is an explicit, user-initiated action (never triggered by a GET).
    Deliberately ``def``, not ``async def``: FastAPI runs a blocking ``def``
    handler in its threadpool, so a slow network fetch here cannot stall any
    other dashboard request running on the event loop.

    Fail-open in both directions that matter:

    - ``agent.models_dev`` not being importable (true on the development
      machine; only the production host has it) is a reportable outcome,
      not a crash.
    - ``fetch_models_dev`` raising (network error, timeout, ...) is reported
      as ``{"ok": false, "detail": ...}``, never an unhandled exception and
      never a 500.
    """
    ok = False
    detail: str | None = None
    try:
        from agent.models_dev import fetch_models_dev
    except Exception as exc:
        detail = f"agent.models_dev is not importable: {exc}"
    else:
        try:
            fetch_models_dev(force_refresh=True)
            ok = True
        except Exception as exc:
            detail = f"refresh failed: {exc}"

    pricing_data = pricing.models_dev_freshness(_models_dev_path())
    return {
        "ok": ok,
        "detail": detail,
        "pricing_data": pricing_data,
    }


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
