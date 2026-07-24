# Roadmap

Design rationale for the current scope lives in [docs/DESIGN.md](docs/DESIGN.md).

---

## v0.1 — arbitration on a pinned list *(designed, not yet implemented)*

The question: **"my real tokens — what would they cost pay-as-you-go, and at
what volume does the subscription stop winning?"**

- [ ] Cost engine: revalue real usage at API rates, importing
      `agent.usage_pricing` rather than reimplementing it
- [ ] Explicit per-provider cache modelling — `cache_aware` **and** `no_cache`
      figures, never a single misleading number
- [ ] `/cost` dashboard tab: honesty banner · ghost cost · what-if table ·
      break-even
- [ ] Summary card injected into the `analytics:bottom` slot
- [ ] Pinned candidate list (5–8 models), editable and persisted
- [ ] `pytest` suite over the pure engine
- [ ] README with screenshots, MIT licence, public install instructions

**Deliberately not in v0.1:** the exhaustive catalogue ranking. v0.1 is the
engine's calibration phase — see [DESIGN.md §8](docs/DESIGN.md#8-why-v01-is-a-short-list-not-the-full-catalogue).

---

## v0.2 — exhaustive ranking

Same engine, wider input set. Cheap *because* v0.1 hardened the engine first.

- [ ] Rank every model in `models_dev_cache.json` (~170 providers)
- [ ] Capability filter inferred from real usage — vision, tool calling, context
      window ≥ observed median
- [ ] Deduplicate the same model across providers; surface the cheapest route
- [ ] Collapsible "show all models" beyond the filtered default
- [ ] Flag rows whose capability metadata is incomplete rather than silently
      dropping them

Known hazards, already identified: uneven capability metadata across providers
(false negatives in the filter), and the same model priced differently by
several providers.

---

## v2 — notifications *(ideas captured, not designed)*

Deferred deliberately: it couples the plugin to per-deployment cron and
messaging infrastructure. Worth doing once the dashboard's figures have proven
themselves trustworthy.

- **Digest line** — append a monthly summary to an existing recurring digest
  ("this month: X tokens ≈ Y $ at API rates vs Z $ subscription"), so drift is
  visible without opening the dashboard.
- **Threshold alert** — notify only when projected ghost cost crosses a
  user-set ceiling ("tell me if this would exceed 400 $/month on API"). More
  useful than a periodic digest once the decision is genuinely open.
- **Delta alert** — notify when the cheapest viable candidate changes, or when a
  pinned model's published pricing moves.
- Open questions to settle at design time: which transport (Hermes supports
  Telegram, Discord, Slack, ntfy, email, and more via platform plugins), whether
  the plugin owns its own schedule or piggybacks on an existing job, and how to
  avoid alert storms when the underlying figures are noisy day to day.

---

## v0.3+ — model bench

Cost is only half of an arbitration; the other half is whether a cheaper model
does the work acceptably.

- Replay a reference workload against candidate models and score the results
- Report quality alongside cost, so the two are decided together rather than
  in sequence
- Leans on already-bundled provider plugins (`openrouter-provider`,
  `ollama-cloud-provider`) — pay-as-you-go credits, no subscription required to
  evaluate a model

---

## Ideas, unscheduled

- Publish an `integrity` (SRI) hash in the manifest once releases are tagged
- Export a window's figures as CSV/JSON for offline analysis
- Reconcile local counts against a provider's real invoice, to measure how far
  the documented floor sits below actual billing
