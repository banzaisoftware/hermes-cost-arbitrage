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
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Optional

from .cost_engine import PricingGrid

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


#: Returned by :func:`models_dev_freshness` whenever the cache's age can't be
#: established — a missing file, a permission error, or a clock anomaly. The
#: UI must never render an age it can't trust.
_UNAVAILABLE_FRESHNESS: dict[str, Any] = {
    "updated_at": None,
    "age_hours": None,
    "available": False,
}


def models_dev_freshness(path: Path | str) -> dict[str, Any]:
    """How old is the local models.dev cache, derived from its mtime.

    Hermes refreshes ``$HERMES_HOME/models_dev_cache.json`` in the background
    every 60 minutes; nothing here does any I/O beyond a single ``stat`` call
    (no network, no reading the file's contents).

    Fail-open like :func:`load_models_dev`: a missing file, a permission
    error, or a clock anomaly (the file's mtime sits in the future, which
    would otherwise report a nonsensical negative age) all yield
    ``{"updated_at": None, "age_hours": None, "available": False}`` rather
    than raising. This function must never raise.
    """
    try:
        mtime = Path(path).stat().st_mtime
        now = datetime.now(timezone.utc).timestamp()
        age_seconds = now - mtime
        if age_seconds < 0:
            return dict(_UNAVAILABLE_FRESHNESS)
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        return {
            "updated_at": updated_at,
            "age_hours": age_seconds / 3600.0,
            "available": True,
        }
    except Exception:
        return dict(_UNAVAILABLE_FRESHNESS)


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


def _tier_grid_from_models_dev(
    model: str, provider: str, models_dev: dict[str, Any]
) -> tuple[Optional[PricingGrid], Optional[int]]:
    """The long-context tier grid and its threshold for *model*, or ``(None, None)``.

    Prefers ``cost.tiers`` — a list of tier blocks, each carrying its own
    ``tier.size`` threshold — over the fixed-name key ``context_over_200k``,
    because the real production cache disagrees with itself: `gpt-5.5`'s
    ``tiers[0].tier.size`` is ``272000`` while its ``context_over_200k`` key
    implies ``200000``. ``tiers`` carries the actual threshold, so it wins
    whenever a usable entry is present; ``context_over_200k`` (with its
    implied 200 000 threshold) is only consulted when ``tiers`` is absent or
    contains no usable ``"context"``-typed entry.

    A ``tiers`` entry whose ``tier.type`` isn't ``"context"`` is ignored —
    models.dev reuses the same list shape for other tier kinds this plugin
    does not model (e.g. volume tiers). Fails open exactly like
    :func:`_grid_from_models_dev`: a malformed shape at any level (``cost``
    not a dict, ``tiers`` not a list, a tier entry not a dict, a non-numeric
    ``size``, ...) is treated as "no usable tier" for that entry, never an
    exception.
    """
    try:
        entry = ((models_dev.get(provider) or {}).get("models") or {}).get(model)
        if not isinstance(entry, dict):
            return None, None
        cost = entry.get("cost")
        if not isinstance(cost, dict):
            return None, None

        tiers = cost.get("tiers")
        if isinstance(tiers, list):
            for tier_entry in tiers:
                if not isinstance(tier_entry, dict):
                    continue
                tier_meta = tier_entry.get("tier")
                if not isinstance(tier_meta, dict) or tier_meta.get("type") != "context":
                    continue
                size = tier_meta.get("size")
                if size is None or isinstance(size, bool):
                    continue
                try:
                    threshold = int(size)
                except (TypeError, ValueError):
                    continue
                grid = PricingGrid(
                    input_per_million=_decimal(tier_entry.get("input")),
                    output_per_million=_decimal(tier_entry.get("output")),
                    cache_read_per_million=_decimal(tier_entry.get("cache_read")),
                    cache_write_per_million=_decimal(tier_entry.get("cache_write")),
                    source="models.dev-tier",
                )
                if grid.is_priced:
                    return grid, threshold
                # This particular entry's rates are unusable; keep scanning
                # in case a later tiers entry is usable rather than giving up.
                continue

        fallback = cost.get("context_over_200k")
        if isinstance(fallback, dict):
            grid = PricingGrid(
                input_per_million=_decimal(fallback.get("input")),
                output_per_million=_decimal(fallback.get("output")),
                cache_read_per_million=_decimal(fallback.get("cache_read")),
                cache_write_per_million=_decimal(fallback.get("cache_write")),
                source="models.dev-tier",
            )
            if grid.is_priced:
                return grid, 200_000

        return None, None
    except (AttributeError, TypeError):
        return None, None


