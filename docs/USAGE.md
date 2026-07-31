# Using the Cost tab

This page explains what the `/cost` tab and the `analytics:bottom` summary
card show, where each number comes from, and how much to trust it. Read it
once before you act on any figure in the tab.

## The Ghost cost card

The `/cost` tab's own Ghost cost card, for the 7d/30d/90d window you have
selected, shows:

- **Ghost cost** — the sum of `headline_usd` across every model you actually
  used in the window. `headline_usd` is the cache-aware price when the
  provider publishes a cache-read rate, or the no-cache price when it does
  not (see "The two cache scenarios" below). This is the number labelled
  large on the card.
- **Projected monthly** — `ghost cost × 30 / days`. It is a straight-line
  extrapolation of the window's rate to a 30-day month, nothing more. At the
  30-day window the two figures are necessarily equal, because `30 / 30 = 1`;
  at 7d or 90d they diverge, and the projection is what you should compare
  against a monthly subscription.

The compact card injected under the native Analytics page (`analytics:bottom`)
is a fixed 30-day view of the same idea: it shows only the ghost cost figure
against the subscription price, with no separate projection line, because at a
30-day window the projection would just repeat the ghost cost.

Both are compared against `subscription_usd_per_month` from the plugin's
config — the flat monthly price you're weighing the paid APIs against. The
card also says in words which side currently wins at this volume.

## The per-model table

Each row is one `(model, billing_provider)` pair that appears in the
`sessions` table within the selected window. Columns, in order:

| Column | What it is | Unit / source |
|---|---|---|
| Model | The model name as recorded by Hermes, plus badges (below) | — |
| Sessions | Count of session rows aggregated into this line | integer, `COUNT(*)` grouped by `(model, provider)` |
| Input | Input tokens | summed `input_tokens` over the window |
| Cache read | Cache-read tokens | summed `cache_read_tokens` over the window |
| Output | Output tokens | summed `output_tokens` over the window |
| Cost | `headline_usd` for this model, in this window | USD |

The token counts are summed directly from the `sessions` table for
`started_at` timestamps inside the window — no estimation, no sampling. The
three columns shown are a subset: the table also tracks `cache_write_tokens`
per row, which is not shown as a column but **is** included in the pricing
(both the cache-aware and no-cache scenarios bill it — see below). Don't read
the three visible token columns as the whole of what was billed; they're the
volumes that fit comfortably in a table, not the whole usage vector.

## The Catalogue

Below the per-model table sits the catalogue: one row per `(model, provider)`
pair in the local models.dev pricing cache — thousands of models across well
over a hundred providers — each row repricing your **entire measured usage**
as if that one candidate had served all of it. Filtering, sorting, search and
pagination all run server-side; the tab only ever holds one page.

Sortable columns: Provider · Model · Monthly · Cache-aware · No cache ·
Break-even, followed by fixed Long-context bound, Capabilities, Context and
Action columns.

| Column | What it is |
|---|---|
| Provider / Model | The candidate, with capability badges and a `pinned` badge for models pinned in the config |
| Monthly | What your **entire measured usage**, summed across every model, would cost on this one candidate, projected to a 30-day month |
| Cache-aware | The cache-aware price of that same combined usage over the *selected window* (7d/30d/90d) — not projected to 30 days |
| No cache | The no-cache price of that same combined usage over the *selected window* — likewise not projected |
| Break-even | `subscription_usd_per_month / Monthly`, as a percentage |
| Long-context bound | An upper bound for tiered pricing — see "Accuracy" below |
| Capabilities / Context | Capability metadata (tools, vision, reasoning) and context window, from models.dev |
| Action | The switch-model control — see "Switching models" below |

Rows priced cheaper than the subscription are highlighted.

### Filters

The default view is deliberately narrow — every default is a toggle, never a
hard constraint:

- **Tool calling required** (on by default) — agents need it.
- **Hide free routes** (on by default) — zero-priced routes are usually
  rate-limited previews and would meaninglessly dominate an ascending sort.
- **Credentialed providers only** (on by default) — only providers for which
  a credential is present locally. Presence, not validity: a credential is
  never verified as working by this filter.
- **Vision / Reasoning / Open weights / Minimum context** — off by default.
- **Provider facet** — an include/exclude checkbox panel over every provider
  in the cache, with per-provider row counts.
- **Search** — free-text match on model and provider names.

The card's freshness line shows when the local models.dev cache was last
fetched, and the refresh button next to it re-downloads the cache. That
explicit click is the **only** network request the plugin ever makes outside
of a switch probe — every `GET` endpoint is strictly local.

### Two things that are easy to misread

**This is not a per-model breakdown.** Every candidate row reprices the same
combined total — the sum of input, output, cache-read, and cache-write tokens
across *every* model you used in the window — as if that one candidate had
served all of it. A row for `claude-haiku-4-5` does not mean "here's what your
Haiku sessions cost"; it means "here's what your *entire* month of usage would
cost if it had all gone through Haiku instead."

