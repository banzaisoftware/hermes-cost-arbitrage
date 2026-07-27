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
        "api_call_count": 0,
    }
    for row in usage_rows:
        totals["input_tokens"] += row.usage.input_tokens
        totals["output_tokens"] += row.usage.output_tokens
        totals["cache_read_tokens"] += row.usage.cache_read_tokens
        totals["cache_write_tokens"] += row.usage.cache_write_tokens
        totals["sessions"] += row.sessions
        totals["api_call_count"] += row.api_call_count
    return totals


def _avg_context_per_call(totals: dict[str, int]) -> Optional[float]:
    """Observed average prompt context per API call, blended across every
    model in *totals* — the same (input + cache_read + cache_write) /
    api_call_count ratio as :attr:`store.ModelUsage.avg_context_per_call`,
    but over the combined usage vector rather than a single model's.

    Lets the UI say how close the measured workload sits to a candidate's
    long-context threshold (v0.2 Task 4) even though ``build_whatif`` and
    ``build_catalogue`` price one combined vector against many candidates,
    not one usage row per candidate. ``None`` when no API calls were
    recorded in the window — division is guarded, never raises.
    """
    calls = totals.get("api_call_count", 0)
    if not calls:
        return None
    prompt_tokens = totals["input_tokens"] + totals["cache_read_tokens"] + totals["cache_write_tokens"]
    return prompt_tokens / calls


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

        # The long-context upper bound (v0.2 Task 4): additive information
        # only, never folded into headline_usd or ghost_total above. See
        # cost_engine.price_long_context for why this is a bound, not an
        # estimate — the sessions table has no per-call context size.
        tier_grid, tier_threshold_tokens = pricing.resolve_tier_grid(row.model, row.provider, models_dev)
        long_context_usd = cost_engine.price_long_context(row.usage, tier_grid)

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
                "long_context_usd": _usd(long_context_usd),
                "tier_threshold_tokens": tier_threshold_tokens,
                "avg_context_per_call": row.avg_context_per_call,
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

        # Long-context upper bound (v0.2 Task 4), additive alongside the
        # existing cache-aware / no-cache pair — never blended into monthly_usd.
        tier_grid, tier_threshold_tokens = pricing.resolve_tier_grid(model, provider, models_dev)
        long_context_usd = cost_engine.price_long_context(combined, tier_grid)

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
                "long_context_usd": _usd(long_context_usd),
                "tier_threshold_tokens": tier_threshold_tokens,
            }
        )

    candidates.sort(key=lambda row: (row["monthly_usd"] is None, row["monthly_usd"] or 0.0))
    return {
        "days": days,
        "subscription_usd_per_month": float(subscription_usd),
        "avg_context_per_call": _avg_context_per_call(totals),
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


def _parse_providers(providers: str) -> list[str]:
    """Normalise a comma-separated provider list: trim, lowercase, dedupe, sort.

    Fail-open: anything that isn't splittable like a string (``None``, a
    non-string) yields an empty list -- "no constraint" -- rather than
    raising. An empty or whitespace-only string also yields an empty list,
    which both ``providers_mode`` values must read as "no constraint" (see
    the include/exclude handling in :func:`build_catalogue`) rather than
    "match nothing".
    """
    try:
        names = {name.strip().lower() for name in providers.split(",") if name.strip()}
    except AttributeError:
        return []
    return sorted(names)


def _is_free_grid(grid: cost_engine.PricingGrid) -> bool:
    """A model is "free" when its *published* grid prices both input and
    output at exactly ``Decimal(0)`` -- a fact about the rates models.dev
    publishes, never about what a particular usage window happens to price
    it at. Deliberately not ``monthly_usd == 0``: with an empty usage window
    every model prices to $0, and that definition would hide the entire
    catalogue rather than just the genuinely free entries.
    """
    return (
        grid.input_per_million is not None
        and grid.output_per_million is not None
        and grid.input_per_million == Decimal(0)
        and grid.output_per_million == Decimal(0)
    )


def build_catalogue(
    usage_rows: list,
    models_dev: dict[str, Any],
    subscription_usd: float,
    days: int,
    *,
    sort: str = "monthly",
    order: str = "asc",
    limit: int = 25,
    offset: int = 0,
    query: str = "",
    providers: str = "",
    providers_mode: str = "include",
    tool_call: bool = True,
    vision: bool = False,
    reasoning: bool = False,
    open_weights: bool = False,
    min_context: int = 0,
    # Deliberately False here, unlike the /catalogue *handler*'s default of
    # True: this fixture-backed builder is called directly (with no
    # hide_free argument) by several tests that predate this filter and
    # assert a zero-priced fixture model is present by default (e.g.
    # test_build_catalogue_prices_every_priced_entry_in_the_cache). Keeping
    # this default at "no constraint" preserves that behaviour; the handler
    # supplies True explicitly so end users still get the useful default.
    hide_free: bool = False,
    usage_available: bool = True,
    usage_unavailable_reason: str | None = None,
    pricing_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Price the whole measured usage vector against every catalogued model.

    Pure function, like :func:`build_summary` and :func:`build_whatif`: no
    I/O, no globals, everything arrives as an argument. Filters by *query*
    (case-insensitive substring over provider or model) and by capability,
    sorts, then truncates to *limit* — search, sort, filter and limit all
    happen here rather than in the client, because the full priced catalogue
    would be several MB per window change.

    Capability filters (*tool_call*, *vision*, *reasoning*, *open_weights*)
    each work the same way: when the flag is ``True`` the candidate must
    have that capability; when ``False`` it imposes **no constraint** at
    all — "off" must never be misread as "require the capability's absence".
    ``tool_call`` defaults ``True`` because 1 137 of the 5 754 real models
    cannot call a tool at all and therefore cannot run the agent; a
    candidate that can't act is not a meaningful price comparison. The
    other three default off.

    *min_context* (default 0, meaning no constraint) requires
    ``capabilities.context_limit >= min_context``. A model whose context
    limit is unknown (``None``) is excluded whenever *min_context* is a
    genuine positive threshold — an unverifiable capability must not be
    presented as satisfying a requirement the user's own workload set. When
    *min_context* is 0 (the "off" state), an unknown limit passes through
    like everything else, consistent with the other filters' off-state.

    *providers* (a comma-separated list, case-insensitive, trimmed; empty
    means no constraint) combines with *providers_mode* (``"include"`` or
    ``"exclude"``, defaulting to ``"include"``): in include mode only
    listed providers pass, in exclude mode listed providers are removed and
    everything else passes. An **empty list imposes no constraint in
    either mode** — this must never be misread as "show nothing" for an
    empty include list. An unlisted/unknown provider name simply never
    matches anything; it is not an error.

    *hide_free* (default ``False`` on this builder; the ``/catalogue``
    handler supplies ``True``) drops candidates whose grid publishes
    ``input_per_million == output_per_million == Decimal(0)`` — see
    :func:`_is_free_grid`. This is deliberately a fact about the published
    rates, not about the computed ``monthly_usd`` for the current usage
    window: an empty window prices every model to $0, and keying off that
    would hide the entire catalogue.

    *offset* (default 0, clamped to >= 0) is applied after sorting and
    before truncation to *limit*: the full filtered, sorted set is sliced
    ``[offset : offset + limit]``. An offset past the end of the set
    yields an empty ``candidates`` list rather than an error or a
    wrapped-around page. The envelope's ``page`` (1-based) and ``pages``
    let the UI render "page X of Y" without a second request; both are 0
    or 1 respectively whenever *limit* or ``total_matched`` is 0, guarding
    the division rather than raising.
    """
    totals = _aggregate(usage_rows)
    combined = UsageVector(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
    )

    needle = query.strip().lower()
    provider_names = _parse_providers(providers)
    provider_set = set(provider_names)
    effective_providers_mode = "exclude" if providers_mode == "exclude" else "include"

    candidates: list[dict[str, Any]] = []
    for entry in pricing.iter_catalogue(models_dev):
        provider, model, grid, capabilities = entry.provider, entry.model, entry.grid, entry.capabilities
        tier_grid, tier_threshold_tokens = entry.tier_grid, entry.tier_threshold_tokens

        if needle and needle not in provider.lower() and needle not in model.lower():
            continue
        if tool_call and not capabilities.tool_call:
            continue
        if vision and not capabilities.vision:
            continue
        if reasoning and not capabilities.reasoning:
            continue
        if open_weights and not capabilities.open_weights:
            continue
        if min_context > 0 and (capabilities.context_limit is None or capabilities.context_limit < min_context):
            continue
        if provider_set:
            # Empty provider_set (the common case) imposes no constraint in
            # either mode -- that check happens above, before this block is
            # even reached, so include and exclude can never both be
            # misread as "match nothing" for an empty list.
            is_listed = provider.strip().lower() in provider_set
            if effective_providers_mode == "exclude":
                if is_listed:
                    continue
            elif not is_listed:
                continue
        if hide_free and _is_free_grid(grid):
            continue

        cost = price_usage(combined, grid)

        monthly: Optional[float] = None
        if cost.headline_usd is not None:
            monthly = float(round(cost.headline_usd * Decimal(DAYS_IN_MONTH) / Decimal(days), 2)) if days else 0.0

        break_even: Optional[float] = None
        if monthly:
            break_even = subscription_usd / monthly

        # Long-context upper bound (v0.2 Task 4): the tier grid/threshold
        # already rode along on the CatalogueEntry from iter_catalogue, so no
        # second models.dev lookup is needed here. Additive only — never
        # blended into monthly_usd or the cache-aware/no-cache pair above.
        long_context_usd = cost_engine.price_long_context(combined, tier_grid)

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
                "long_context_usd": _usd(long_context_usd),
                "tier_threshold_tokens": tier_threshold_tokens,
                "capabilities": {
                    "tool_call": capabilities.tool_call,
                    "vision": capabilities.vision,
                    "reasoning": capabilities.reasoning,
                    "open_weights": capabilities.open_weights,
                    "context_limit": capabilities.context_limit,
                },
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

    # Unlike sort/order, limit and offset are not restricted here to the
    # UI's allowed set — that whitelist ({10, 25, 50, 100} for limit) is the
    # /catalogue handler's job. This pure function just clamps offset to a
    # sane non-negative value and slices with whatever limit it is given.
    effective_limit = max(0, limit)
    effective_offset = max(0, offset)
    truncated = candidates[effective_offset : effective_offset + effective_limit]

    # page/pages let the UI render "page X of Y" without a second request.
    # Both branches of this guard exist for the same reason: dividing by a
    # zero limit or paging over zero matches must never raise, and must
    # never fabricate a page number that implies data that isn't there.
    if effective_limit > 0 and total_matched > 0:
        pages = -(-total_matched // effective_limit)  # ceil division
        page = effective_offset // effective_limit + 1
    else:
        pages = 0
        page = 1

    return {
        "days": days,
        "subscription_usd_per_month": float(subscription_usd),
        "avg_context_per_call": _avg_context_per_call(totals),
        "candidates": truncated,
        "total_matched": total_matched,
        "returned": len(truncated),
        "sort": effective_sort,
        "order": effective_order,
        "limit": limit,
        "offset": offset,
        "page": page,
        "pages": pages,
        "query": query,
        "filters": {
            "tool_call": tool_call,
            "vision": vision,
            "reasoning": reasoning,
            "open_weights": open_weights,
            "min_context": min_context,
            "providers": provider_names,
            "providers_mode": effective_providers_mode,
            "hide_free": hide_free,
        },
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
    offset: int = 0,
    query: str = "",
    providers: str = "",
    providers_mode: str = "include",
    tool_call: bool = True,
    vision: bool = False,
    reasoning: bool = False,
    open_weights: bool = False,
    min_context: int = 0,
    # True here (unlike build_catalogue's own False default): the endpoint's
    # useful default per the v0.2 Task 7 brief. Never a hard constraint --
    # the query param lets a caller switch it off.
    hide_free: bool = True,
) -> dict[str, Any]:
    days = _clamp_days(days)
    sort = sort if sort in CATALOGUE_SORT_FIELDS else "monthly"
    order = order if order in ("asc", "desc") else "asc"
    limit = limit if limit in CATALOGUE_LIMITS else 25
    offset = max(0, offset)
    providers_mode = providers_mode if providers_mode in ("include", "exclude") else "include"
    min_context = max(0, min_context)
    usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data = _context(days)
    return build_catalogue(
        usage_rows,
        models_dev,
        config["subscription_usd_per_month"],
        days,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
        query=query,
        providers=providers,
        providers_mode=providers_mode,
        tool_call=tool_call,
        vision=vision,
        reasoning=reasoning,
        open_weights=open_weights,
        min_context=min_context,
        hide_free=hide_free,
        usage_available=usage_available,
        usage_unavailable_reason=usage_unavailable_reason,
        pricing_data=pricing_data,
    )


#: Providers pinned on the /providers facet unconditionally, regardless of the
#: user's own billing history. openrouter carries no billing_provider rows on
#: this deployment (the user has no sessions there) but is explicitly where
#: the interesting cheap alternatives live, so it is always pinned.
_ALWAYS_PINNED_PROVIDERS = {"openrouter"}


def _pinned_providers(usage_rows: list) -> list[str]:
    """Providers the checkbox list should pin.

    Billing providers actually observed in the usage window, each mapped
    through :func:`pricing.ghost_provider` (so a subscription route like
    ``"openai-codex"`` pins ``"openai"``, the paid API that actually serves
    it), with empties dropped, plus ``openrouter`` unconditionally.

    Returned sorted so the facet is deterministic regardless of iteration
    order over *usage_rows*.
    """
    pinned = set(_ALWAYS_PINNED_PROVIDERS)
    for row in usage_rows:
        mapped = pricing.ghost_provider(row.provider)
        if mapped:
            pinned.add(mapped)
    return sorted(pinned)


def build_providers(
    usage_rows: list,
    models_dev: dict[str, Any],
    *,
    pricing_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The provider facet the catalogue's checkbox list is built from.

    Pure, like :func:`build_summary`, :func:`build_whatif` and
    :func:`build_catalogue`: no I/O, no globals, everything arrives as an
    argument. ``model_count`` counts exactly the models
    :func:`pricing.iter_catalogue` yields for that provider (i.e. priced
    ones), so the numbers agree with what ``/catalogue`` can actually show.

    Sorted pinned first, then by ``model_count`` descending, then by name
    for stability.
    """
    pinned = _pinned_providers(usage_rows)
    pinned_set = set(pinned)

    counts: dict[str, int] = {}
    for entry in pricing.iter_catalogue(models_dev):
        counts[entry.provider] = counts.get(entry.provider, 0) + 1

    provider_rows = [
        {
            "provider": provider,
            "model_count": count,
            "pinned": provider.strip().lower() in pinned_set,
        }
        for provider, count in counts.items()
    ]
    provider_rows.sort(key=lambda row: (not row["pinned"], -row["model_count"], row["provider"].lower()))

    return {
        "providers": provider_rows,
        "pinned": pinned,
        "pricing_data": pricing_data if pricing_data is not None else dict(_UNKNOWN_PRICING_DATA),
    }


@router.get("/providers")
def providers(days: int = 30) -> dict[str, Any]:
    days = _clamp_days(days)
    usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data = _context(days)
    return build_providers(usage_rows, models_dev, pricing_data=pricing_data)


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
