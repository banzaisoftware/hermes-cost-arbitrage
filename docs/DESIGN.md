# hermes-cost-arbitrage — design (v0.1)

> Dashboard plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
> Prices real token usage against any provider, so you can decide between a flat
> subscription and pay-as-you-go APIs.
>
> Status: **v0.1 shipped**; much of v0.2 has shipped since — see
> [ROADMAP.md](../ROADMAP.md) for what exists today. This document is kept as
> the design rationale for the core engine and the v0.1 scope decisions.
> Target Hermes version: **v0.17.0 (2026.6.19)**.

---

## 1. Why this exists

Hermes already records every model call it makes. The dashboard already renders
those volumes. What it cannot do is tell you what they would *cost somewhere
else*.

When Hermes runs on a flat subscription (ChatGPT Plus via `openai-codex`,
Claude Max via `anthropic`), every session is written to `state.db` with:

```
billing_mode      = subscription_included
cost_status       = included
estimated_cost_usd = 0
```

Both dashboard analytics endpoints aggregate that column directly:

```sql
COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost
```

So the Models page shows **`EST. COST $0`** and the Analytics page shows no cost
at all. That is *correct accounting* — a subscription call has no marginal cost —
and *useless for arbitration*. The question "should I move to pay-as-you-go, and
onto which model?" needs the tokens **revalued** at API rates. That is the entire
purpose of this plugin.

### The baseline that motivated it

On the real 30-day agent workload that motivated this plugin, cache reads were
by far the largest share of total token volume — well over half. Naively
revalued at published API rates, frontier models came out an order of magnitude
above the subscription price, while several open models landed in the same
order of magnitude as the subscription. That last band is where the arbitration
becomes real — and where it turns into a **quality** question rather than a
price one.

### Non-obvious finding: the host's own totals disagree

The dashboard's Models page **excludes cache read** from its token total;
`hermes insights` **includes** it. Both are right, but with cache reads
dominating volume, two screens of the same tool differ by a large factor.
That ambiguity is precisely what converting to money removes: there is only
one way to sum currency.

---

## 2. Scope

### In scope (v0.1)

1. **Ghost cost** — what the real, already-consumed usage would have cost at API
   rates, per model actually used, over a 7/30/90-day window.
2. **What-if comparison** — a short, user-pinned list of 5–8 candidate models,
   priced against the same real usage vector.
3. **Break-even** — at the current volume, where a flat subscription crosses a
   given pay-as-you-go model.
4. **Honest cache modelling** — see §5, this is the actual engineering work.

### Out of scope (see [ROADMAP.md](../ROADMAP.md))

- Exhaustive ranking across all providers + capability filtering → **v0.2**
- Notifications (digest line, threshold alerts) → **v2**
- Model bench / replay harness → **v0.3+**

### Explicit non-goal

**This plugin does not replace the native Analytics or Models pages.** They own
volumes; this plugin owns money. No `tab.override` is used.

---

## 3. Architecture

Modelled on the bundled `hermes-achievements` plugin, which is the reference
implementation for a dashboard plugin with a backend.

```
dashboard/
  manifest.json      # tab registration + slot declaration
  plugin_api.py      # FastAPI APIRouter, mounted at
                     #   /api/plugins/hermes-cost-arbitrage/
  dist/index.js      # hand-written IIFE (no bundler — see §7)
  dist/style.css
docs/DESIGN.md       # this file
ROADMAP.md
README.md
LICENSE              # MIT
```

### Data flow

```
$HERMES_HOME/state.db (sessions table) ─┐
                                        ├─► plugin_api.py ─► cost engine ─► JSON
agent/usage_pricing.py (import)        ─┤     (in-process)                  │
$HERMES_HOME/models_dev_cache.json     ─┘                                   ▼
                                                       /cost tab + analytics:bottom slot
```

The backend runs **inside the Hermes dashboard process**, so it reads
`state.db` (which can run to hundreds of MB on a busy deployment — always
opened read-only) and imports `agent.usage_pricing` directly. Nothing is
shipped off the host; no dependency on any other codebase.

### Host location

`HERMES_HOME` resolution follows the achievements pattern exactly — import
`get_hermes_home()` from `hermes_constants`, fall back to
`$HERMES_HOME` / `~/.hermes` when unavailable. Containerised deployments
routinely point `HERMES_HOME` at a mounted volume rather than `~/.hermes`;
hardcoding the latter is a bug.

---

## 4. Plugin registration

`manifest.json`, following the schema in `web/src/plugins/types.ts`:

```json
{
  "name": "hermes-cost-arbitrage",
  "label": "Cost",
  "description": "Price your real token usage against any provider.",
  "icon": "DollarSign",
  "version": "0.1.0",
  "tab": { "path": "/cost", "position": "after:analytics" },
  "slots": ["analytics:bottom"],
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

Two registered surfaces:

- **`/cost` tab** — the full view (§6), placed right after the native Analytics
  tab.
- **`analytics:bottom` slot** — a compact summary card injected *below* the
  native daily-token chart, without overriding that page. This placement is
  deliberate: the native page states `$0`, and the card immediately underneath
  supplies the number it structurally cannot compute.

---

## 5. The cost engine (the only real engineering)

### 5.1 Reuse, don't reimplement

`agent/usage_pricing.py` is the canonical pricing layer and is imported, not
rewritten. Relevant surface:

```
CanonicalUsage · BillingRoute · PricingEntry · CostResult
resolve_billing_route() · get_pricing_entry() · normalize_usage()
estimate_usage_cost() · has_known_pricing() · _openrouter_pricing_entry()
```

It works in `Decimal`, resolves billing routes, and already handles OpenRouter
pricing natively. This plugin is a **presentation + what-if layer** on top of it.
That is what keeps it small, and therefore maintainable and shareable.

### 5.2 The problem the engine must solve: cache

**Cache reads dominate an agent's token volume.** Providers differ
fundamentally here:

- **Cache-aware providers** (OpenAI, Anthropic) bill cache reads at roughly a
  tenth of full input rate.
- **Many open models / OpenRouter routes have no prompt cache at all.** For
  them, the entire cache-read volume would be billed at the **full input
  rate** — roughly **10× the naive estimate**.

A ranking is only as trustworthy as its worst estimate. Applying "cache_read at
cache_read rate" to a provider that has no cache would rank that model ~10× too
cheap, and could drive a four-figure monthly decision off a wrong number.

### 5.3 Two scenarios, always both

For every candidate model the engine emits **two** figures:

| Scenario | Formula |
|---|---|
| `cache_aware` | `input×r_in + output×r_out + cache_read×r_cache_read + cache_write×r_cache_write` |
| `no_cache` | `(input + cache_read + cache_write)×r_in + output×r_out` |

When the model's pricing entry declares a `cache_read` rate, `cache_aware` is
the headline figure and `no_cache` is shown as the pessimistic bound. When it
does **not**, `no_cache` becomes the headline and the row is flagged
`cache: unknown`. **A single, potentially misleading number is never shown
alone.**

### 5.4 Honesty banner (non-negotiable)

Hermes gates its own token analytics behind `dashboard.show_token_analytics`
(default `false`) for a documented reason, quoted from `AnalyticsPage.tsx`:

> the local token counts exclude auxiliary calls and provider retries, so they
> diverge from provider billing in ways that mislead users

Consequence: **every figure this plugin produces is a floor, not a bill.** The
error runs *against* the pay-as-you-go option, so the comparison stays
conservative in favour of the subscription — but the UI must say so, plainly, on
every page that shows money. Auxiliary calls are material here: a production
config can easily declare a dozen auxiliary tasks.

### 5.5 Purity and testability

The engine is a pure function: `(usage vector, pricing grid) → cost result`. No
I/O, no dashboard, no database. It is unit-testable in isolation, which is what
makes the v0.2 expansion cheap (§8).

---

## 6. The `/cost` tab

Four blocks, top to bottom:

1. **Honesty banner** — §5.4, always visible.
2. **Ghost cost** — real consumption revalued per actually-used model, versus
   the subscription's flat cost. Window selector 7d / 30d / 90d, matching the
   native pages' control.
3. **What-if table** — the pinned candidate list, sorted by monthly cost,
   columns: model · provider · cache-aware $ · no-cache $ · context · vision ·
   tools. Capability metadata comes from `models_dev_cache.json`, which the
   native `/api/analytics/models` endpoint already surfaces. *(Since v0.2 this
   table has grown into the full catalogue — same row contract, wider input
   set. See [docs/USAGE.md](USAGE.md) for the tab as it exists today.)*
4. **Break-even** — at the current volume, the crossover point between the flat
   subscription and each candidate.

### Pinned list

Default seed: `gpt-5.6-terra`, `gpt-5.5`, `claude-sonnet-5`, `claude-haiku-4-5`,
`z-ai/glm-5`, `moonshotai/kimi-k2.5`, `minimax/minimax-m2.7`. Editable in the
tab, persisted via `PUT /api/plugins/hermes-cost-arbitrage/config` to a small
JSON file under `HERMES_HOME` (the achievements plugin's state-file pattern).

### The `analytics:bottom` card

One line, no controls: *"This month: X tokens ≈ Y $ at API rates vs Z $
subscription"*, linking to `/cost`.

---

## 7. Frontend approach: no build step

The reference plugin's `dist/index.js` is a **hand-written IIFE**:

```js
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const hooks = SDK.hooks;
  const C = SDK.components;
  const cn = SDK.utils.cn;
  ...
})();
```

No bundler, no `npm build`, no vendored React. This plugin does the same.

### Why this is also the styling answer

The host exposes `window.__HERMES_PLUGIN_SDK__`, a **versioned contract**
(`web/src/plugins/sdk.d.ts`) providing:

- `React` + hooks — **do not bundle React**
- `components` — the dashboard's own Nous DS / shadcn primitives
- `fetchJSON` / `authedFetch` / `buildWsUrl` — auth handled for both loopback and
  gated OAuth modes, including the 401 redirect
- `utils.cn`, `timeAgo`, `isoTimeAgo`, and `useI18n()`

The tab therefore renders in the dashboard's native look (terminal green,
monospace, bordered cards) **without a single line of theming**. The SDK is
explicit that plugins MUST use `fetchJSON`/`authedFetch` rather than hand-reading
`window.__HERMES_SESSION_TOKEN__` — this plugin complies.

The manifest also supports an `integrity` field (SRI hash); it will be populated
once a published release exists.

---

## 8. Why v0.1 is a short list, not the full catalogue

The obvious objection: enumerating every provider is easy — the metadata is
already local — so why not ship the exhaustive ranking immediately?

Because enumeration is not the hard part; **§5.2 cache modelling is**. And the
difficulty scales with the list: on a short list, every candidate can be
sanity-checked by hand against known behaviour. On thousands of rows, it
cannot — the engine has to be right *unsupervised*.

Two further irritants that only bite at catalogue scale:

- **Uneven capability metadata** across providers → false negatives in a
  capability filter.
- **The same model offered by several providers** at different prices → needs
  dedup plus "cheapest route for this model".

v0.1 and v0.2 share **the same engine**; only the input set differs (pinned list
vs. every capable model). So v0.1 is the engine's *calibration phase*: build the
cache model properly once, verify it against models whose behaviour is known,
then open the same machine onto the full catalogue. Shipping the full ranking
first would mean trusting an uncalibrated engine on thousands of rows before it
has been shown correct on three.

---

## 9. Error handling

Fail-open throughout, matching Hermes plugin convention (the built-in Langfuse
plugin degrades to a silent no-op on missing SDK, missing credentials, or a
transient error, and never impacts the agent loop):

| Condition | Behaviour |
|---|---|
| `state.db` missing / unreadable | Empty state with explanation; no crash |
| No pricing entry for a model | Row rendered, flagged `pricing unknown`, excluded from ranking |
| `models_dev_cache.json` stale/absent | Fall back to `usage_pricing` lookups; warn in UI |
| Backend endpoint fails | Tab shows an error card; the rest of the dashboard is unaffected |

A plugin must never be able to take down the dashboard or the agent loop.

The one deliberate exception, added with the switch-model endpoint: the
pre-switch backup of `config.yaml` is **fail-closed** — if the backup cannot
be written, the switch is refused. Reversibility outranks availability for
the single write this plugin can perform.

---

## 10. Testing

`pytest` over the pure engine (matching the reference plugin, which ships
`tests/test_achievement_engine.py`). The engine's purity (§5.5) means no
dashboard or database is required. The suite has since grown to cover the
store, pricing resolution, config handling, path resolution, the entitlement
probe, and the API builders — plus a Node smoke test that evaluates the
hand-written bundle and renders every component (`tests/bundle_smoke.mjs`).

Original cases:

- Known usage vector → expected cost, both scenarios
- Cache-aware vs no-cache divergence on the same vector
- Model with no pricing entry → flagged, not crashed
- Empty window → zeroes, no division by zero
- `HERMES_HOME` override respected

---

## 11. Deployment

Install path, documented publicly with the canonical URL:

```bash
hermes plugins install banzaisoftware/hermes-cost-arbitrage
```

Dashboard plugins are discovered by scanning for `dashboard/manifest.json`, so
`hermes plugins enable` does not apply to them — it resolves plugins through
`plugin.yaml`, which a dashboard-only plugin does not carry, and exits non-zero.
The dashboard must be restarted after installing, because the plugin's API
routes are mounted at process start. See the README for the exact sequence.

---

## 12. Prerequisites discovered during design

- `dashboard.show_token_analytics` defaults to `false`, which hides the native
  Analytics tab entirely. This plugin does not require it, but the native tab
  is the reference view alongside which the plugin is read — you probably want
  it on.
- **Do not save `config.yaml` from the dashboard UI.** `GET /api/config` strips
  internal keys (e.g. `_config_version`) before sending to the frontend, and
  `save_config()` performs a full rewrite rather than a merge — a UI save would
  drop internal keys and comments.
- `openrouter-provider` and `ollama-cloud-provider` ship as **bundled plugins**,
  so testing alternative models needs `plugins enable` + an API key, not new
  code. OpenRouter is prepaid credits, no subscription — a small top-up is
  enough to start.

---

## 13. Licence and ownership

MIT, `Copyright (c) 2026 Banzai Software`. Consistent with the ecosystem:
`hermes-agent` is MIT (Nous Research), and the `hermes-achievements` reference
plugin is MIT. Nothing proprietary transits through this plugin — it reads only
`state.db` and `usage_pricing`, both of which belong to Hermes.