def resolve_tier_grid(
    model: str, provider: Optional[str], models_dev: dict[str, Any]
) -> tuple[Optional[PricingGrid], Optional[int]]:
    """Best available long-context tier grid for *model*, or ``(None, None)``.

    Mirrors :func:`resolve_grid`'s provider rewrite (:func:`ghost_provider`)
    so a subscription route's tier is read from the same paid-API entry as
    its base grid. Tiers are modelled by models.dev only — Hermes' own
    offline pricing table (``agent.usage_pricing``) carries no tier
    information, so unlike :func:`resolve_grid` this never consults it.
    """
    paid_provider = ghost_provider(provider)
    return _tier_grid_from_models_dev(model, paid_provider, models_dev)


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
    raw_source = (entry.source or "").strip().lower()
    grid = PricingGrid(
        input_per_million=entry.input_cost_per_million,
        output_per_million=entry.output_cost_per_million,
        cache_read_per_million=entry.cache_read_cost_per_million,
        cache_write_per_million=entry.cache_write_cost_per_million,
        source=entry.source if raw_source and raw_source != "none" else "hermes",
    )
    # A subscription route would slip through as an all-zero grid; treat that
    # as "no usable pricing" so the models.dev fallback gets its turn.
    if not grid.is_priced or grid.input_per_million == Decimal(0):
        return None
    return grid


#: Exactly which local credential sources :func:`credentialed_provider_slugs`
#: consults, in the order it consults them. Exposed verbatim on both the
#: ``/catalogue`` and ``/providers`` JSON payloads as
#: ``credential_sources_checked`` so a consumer of the API can tell "not
#: found in the stores we checked" apart from "verified absent everywhere"
#: -- see that function's own "Coverage this deliberately excludes"
#: paragraph for what is NOT in this list, and why a provider credentialed
#: only through one of those excluded stores (``bedrock`` via the AWS SDK
#: chain, say) reads as ``credential_present: false`` despite genuinely
#: having a credential.
#:
#: A plain static tuple rather than something computed per call or per
#: provider: which sources get checked never varies by provider or by call
#: in this implementation, so a static list keeps the payload honest without
#: inventing a per-provider tri-state this project doesn't have the signal
#: to support responsibly (considered and rejected -- see the v0.2 Task 9
#: report for the reasoning).
CREDENTIAL_SOURCES_CHECKED: tuple[str, ...] = (
    "env_vars",
    "auth_store.credential_pool",
    "auth_store.providers",
)


