from decimal import Decimal

from hermes_cost_arbitrage_dashboard.cost_engine import (
    PricingGrid,
    UsageVector,
    price_long_context,
    price_usage,
)


def test_cache_aware_and_no_cache_diverge_on_the_same_vector():
    usage = UsageVector(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    grid = PricingGrid(
        input_per_million=Decimal("2"),
        output_per_million=Decimal("10"),
        cache_read_per_million=Decimal("0.2"),
        source="test",
    )

    result = price_usage(usage, grid)

    # cache-aware: 1M@2 + 1M@10 + 1M@0.2
    assert result.cache_aware_usd == Decimal("12.2")
    # no-cache: the cache-read million is re-billed at the full input rate
    assert result.no_cache_usd == Decimal("14")
    assert result.headline_usd == Decimal("12.2")
    assert result.cache_status == "priced"
    assert result.status == "ok"


def test_grid_without_cache_pricing_falls_back_to_no_cache_headline():
    usage = UsageVector(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    grid = PricingGrid(
        input_per_million=Decimal("2"),
        output_per_million=Decimal("10"),
        source="test",
    )

    result = price_usage(usage, grid)

    assert result.cache_aware_usd is None
    assert result.no_cache_usd == Decimal("14")
    assert result.headline_usd == Decimal("14")
    assert result.cache_status == "unknown"
    assert result.status == "ok"


def test_unpriced_grid_reports_no_pricing_instead_of_zero():
    result = price_usage(UsageVector(input_tokens=1_000), PricingGrid(source="test"))

    assert result.status == "no_pricing"
    assert result.headline_usd is None
    assert result.cache_aware_usd is None
    assert result.no_cache_usd is None


def test_empty_usage_costs_nothing():
    grid = PricingGrid(
        input_per_million=Decimal("2"),
        output_per_million=Decimal("10"),
        cache_read_per_million=Decimal("0.2"),
        source="test",
    )

    result = price_usage(UsageVector(), grid)

    assert result.cache_aware_usd == Decimal("0")
    assert result.no_cache_usd == Decimal("0")
    assert result.status == "ok"


def test_matches_the_measured_production_baseline():
    """30 days of real usage on gpt-5.5 rates — the number that motivated the plugin."""
    usage = UsageVector(
        input_tokens=59_614_755,
        output_tokens=2_135_048,
        cache_read_tokens=377_920_000,
        cache_write_tokens=24_368,
    )
    grid = PricingGrid(
        input_per_million=Decimal("5"),
        output_per_million=Decimal("30"),
        cache_read_per_million=Decimal("0.5"),
        source="models.dev",
    )

    result = price_usage(usage, grid)

    assert round(result.cache_aware_usd, 2) == Decimal("551.21")
    # Without a prompt cache the same month costs ~4x more.
    assert round(result.no_cache_usd, 2) == Decimal("2251.85")


# --- price_long_context: the tier upper bound -------------------------------


def test_price_long_context_prices_the_whole_usage_at_the_tier_rate():
    """Same real usage as the production baseline, priced at gpt-5.5's actual
    published tier rate (input $10, output $45, cache_read $1 per million; no
    cache_write rate published, so it falls back to the tier's input rate,
    same fallback rule as the base grid). This is what every one of those
    calls would have cost had every single one landed above the threshold —
    an upper bound, not a split of which calls actually did."""
    usage = UsageVector(
        input_tokens=59_614_755,
        output_tokens=2_135_048,
        cache_read_tokens=377_920_000,
        cache_write_tokens=24_368,
    )
    tier_grid = PricingGrid(
        input_per_million=Decimal("10"),
        output_per_million=Decimal("45"),
        cache_read_per_million=Decimal("1"),
        source="models.dev-tier",
    )

    result = price_long_context(usage, tier_grid)

    assert round(result, 2) == Decimal("1070.39")


def test_price_long_context_is_none_when_there_is_no_tier_grid():
    usage = UsageVector(input_tokens=1_000, output_tokens=500)

    assert price_long_context(usage, None) is None


def test_price_long_context_is_none_when_the_tier_grid_is_unpriced():
    usage = UsageVector(input_tokens=1_000, output_tokens=500)
    unpriced_tier = PricingGrid(source="models.dev-tier")

    assert price_long_context(usage, unpriced_tier) is None
