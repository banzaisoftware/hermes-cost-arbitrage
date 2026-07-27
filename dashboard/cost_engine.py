"""Pure cost engine: price a usage vector against a pricing grid.

No I/O, no database, no dashboard — everything this module needs arrives as
arguments. That purity is what makes it unit-testable in isolation, and what
makes the v0.2 expansion (the same engine over the full catalogue) cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class UsageVector:
    """Token counts for one model over one window."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class PricingGrid:
    """USD per million tokens. ``None`` means the rate is not published."""

    input_per_million: Optional[Decimal] = None
    output_per_million: Optional[Decimal] = None
    cache_read_per_million: Optional[Decimal] = None
    cache_write_per_million: Optional[Decimal] = None
    source: str = "unknown"

    @property
    def has_cache_pricing(self) -> bool:
        return self.cache_read_per_million is not None

    @property
    def is_priced(self) -> bool:
        return self.input_per_million is not None and self.output_per_million is not None


@dataclass(frozen=True)
class ScenarioCost:
    """Both cache scenarios for one (usage, grid) pair.

    ``headline_usd`` is the figure the UI shows large: cache-aware when the
    provider publishes a cache rate, no-cache otherwise. It is never rendered
    without ``cache_status`` next to it.
    """

    cache_aware_usd: Optional[Decimal]
    no_cache_usd: Optional[Decimal]
    headline_usd: Optional[Decimal]
    cache_status: str  # "priced" | "unknown"
    status: str  # "ok" | "no_pricing"
    source: str


def _cost(tokens: int, per_million: Optional[Decimal]) -> Decimal:
    if not tokens or per_million is None:
        return Decimal(0)
    return (Decimal(tokens) * per_million) / MILLION


def price_usage(usage: UsageVector, grid: PricingGrid) -> ScenarioCost:
    """Price *usage* against *grid*, always returning both cache scenarios."""
    if not grid.is_priced:
        return ScenarioCost(None, None, None, "unknown", "no_pricing", grid.source)

    # The world where the provider has no prompt cache at all: every prompt
    # token, cached or not, is billed at the full input rate.
    prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    no_cache = _cost(prompt_tokens, grid.input_per_million) + _cost(
        usage.output_tokens, grid.output_per_million
    )

    if not grid.has_cache_pricing:
        return ScenarioCost(None, no_cache, no_cache, "unknown", "ok", grid.source)

    # A provider that prices cache reads but not cache writes bills writes at
    # the full input rate.
    cache_write_rate = (
        grid.cache_write_per_million
        if grid.cache_write_per_million is not None
        else grid.input_per_million
    )
    cache_aware = (
        _cost(usage.input_tokens, grid.input_per_million)
        + _cost(usage.output_tokens, grid.output_per_million)
        + _cost(usage.cache_read_tokens, grid.cache_read_per_million)
        + _cost(usage.cache_write_tokens, cache_write_rate)
    )
    return ScenarioCost(cache_aware, no_cache, cache_aware, "priced", "ok", grid.source)


def price_long_context(usage: UsageVector, tier_grid: Optional[PricingGrid]) -> Optional[Decimal]:
    """What *usage* would cost if every call in it were priced at *tier_grid*.

    This is the long-context upper bound (v0.2 Task 4): some providers
    publish a second, higher rate that applies above a context-size
    threshold, but the ``sessions`` table only stores aggregate token
    counts per window — there is no per-call context size recorded anywhere
    this plugin can read. The real split between "billed at the base rate"
    and "billed at the tier rate" is therefore unknowable, so this
    deliberately does not attempt to estimate it. Instead it prices the
    *entire* usage vector at the tier grid's rates, answering "what would
    this have cost in the worst case, if every single call had landed above
    the threshold" — an upper bound, never a prediction.

    ``None`` when there is no tier grid (the model publishes no tier) or
    when the tier grid itself carries no usable rates — fail-open, never an
    exception, and never silently rendered as an implicit ``$0``.

    Reuses :func:`price_usage` rather than duplicating its cache-aware /
    no-cache selection logic: the tier grid is priced exactly like the base
    grid, then its own ``headline_usd`` (cache-aware when the tier publishes
    a cache-read rate, no-cache otherwise) is the bound. Callers must never
    fold this into :attr:`ScenarioCost.headline_usd` or a ghost-cost total —
    it is additive information about a different, hypothetical scenario.
    """
    if tier_grid is None:
        return None
    return price_usage(usage, tier_grid).headline_usd
