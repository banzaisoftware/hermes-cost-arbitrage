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
    # v0.2 Task 9: unified with the /catalogue *handler*'s own hide_free
    # default (also True). Every other filter here (tool_call, vision,
    # reasoning, open_weights, min_context, providers_mode) already has
    # matching builder and handler defaults; hide_free used to be the sole
    # exception, which meant a future direct caller of build_catalogue (one
    # that doesn't go through the /catalogue handler) would silently get free
    # models back, contradicting the product intent that hide_free is a
    # useful default everywhere, never a hard constraint -- it stays fully
    # switchable via an explicit hide_free=False.
    hide_free: bool = True,
    # v0.2 Task 9: like every filter above, "on" is a useful default, never a
    # hard constraint. Unlike them, this one depends on host state
    # (credential presence) that build_catalogue itself cannot look up --
    # staying a pure function, no I/O, no globals -- so the two pieces of
    # that state arrive as plain arguments computed once by the /catalogue
    # handler via pricing.credentialed_provider_slugs(). The defaults here
    # (empty set, unavailable) deliberately match that function's own
    # fail-open return value, so a bare call (as ~100 pre-existing tests
    # make) gets the same "could not determine -> no constraint" behaviour a
    # real host failure would produce, without needing every such test to
    # pass the new arguments.
    credentialed_only: bool = True,
    credentialed_provider_slugs: frozenset[str] | set[str] = frozenset(),
    credential_status_available: bool = False,
    # Static, not host-dependent -- see pricing.CREDENTIAL_SOURCES_CHECKED's
    # own comment for why this is a plain tuple rather than something
    # computed per call. Carried as a parameter (rather than importing
    # pricing directly here) purely so this stays a pure function whose
    # every output is traceable to an argument.
    credential_sources_checked: tuple[str, ...] = pricing.CREDENTIAL_SOURCES_CHECKED,
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

    *hide_free* (default ``True`` here and on the ``/catalogue`` handler --
    unified in v0.2 Task 9, see the parameter's own inline comment) drops
    candidates whose grid publishes ``input_per_million ==
    output_per_million == Decimal(0)`` — see :func:`_is_free_grid`. This is
    deliberately a fact about the published rates, not about the computed
    ``monthly_usd`` for the current usage window: an empty window prices
    every model to $0, and keying off that would hide the entire catalogue.
    Like every other filter here, "on" is a useful default, never a hard
    constraint -- pass ``hide_free=False`` to see free models too.

    *credentialed_only* (default ``True``, v0.2 Task 9) keeps only
    candidates whose provider (case-insensitively) is in
    *credentialed_provider_slugs* — the set of models.dev provider keys with
    a credential *present* on this host, computed once by the ``/catalogue``
    handler via :func:`pricing.credentialed_provider_slugs` and passed
    through as a plain argument so this function stays pure. **The dangerous
    failure mode**: when *credential_status_available* is ``False`` (status
    genuinely could not be determined — see that function's own docstring),
    *credentialed_only* imposes **no constraint at all**, regardless of its
    own value — a host where credential detection is simply unavailable
    must never read as "nobody has a credential" and silently empty the
    catalogue. Like every filter here, "on" is a useful default, never a
    hard constraint, and pass ``credentialed_only=False`` to see every
    provider regardless of credential status.

    *credential_sources_checked* is echoed verbatim into the envelope so a
    consumer of this payload is never left to assume ``credential_present:
    false`` means "verified absent" -- :func:`pricing.credentialed_provider_slugs`
    deliberately does not check every possible credential store (see that
    function's own "Coverage this deliberately excludes" paragraph), so
    ``false`` only ever means "not found in the stores named here."

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
    # Defensive normalization, same posture as provider_set above: the
    # contract with pricing.credentialed_provider_slugs() already guarantees
    # a lowercased set, but a caller passing anything else (tests, a future
    # direct caller) must still get the right answer.
    credentialed_set = {str(slug).strip().lower() for slug in credentialed_provider_slugs}

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
        # The dangerous failure mode: credentialed_only must impose NO
        # constraint whenever credential_status_available is False, however
        # credentialed_only itself is set -- see the docstring paragraph
        # above. Pinned by
        # test_build_catalogue_credentialed_only_defaults_true_but_imposes_no_constraint_when_status_unavailable.
        if credentialed_only and credential_status_available:
            if provider.strip().lower() not in credentialed_set:
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
            "credentialed_only": credentialed_only,
        },
        "notice": FLOOR_NOTICE,
        "usage_available": usage_available,
        "usage_unavailable_reason": usage_unavailable_reason,
        "models_dev_available": bool(models_dev),
        # Not itself a filter (hence not in "filters" above) -- tells the UI
        # whether credentialed_only actually had any opportunity to
        # constrain anything, same role model_dev_available already plays
        # for the catalogue as a whole.
        "credential_status_available": credential_status_available,
        # Which local credential stores were actually consulted (see
        # pricing.CREDENTIAL_SOURCES_CHECKED) -- so a consumer of this payload
        # can tell "not found in the stores we checked" apart from "verified
        # absent everywhere" for every credential_present-adjacent field.
        "credential_sources_checked": list(credential_sources_checked),
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
    # True here, matching build_catalogue's own default since v0.2 Task 9
    # (they used to disagree -- see that task's report). The endpoint's
    # useful default per the v0.2 Task 7 brief. Never a hard constraint --
    # the query param lets a caller switch it off.
    hide_free: bool = True,
    # v0.2 Task 9: True to match build_catalogue's own default. Never a hard
    # constraint -- the query param lets a caller switch it off. See
    # pricing.credentialed_provider_slugs() for what "credentialed" means
    # here (a credential is present, never verified as working) and for why
    # it is computed locally rather than by calling Hermes'
    # get_authenticated_provider_slugs (that convenience wrapper is not
    # network-free -- see that function's own docstring for the full
    # evidence trail).
    credentialed_only: bool = True,
) -> dict[str, Any]:
    days = _clamp_days(days)
    sort = sort if sort in CATALOGUE_SORT_FIELDS else "monthly"
    order = order if order in ("asc", "desc") else "asc"
    limit = limit if limit in CATALOGUE_LIMITS else 25
    offset = max(0, offset)
    providers_mode = providers_mode if providers_mode in ("include", "exclude") else "include"
    min_context = max(0, min_context)
    usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data = _context(days)
    credentialed_slugs, credential_status_available = pricing.credentialed_provider_slugs()
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
        credentialed_only=credentialed_only,
        credentialed_provider_slugs=credentialed_slugs,
        credential_status_available=credential_status_available,
        credential_sources_checked=pricing.CREDENTIAL_SOURCES_CHECKED,
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
    # v0.2 Task 9: same "computed once by the handler, passed through as a
    # plain argument" shape as build_catalogue's credentialed_only support,
    # and the same fail-open defaults (empty set, unavailable) so this stays
    # pure and every pre-existing bare call keeps working unchanged.
    credentialed_provider_slugs: frozenset[str] | set[str] = frozenset(),
    credential_status_available: bool = False,
    # Static, not host-dependent -- see build_catalogue's identical
    # parameter and pricing.CREDENTIAL_SOURCES_CHECKED's own comment.
    credential_sources_checked: tuple[str, ...] = pricing.CREDENTIAL_SOURCES_CHECKED,
) -> dict[str, Any]:
    """The provider facet the catalogue's checkbox list is built from.

    Pure, like :func:`build_summary`, :func:`build_whatif` and
    :func:`build_catalogue`: no I/O, no globals, everything arrives as an
    argument. ``model_count`` counts exactly the models
    :func:`pricing.iter_catalogue` yields for that provider (i.e. priced
    ones), so the numbers agree with what ``/catalogue`` can actually show.

    Sorted pinned first, then by ``model_count`` descending, then by name
    for stability.

    ``credential_present`` (v0.2 Task 9) mirrors
    :func:`build_catalogue`'s ``credentialed_only`` filter: ``True`` when
    the row's provider (case-insensitively) is in
    *credentialed_provider_slugs*, else ``False``. When
    *credential_status_available* is ``False`` the set is always empty by
    contract (see :func:`pricing.credentialed_provider_slugs`), so every row
    reads ``False`` here too -- but that must be read by the UI as "unknown",
    never as "verified nobody has a credential"; ``credential_status_available``
    in the returned envelope carries that distinction. Even when
    *credential_status_available* is ``True``, a row's ``False`` only means
    "not found in *credential_sources_checked*" -- also echoed in the
    envelope -- never "verified absent from every possible credential store"
    (see :func:`pricing.credentialed_provider_slugs`'s own documented
    exclusions).
    """
    pinned = _pinned_providers(usage_rows)
    pinned_set = set(pinned)
    # Defensive normalization, same posture as build_catalogue's
    # credentialed_set: the contract already guarantees lowercased slugs,
    # but a caller passing anything else still gets the right answer.
    credentialed_set = {str(slug).strip().lower() for slug in credentialed_provider_slugs}

    counts: dict[str, int] = {}
    for entry in pricing.iter_catalogue(models_dev):
        counts[entry.provider] = counts.get(entry.provider, 0) + 1

    provider_rows = [
        {
            "provider": provider,
            "model_count": count,
            "pinned": provider.strip().lower() in pinned_set,
            "credential_present": provider.strip().lower() in credentialed_set,
        }
        for provider, count in counts.items()
    ]
    provider_rows.sort(key=lambda row: (not row["pinned"], -row["model_count"], row["provider"].lower()))

    return {
        "providers": provider_rows,
        "pinned": pinned,
        "credential_status_available": credential_status_available,
        "credential_sources_checked": list(credential_sources_checked),
        "pricing_data": pricing_data if pricing_data is not None else dict(_UNKNOWN_PRICING_DATA),
    }