def credentialed_provider_slugs() -> tuple[set[str], bool]:
    """Provider slugs (models.dev catalogue keys, lowercased) with a
    credential *present* -- never *verified*. Backs the ``/catalogue``
    ``credentialed_only`` filter and the ``/providers`` ``credential_present``
    field (v0.2 Task 9).

    Returns ``(slugs, could_determine)``. Callers MUST branch on
    ``could_determine``, never on whether ``slugs`` is empty:

    - ``could_determine is False`` means credential status genuinely could
      not be established -- ``hermes_cli.auth`` isn't importable (true on
      this development machine; only the production host has it) or reading
      its local auth store raised. ``slugs`` is always ``set()`` here, but
      that must never be read as "verified nobody has a credential" -- any
      credential-based filter must impose **no constraint** in this case
      (see ``build_catalogue``'s ``credentialed_only`` handling), exactly
      like the dangerous-failure-mode guard on every other filter in this
      plugin (``build_catalogue``'s own docstring on ``hide_free``,
      ``min_context``, etc.).
    - ``could_determine is True`` means the checks below ran to completion;
      ``slugs`` is whatever they found (possibly still genuinely empty).

    Deliberately does **not** call
    ``hermes_cli.model_switch.get_authenticated_provider_slugs`` (or
    ``list_authenticated_providers``), even though that is the obvious,
    already-existing convenience wrapper for "which providers have
    credentials" -- because it is not the network-free presence check its
    own docstring claims to be. Its full body
    (``hermes_cli/model_switch.py:1243-2097``) unconditionally calls
    ``fetch_models_dev()`` (a live HTTP GET to models.dev whenever the
    shared ``models_dev_cache.json`` this plugin also reads via
    :func:`load_models_dev` is more than an hour stale -- a routine
    condition, not an edge case), ``fetch_ollama_cloud_models()`` (a live
    API probe on a stale disk cache), and ``get_curated_nous_model_ids()``
    (a live remote-manifest fetch on a stale disk cache) -- all *before* it
    even reaches the credential check itself. Wiring that into a dashboard
    GET handler would violate this plugin's no-network-I/O-in-a-GET rule.
    **Do not "simplify" this back into a call to that function** without
    re-auditing the same code path for network calls in whatever Hermes
    version is running then -- that is the whole reason this function looks
    like a reimplementation instead of a one-line delegation.

    Reimplements the same *local* presence check Hermes itself performs,
    mirroring ``hermes_cli/model_switch.py:1451-1477`` (the ``api_key``
    path) and ``:1544-1550`` (the "everything else" path), against two
    purely local sources:

    - ``hermes_cli.auth.PROVIDER_REGISTRY`` -- a module-level dict literal of
      ``ProviderConfig`` objects, BUT importing ``hermes_cli.auth`` is not
      itself inert. **Re-check this on every host upgrade** -- audited as
      network-free against ``hermes-agent`` v2026.6.19 only, not guaranteed
      for any other version:

      - ``hermes_cli/auth.py:453-481`` eagerly (at import time, inside a
        bare ``try/except Exception: pass``) calls
        ``providers.list_providers()``, which imports the ``__init__.py`` of
        *every* directory under bundled ``plugins/model-providers/*`` (29 at
        v2026.6.19, each read and confirmed to execute no network call at
        import time -- one, ``plugins/model-providers/anthropic/__init__.py:16-36``,
        *defines* a ``fetch_models()`` that calls
        ``urllib.request.urlopen``, but only as an uninvoked method body, not
        at import) **and every directory under the user-writable
        ``$HERMES_HOME/plugins/model-providers/*``** (``providers/__init__.py:91-171``).
        The bundled set was fully audited; a user-supplied plugin in the
        second location is arbitrary third-party code this plugin has no way
        to audit or control, and could in principle do anything at import
        time, including network I/O -- this function's network-free
        guarantee is only as good as whatever the host operator has dropped
        in that directory.
      - Importing ``hermes_cli.auth`` also pulls in ``hermes_cli.config``
        (``hermes_cli/auth.py:46``), which runs its own eager,
        import-time call, ``_inject_profile_env_vars()``
        (``hermes_cli/config.py:6828-6859``). That function itself calls
        ``providers.list_providers()`` -- reading ``.auth_type``/``.env_vars``
        off the already-registered ``ProviderProfile`` objects, no YAML
        involved -- and because it runs before ``hermes_cli.auth``'s own
        ``list_providers()`` call at ``:453``, while
        ``providers/__init__.py``'s ``_discovered`` guard
        (``providers/__init__.py:140-154``) is still ``False``, it is in
        fact the *first* thing that triggers the real bundled-plus-user
        provider-plugin scan described above -- not a separate mechanism
        running alongside it. A second, genuinely separate injector,
        ``_inject_platform_plugin_env_vars()``
        (``hermes_cli/config.py:6885-6956``), reads a different manifest
        category -- ``plugins/platforms/*/plugin.yaml`` (platform
        integrations like Teams/IRC, unrelated to model-providers) via
        ``yaml.safe_load`` -- genuine local file I/O, not network.

      None of this changes the conclusion (network-free today, for the
      bundled set, at v2026.6.19) but it is a materially larger and more
      dynamic import surface than "a dict literal," and the next person to
      re-audit this needs to know to look at *all* of the above, not just
      re-read the registry's own source.
    - ``hermes_cli.auth._load_auth_store()`` -- reads
      ``$HERMES_HOME/auth.json`` and returns
      ``{"version": ..., "providers": {}}`` when the file is absent. No
      network. One side effect worth knowing, not this plugin's own but
      Hermes': if the file exists and fails to parse as JSON, Hermes'
      loader copies it to a sibling ``*.json.corrupt`` file before falling
      back to an empty store (``hermes_cli/auth.py:1056-1061``) -- a write,
      but one this plugin neither requests nor controls, and it only fires
      on an already-corrupt file (this plugin is otherwise strictly
      read-only and never touches ``state.db``).

    For each ``PROVIDER_REGISTRY`` entry, a credential is considered
    *present* when, reading only from the single ``_load_auth_store()``
    call above (no further I/O) -- these three checks are exactly
    :data:`CREDENTIAL_SOURCES_CHECKED`, in order:

    - its ``auth_type == "api_key"`` and any of its ``api_key_env_vars`` is
      set in the environment, or
    - its id is a key in the auth store's ``credential_pool`` dict, or
    - its id is a key in the auth store's ``providers`` dict.

    **Coverage this deliberately excludes**, because each would need its
    own network-I/O audit this task didn't request: the deeper
    ``agent.credential_pool.load_pool(...).has_credentials()`` fallback
    (which auto-seeds from external CLI config files), the
    Anthropic-specific external credential file reads (Claude Code / Hermes
    OAuth token files), and the AWS SDK credential-chain check for
    ``bedrock`` (``agent.bedrock_adapter.has_aws_credentials()``, which can
    itself reach the EC2/ECS instance-metadata service over the network). A
    provider whose only real credential lives in one of those three places
    reads as "no credential present" here even though Hermes itself would
    show it as configured -- an honest under-count, never an over-count,
    consistent with fail-open.

    Hermes' own provider slugs and models.dev's catalogue provider keys are
    separate namespaces that do not always agree (e.g. Hermes' ``"copilot"``
    is models.dev's ``"github-copilot"``). ``agent.models_dev.PROVIDER_TO_MODELS_DEV``
    is Hermes' own authoritative mapping table for this, used here with a
    same-slug (lowercased) fallback for any ``PROVIDER_REGISTRY`` id the
    table doesn't cover, since most uncovered ids still turn out to be
    identical (e.g. ``"openai"`` -> ``"openai"``). See the v0.2 Task 9
    report for exactly which ids were confirmed to differ versus which are
    an unverified identity guess.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, _load_auth_store
    except Exception:
        return set(), False

    try:
        from agent.models_dev import PROVIDER_TO_MODELS_DEV
    except Exception:
        PROVIDER_TO_MODELS_DEV: dict[str, str] = {}

    try:
        store = _load_auth_store() or {}
        providers_configured = store.get("providers")
        if not isinstance(providers_configured, dict):
            providers_configured = {}
        credential_pool = store.get("credential_pool")
        if not isinstance(credential_pool, dict):
            credential_pool = {}

        present: set[str] = set()
        for hermes_id, config in PROVIDER_REGISTRY.items():
            has_creds = False
            if getattr(config, "auth_type", "") == "api_key":
                env_vars = getattr(config, "api_key_env_vars", None) or ()
                has_creds = any(os.environ.get(ev) for ev in env_vars)
            if not has_creds and hermes_id in credential_pool:
                has_creds = True
            if not has_creds and hermes_id in providers_configured:
                has_creds = True
            if not has_creds:
                continue
            catalogue_key = PROVIDER_TO_MODELS_DEV.get(hermes_id, hermes_id)
            present.add(str(catalogue_key).strip().lower())
        return present, True
    except Exception:
        return set(), False


@dataclass(frozen=True)
class CatalogueCapabilities:
    """What a model can do, extracted from its models.dev entry.

    Every field is fail-open: a missing or malformed source field yields
    ``False`` (or ``None`` for ``context_limit``), never an exception. This
    mirrors :func:`_grid_from_models_dev`'s tolerance of a malformed cache —
    a capability we can't establish is treated as absent, not as a crash.

    ``vision`` has no ``vision`` key of its own in models.dev; it is derived
    from ``"image" in modalities.input``.
    """

    tool_call: bool = False
    vision: bool = False
    reasoning: bool = False
    open_weights: bool = False
    context_limit: Optional[int] = None


@dataclass(frozen=True)
class CatalogueEntry:
    """One priced, capability-tagged row of the models.dev catalogue.

    ``tier_grid`` and ``tier_threshold_tokens`` (v0.2 Task 4) are ``None``
    together whenever the model publishes no long-context tier — the ~93% of
    the catalogue with no tier key at all. Never a synthesised zero.
    """

    provider: str
    model: str
    grid: PricingGrid
    capabilities: CatalogueCapabilities
    tier_grid: Optional[PricingGrid] = None
    tier_threshold_tokens: Optional[int] = None


def _bool_capability(entry: dict[str, Any], key: str) -> bool:
    # Strict identity check rather than `bool(value)`: models.dev's own
    # fields are always genuine booleans, so a value that isn't literally
    # `True` — a stray string, a number, `None`, a missing key — is
    # malformed or absent and must fail open to False rather than being
    # coerced by Python's usual truthiness (which would turn any non-empty
    # string into True).
    try:
        return entry.get(key, False) is True
    except Exception:
        return False


def _vision_capability(entry: dict[str, Any]) -> bool:
    try:
        modalities = entry.get("modalities")
        if not isinstance(modalities, dict):
            return False
        input_modalities = modalities.get("input")
        if not isinstance(input_modalities, (list, tuple, set)):
            return False
        return "image" in input_modalities
    except Exception:
        return False


def _context_limit_capability(entry: dict[str, Any]) -> Optional[int]:
    try:
        limit = entry.get("limit")
        if not isinstance(limit, dict):
            return None
        context = limit.get("context")
        if context is None or isinstance(context, bool):
            return None
        return int(context)
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


def _capabilities_from_models_dev(model: str, provider: str, models_dev: dict[str, Any]) -> CatalogueCapabilities:
    try:
        entry = ((models_dev.get(provider) or {}).get("models") or {}).get(model)
        if not isinstance(entry, dict):
            return CatalogueCapabilities()
        return CatalogueCapabilities(
            tool_call=_bool_capability(entry, "tool_call"),
            vision=_vision_capability(entry),
            reasoning=_bool_capability(entry, "reasoning"),
            open_weights=_bool_capability(entry, "open_weights"),
            context_limit=_context_limit_capability(entry),
        )
    except (AttributeError, TypeError):
        return CatalogueCapabilities()


def iter_catalogue(models_dev: dict[str, Any]) -> Iterator[CatalogueEntry]:
    """Walk every provider/model in the local models.dev cache.

    Yields a :class:`CatalogueEntry` only for entries that resolve to a
    priced grid (see :func:`PricingGrid.is_priced`). Reuses
    :func:`_grid_from_models_dev` for the actual grid resolution so the two
    never drift apart, and so this walk inherits that function's fail-open
    guard for malformed nested shapes (a corrupt ``cost`` block, a model
    entry that isn't a dict, ...) without duplicating it. Capability
    extraction (:func:`_capabilities_from_models_dev`) and tier extraction
    (:func:`_tier_grid_from_models_dev`) each carry the same fail-open
    guarantee independently.

    Tolerant of a malformed cache at every level of the walk itself: a
    provider entry that isn't a dict, or a ``models`` block that isn't a
    dict, is skipped rather than raised — the rest of the catalogue still
    yields. An empty or non-dict cache yields nothing.
    """
    if not isinstance(models_dev, dict):
        return

    for provider, provider_entry in models_dev.items():
        try:
            models = provider_entry.get("models")
        except AttributeError:
            continue
        if not isinstance(models, dict):
            continue

        for model in models:
            grid = _grid_from_models_dev(model, provider, models_dev)
            if grid is not None:
                capabilities = _capabilities_from_models_dev(model, provider, models_dev)
                tier_grid, tier_threshold_tokens = _tier_grid_from_models_dev(model, provider, models_dev)
                yield CatalogueEntry(provider, model, grid, capabilities, tier_grid, tier_threshold_tokens)


def resolve_grid(model: str, provider: Optional[str], models_dev: dict[str, Any]) -> PricingGrid:
    """Best available paid-API pricing for *model*, never a subscription zero."""
    paid_provider = ghost_provider(provider)
    return (
        _grid_from_hermes(model, paid_provider)
        or _grid_from_models_dev(model, paid_provider, models_dev)
        or PricingGrid(source="unknown")
    )
