# hermes-cost-arbitrage

Dashboard plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Prices your real token usage against any provider, so you can decide between a
flat subscription and pay-as-you-go APIs.

## Why

When Hermes runs on a flat subscription, every session is recorded with
`billing_mode=subscription_included` and `estimated_cost_usd=0`. Both dashboard
analytics endpoints sum that column, so the Models page shows **`EST. COST $0`**.

That is correct accounting — a subscription call has no marginal cost — and it
is useless for deciding whether to switch. This plugin revalues the same tokens
at published API rates and answers the question the native pages structurally
cannot.

## What it shows

- **Ghost cost** — what your actual consumption would have cost on the paid API.
- **What-if** — a pinned list of candidate models priced against the same usage.
- **Break-even** — how much of your current volume the subscription buys.

Every model is priced under **two cache scenarios**. Cache reads are typically
the large majority of an agent's token volume, and a provider without a prompt
cache bills them at the full input rate — several times more. A single figure
would be misleading, so both are always shown.

## Install

```bash
hermes plugins install banzaisoftware/hermes-cost-arbitrage
hermes plugins enable hermes-cost-arbitrage
```

Then open the **Cost** tab in the dashboard.

## Accuracy

Figures are a **floor, not a bill**. Local token counts exclude auxiliary calls
and provider retries, so real provider billing sits above these numbers. The
error runs against the pay-as-you-go option, which keeps the comparison
conservative in favour of the subscription.

## Configuration

Settings live in `$HERMES_HOME/cost_arbitrage_config.json`:

```json
{
  "subscription_usd_per_month": 23.0,
  "pinned": [{ "provider": "openai", "model": "gpt-5.5" }]
}
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -v
```

The cost engine is a pure function, so the whole suite runs without Hermes
installed.

See [docs/DESIGN.md](docs/DESIGN.md) for the design and [ROADMAP.md](ROADMAP.md)
for what is planned next.

## Licence

MIT © 2026 Banzai Software