**Break-even reads the opposite way you might expect.** It is
`subscription_usd_per_month / monthly_usd`, expressed as a percentage: what
share of your current monthly volume the flat subscription would buy you at
this candidate's rates. A worked example with round numbers: a $23/month
subscription, and a candidate whose repriced Monthly figure is $46. Break-even
is 23 / 46 = 50%. Read literally: at this candidate's rates, the subscription
only covers half of what you're actually using — so if you switched to this
candidate and kept the same usage, you'd be paying roughly double what the
subscription costs. A candidate is only *cheaper* than the subscription when
its Monthly figure is below the subscription price, which is exactly the case
where Break-even is **above 100%** — the subscription would need more than
your current volume to justify itself at that candidate's rates. Below 100%,
the subscription wins at this volume: you're using less than the subscription
"buys" if repriced at that candidate.

## Switching models

Each catalogue row carries a Switch control — the only place this plugin is
not read-only against Hermes. The flow, in order:

1. **A confirmation panel** states exactly what will change, and how the
   candidate's repriced monthly cost compares to the subscription. Nothing is
   sent until you confirm; exactly one row's panel can be open at a time.
2. **An entitlement probe** makes one real, minimal test call (a
   `max_tokens=1` completion) to confirm the target model is actually
   *callable* with your credentials — a provider's model listing alone is not
   trustworthy; some list models that then 404 on a real call. A
   `not_entitled` or `credential_rejected` result refuses the switch and
   leaves the config untouched. Every other outcome — including a timeout or
   a throttle — is fail-open, and the probe result is surfaced in the
   response so you can see what happened.
3. **A backup of `config.yaml`** is written before anything changes (rotated,
   the most recent few are kept). Uniquely in this plugin, this step is
   fail-closed: if the backup cannot be written, the switch is refused.
4. **The write goes through the host's own validated switch path** — the same
   resolution and validation the dashboard's built-in picker uses — never a
   raw YAML edit. Hermes' expensive-model guard still applies: a model whose
   published rates exceed the host's thresholds requires an explicit second
   confirmation.
5. **The response reports the previous model**, so the change can be reversed
   from the tab, and repeats any host warning verbatim — the host will accept
   a model it could not confirm exists, and hiding that doubt would be worse
   than showing it. A persistent banner records the switch until you dismiss
   it. API keys are never echoed anywhere in the flow.

One caveat inherited from Hermes itself: any Hermes config write rewrites
`config.yaml` from the parsed data, so hand-written comments in that file do
not survive — this endpoint included.

## The two cache scenarios

Every priced row carries two numbers, never shown alone:

- **Cache-aware** prices `input_tokens` and `output_tokens` at their normal
  rates, `cache_read_tokens` at the provider's cache-read rate, and
  `cache_write_tokens` at the provider's cache-write rate (or the full input
  rate, if the provider doesn't publish one).
- **No cache** re-prices `input_tokens + cache_read_tokens + cache_write_tokens`
  — every prompt token, cached or not — at the full input rate, and adds
  output at the normal output rate. This is what the same month would cost on
  a provider with no prompt cache at all.

Both are always computed and both are always available in the API response,
because cache reads are typically well over half of an agent's total token
volume. A provider that has no prompt cache bills that volume at the full
input rate — several times more than a cache-aware provider would. Showing
only one number would silently pick a side; a model whose cache rate is
unknown gets `no cache` promoted to the headline figure instead, with the row
flagged accordingly (see badges below).

One caveat the tab itself points out: your measured cache-read share reflects
your *current* provider's caching behaviour, not an intrinsic property of
your workload. A different provider's cache may hit less often, which would
move the real bill toward the no-cache figure — another reason both numbers
are always shown.

## Badges

- **`priced as <provider>`** — appears when the billing provider on the
  session (e.g. `openai-codex` for a ChatGPT subscription route) differs from
  the provider actually used to price it (e.g. `openai`). Hermes prices
  subscription routes at a flat $0 marginal cost, which is correct accounting
  but useless for arbitration — that's the whole reason this plugin exists.
  Before pricing, the provider is rewritten to its pay-as-you-go equivalent
  (`openai-codex` → `openai`) so the row reflects what the same usage would
  cost on the metered API, not the subscription's $0.
- **`no cache pricing`** — shown only on a row that *is* priced
  (`status === "ok"`) but whose provider has no published cache-read rate
  (`cache_status === "unknown"`). The cost shown is the no-cache figure.
- **`pricing unknown`** — shown when no pricing could be resolved for this
  model/provider at all (`status === "no_pricing"`). The Cost column reads
  `—` for these rows, never a number, and never alongside `no cache pricing`
  — a row is either entirely unpriced or priced-without-a-cache-rate, never
  both.
- **`pinned`** — a catalogue row for a model pinned in the config.
- The provider badge is informational, not a status flag.

## Reading `—`, and the degraded states