@router.get("/providers")
def providers(days: int = 30) -> dict[str, Any]:
    days = _clamp_days(days)
    usage_rows, models_dev, config, usage_available, usage_unavailable_reason, pricing_data = _context(days)
    credentialed_slugs, credential_status_available = pricing.credentialed_provider_slugs()
    return build_providers(
        usage_rows,
        models_dev,
        pricing_data=pricing_data,
        credentialed_provider_slugs=credentialed_slugs,
        credential_sources_checked=pricing.CREDENTIAL_SOURCES_CHECKED,
        credential_status_available=credential_status_available,
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


@router.post("/switch-model")
def switch_model_endpoint(payload: dict = Body(default={})) -> dict[str, Any]:
    """Switch the agent's active model, through the host's own validated path.

    This is the only endpoint that writes to the *host's* configuration, and
    the only place this plugin is not read-only against Hermes. It exists so a
    model found in the catalogue can be adopted without hand-editing YAML: the
    dashboard's own picker offers a hand-curated 33-model OpenRouter allowlist
    (``hermes_cli/models.py:35-84``), while the catalogue prices thousands.

    The chain below mirrors what the dashboard's own Switch button does, and
    was traced in the host source rather than assumed:

    - ``hermes_cli.model_switch.switch_model`` (``model_switch.py:669``)
      resolves and validates but **persists nothing** — there is no config
      write anywhere in that module.
    - ``hermes_cli.model_cost_guard.expensive_model_warning`` gates on
      published rates (>$20/M input or >$100/M output) and, when it fires,
      the host writes nothing until the caller re-submits with confirmation.
    - ``tui_gateway/server.py:_persist_model_switch`` then sets
      ``model.default`` / ``model.provider`` / ``model.base_url`` and calls
      ``hermes_cli.config.save_config``. That helper is private to the
      gateway, so the same handful of assignments are reproduced here against
      the *public* ``save_config``. We never touch the YAML ourselves and
      never call ``set_config_value``, which validates nothing at all.

    Two things the caller is owed and the host's own response does not give:
    ``previous``, so the change can be reversed from this response alone, and
    ``warning`` verbatim — the host accepts a model it could not confirm
    exists, and rendering that as a plain success would hide a real doubt.

    ``result.api_key`` is deliberately never echoed.

    Note for anyone extending this: ``save_config`` rewrites ``config.yaml``
    from the parsed dict via ``yaml.dump``, re-appending only Hermes' own
    boilerplate comment blocks. Hand-written comments in that file do not
    survive any Hermes config write — this endpoint included.
    """
    data = payload if isinstance(payload, dict) else {}
    model = data.get("model")
    provider = data.get("provider")
    model = model.strip() if isinstance(model, str) else ""
    provider = provider.strip() if isinstance(provider, str) else ""
    confirm_expensive = data.get("confirm_expensive") is True

    def _reply(**over: Any) -> dict[str, Any]:
        """One stable response shape on every branch."""
        base: dict[str, Any] = {
            "ok": False,
            "confirm_required": False,
            "detail": None,
            "warning": None,
            "confirm_message": None,
            "guard_ran": False,
            "previous": None,
            "current": None,
            "target": None,
        }
        base.update(over)
        return base

    if not model:
        return _reply(detail="a model is required")

    # Only the names the endpoint cannot work without. Anything used purely to
    # enrich the call is imported inside its own tolerant block below, so an
    # older host missing an optional helper degrades instead of taking the whole
    # endpoint offline.
    try:
        from hermes_cli.config import is_managed, load_config, read_raw_config, save_config
        from hermes_cli.model_switch import switch_model as _host_switch_model
    except Exception as exc:
        return _reply(detail=f"hermes_cli is not importable: {exc}")

    # Refuse up front on a managed install. ``save_config`` returns None there
    # without raising (``hermes_cli/config.py:5831-5833``), so writing first and
    # trusting the absence of an exception would report a switch that never
    # happened. The gateway survives this because it also switches the live
    # agent in-process; this endpoint has only the write.
    managed_check_error = ""
    try:
        if is_managed():
            return _reply(
                detail=(
                    "this Hermes install is package-manager managed, so its configuration "
                    "is read-only — change the model through your system configuration instead"
                )
            )
    except Exception as exc:
        # Not fatal: save_config calls is_managed() itself and will decline, and
        # the post-write read-back catches that. But keep the reason so the
        # caller is not left with a bare "the write was refused".
        managed_check_error = str(exc)

    # The write path reads the *raw* user config, never load_config(). load_config
    # deep-merges DEFAULT_CONFIG and stamps _config_version, so saving its result
    # back would pin every current default into the user's file and permanently
    # skip future migrations. ``set_config_value`` carries the same warning at
    # ``hermes_cli/config.py:6637``. The gateway's ``_load_cfg`` is raw for the
    # same reason.
    def _current(raw: dict[str, Any]) -> dict[str, str]:
        node = raw.get("model")
        if isinstance(node, str):  # the host also accepts a scalar `model:` key
            return {"model": node.strip(), "provider": "", "base_url": ""}
        node = node if isinstance(node, dict) else {}
        return {
            "model": str(node.get("default") or ""),
            "provider": str(node.get("provider") or ""),
            "base_url": str(node.get("base_url") or ""),
        }

    try:
        previous = _current(read_raw_config())
    except Exception as exc:
        return _reply(detail=f"could not read the current model: {exc}")

    # Read-only use of the merged config, which is correct here: the resolver
    # needs user-defined providers or it re-resolves from scratch and can hop to
    # an aggregator, persisting a base_url that points at the wrong endpoint.
    user_providers = None
    custom_providers = None
    try:
        from hermes_cli.config import get_compatible_custom_providers

        merged = load_config()
        user_providers = merged.get("providers")
        custom_providers = get_compatible_custom_providers(merged)
    except Exception:
        pass

    try:
        result = _host_switch_model(
            model,
            previous["provider"],
            previous["model"],
            current_base_url=previous["base_url"],
            explicit_provider=provider,
            is_global=True,
            user_providers=user_providers,
            custom_providers=custom_providers,
        )
    except Exception as exc:
        return _reply(detail=f"model switch failed: {exc}", previous=previous)

    if not getattr(result, "success", False):
        return _reply(
            detail=str(getattr(result, "error_message", "") or "model switch failed"),
            previous=previous,
        )

    new_model = str(getattr(result, "new_model", "") or "")
    target_provider = str(getattr(result, "target_provider", "") or "")
    base_url = str(getattr(result, "base_url", "") or "")
    target = {"model": new_model, "provider": target_provider}

    guard_ran = False
    if not confirm_expensive:
        warning = None
        try:
            from hermes_cli.model_cost_guard import expensive_model_warning

            warning = expensive_model_warning(
                new_model,
                provider=target_provider,
                base_url=base_url or previous["base_url"],
                api_key=getattr(result, "api_key", "") or "",
                model_info=getattr(result, "model_info", None),
            )
            guard_ran = True
        except Exception:
            # Reported rather than swallowed. This plugin exists to browse
            # thousands of unfamiliar models, so the guard is the only brake
            # between a click and a $150/M model — a caller that cannot see it
            # failed would present a silent success.
            guard_ran = False
        if warning is not None:
            return _reply(
                confirm_required=True,
                confirm_message=str(getattr(warning, "message", "") or ""),
                guard_ran=True,
                previous=previous,
                target=target,
            )

    try:
        raw = read_raw_config()
        model_cfg = raw.get("model")
        if not isinstance(model_cfg, dict):
            model_cfg = {}
            raw["model"] = model_cfg
        model_cfg["default"] = new_model
        model_cfg["provider"] = target_provider
        if base_url:
            model_cfg["base_url"] = base_url
        else:
            model_cfg.pop("base_url", None)
        save_config(raw)
    except Exception as exc:
        return _reply(detail=f"the switch resolved but could not be saved: {exc}", previous=previous)

    # save_config can decline silently — managed scope, or a pinned key stripped
    # by _strip_dotted_keys. Read it back rather than infer success from the
    # absence of an exception.
    #
    # Every leaf we wrote is verified, not just the model. Managed scope is
    # per-key (``hermes_cli/managed_scope.py``) and is *distinct* from
    # is_managed() — an admin can pin model.provider and model.base_url while
    # leaving model.default writable, which is the natural "any model you like,
    # but only through our gateway" policy. Checking the model alone would let
    # that land as ok:true with a misreported provider and, worse, a config
    # pairing the new model id with the old endpoint.
    try:
        written = _current(read_raw_config())
    except Exception as exc:
        return _reply(detail=f"the switch was written but could not be confirmed: {exc}", previous=previous)

    def _refused(key: str, got: str, want: str) -> dict[str, Any]:
        detail = (
            f"the switch resolved but the configuration still reads {key}="
            f"{got or 'nothing'} instead of {want or 'nothing'} — the write was refused"
        )
        if managed_check_error:
            detail += f" (the managed-install check could not run: {managed_check_error})"
        return _reply(detail=detail, previous=previous)

    if written["model"] != new_model:
        return _refused("model", written["model"], new_model)
    if written["provider"] != target_provider:
        return _refused("provider", written["provider"], target_provider)
    # base_url leniently: save_config legitimately restores a ``${VAR}`` template
    # over the expanded value it was given (_preserve_env_ref_templates), so a
    # template on disk is a match, not a refusal.
    if written["base_url"] != base_url and "${" not in written["base_url"]:
        return _refused("base_url", written["base_url"], base_url)

    return _reply(
        ok=True,
        warning=str(getattr(result, "warning_message", "") or "") or None,
        # True only when the host's cost guard actually executed. False covers
        # both "it raised" and "the caller passed confirm_expensive and skipped
        # it" — either way the brake did not engage on this switch.
        guard_ran=guard_ran,
        previous=previous,
        current=target,
        target=target,
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