A `—` anywhere in any table means the underlying value is `null` —
unknown — never zero. A `$0.00` and a `—` are different claims: the former
says "this cost nothing," the latter says "this could not be computed." Cost
cells, Break-even cells, and the projected-monthly figure can all render `—`
under this rule.

If `state.db` cannot be found or read, the tab shows a "Usage data
unavailable" card instead of the cost card, with a short explanation
underneath (why: missing file, permission error, or another `sqlite3` error).
This is deliberate — the plugin exists specifically to replace a silently
misleading `$0`, so it will not itself show `$0` in place of "I couldn't read
the data."

If the local models.dev pricing cache could not be loaded, the tab shows a
notice that some candidates may be unpriced, because their rates fall back to
Hermes' own offline pricing table (openai, anthropic, minimax, minimax-cn)
and nothing else — every other provider depends on that cache.

## Accuracy: this is a floor, not a bill

The Ghost cost card (both the `/cost` tab's and the `analytics:bottom` summary
card's) carries a fixed notice: local token counts exclude auxiliary calls and
provider retries, so every figure here sits at or below what a real provider
invoice would show. The error runs against the pay-as-you-go side of the
comparison, which keeps the bias in favour of the subscription — if anything,
pay-as-you-go looks slightly cheaper here than it would in reality. The same
floor applies to the catalogue's figures even though that card does not
repeat the notice text — the underlying usage counts are the same ones read
from `sessions`.

Two further gaps are worth stating plainly, because they're easy to overlook
and easy to "fix" wrongly later:

**Tiered pricing above a context threshold gets its own column — an upper
bound, not a bill.** Some models publish a second, higher rate for calls
above a context-size threshold (models.dev carries this as a `tiers` entry,
sometimes alongside a same-shaped `context_over_200k` block).
`resolve_tier_grid` parses that second rate block into its own
`PricingGrid`, preferring the `tiers` list — because it carries the real
threshold — over the fixed-name `context_over_200k` key: where the two
disagree on the threshold, `tiers` wins. A `tiers` entry whose `tier.type`
isn't `"context"` is ignored. When a model publishes neither,
`long_context_usd` and `tier_threshold_tokens` are both `None`, rendered in
the tab as the words "not applicable" — never `$0`, and never the bare `—`
this page uses elsewhere for "a priced value could not be computed", because
there is simply no tier to bound here.

`long_context_usd` (shown as the "Long-context bound" column, next to the
Cache-aware / No cache pair) is **what your measured usage would cost if
every single call in it had landed above the threshold** — not a prediction,
not a split of which calls actually did, because `sessions` stores only
aggregate token counts per window and never records a per-call context size.
There is no way to know the real mix from this data, so this tab doesn't
pretend to: it shows the ceiling and says so. It is strictly additive — it
never changes `headline_usd`, `ghost_cost_usd`, `monthly_usd`,
`cache_aware_usd`, or `no_cache_usd` on any row, and it is never summed into
any total.

To keep that ceiling honest rather than alarming, the tab also surfaces your
workload's own observed average prompt context per call
(`avg_context_per_call`, derived from `api_call_count` and the same
input/cache-read/cache-write tokens everything else here is priced from).
When that average sits below a candidate's own threshold, the row says so
plainly ("tier rarely applies at your measured avg") instead of leaving a
large dollar figure to imply the opposite. Across the models.dev cache, only
a small minority of models (on the order of 7%) publish any tier at all —
the rest show "not applicable" here, unaffected either way.

**`reasoning_tokens` is not billed separately, on purpose.** The `sessions`
table records a `reasoning_tokens` column, but this plugin's aggregation query
never selects it, and no cost calculation anywhere adds it in. That's
correct, not an oversight: reasoning tokens are a breakdown of
`output_tokens`, not additional tokens on top of it, and Hermes' own
`estimate_usage_cost` never bills them separately either. If you're ever
tempted to add a `reasoning_tokens` line to the cost calculation, don't —
that would double-count tokens already priced inside `output_tokens`.

## Configuration

Settings live in `$HERMES_HOME/cost_arbitrage_config.json` (`$HERMES_HOME`
resolves the same way throughout this plugin: `hermes_constants.get_hermes_home()`
when the host is importable, else the `HERMES_HOME` environment variable, else
`~/.hermes`). Its shape:

```json
{
  "subscription_usd_per_month": 23.0,
  "pinned": [
    { "provider": "openai", "model": "gpt-5.5" }
  ]
}
```

`subscription_usd_per_month` is the flat price compared against every ghost
and catalogue figure. `pinned` is the list of models badged `pinned` in the
catalogue; each entry needs both `provider` and `model`. As of v0.2 this file
is edited by hand — the backend exposes
`GET`/`PUT /api/plugins/hermes-cost-arbitrage/config`, but no control in the
tab reaches the `PUT` endpoint yet, so there is no in-tab editor.

A config edit takes effect on the next request — the file is read fresh each
time, nothing is cached across requests. Restarting the dashboard is only
required after installing a new version of the plugin itself, because the
dashboard mounts a plugin's API routes once, at process start; a plain config
change never needs a restart.
