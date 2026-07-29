(function () {
  "use strict";

  // Hand-written IIFE, matching the bundled hermes-achievements plugin: no
  // bundler, no vendored React. Everything comes from the host SDK.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const REGISTRY = window.__HERMES_PLUGINS__;
  if (!SDK || !REGISTRY) return;

  const PLUGIN = "hermes-cost-arbitrage";
  const BASE = "/api/plugins/" + PLUGIN;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useMemo, useRef } = SDK.hooks;
  const C = SDK.components;

  // Newer host dashboards expose a DS-styled Checkbox on the plugin SDK.
  // Fall back to a native <input type="checkbox"> shim so older hosts that
  // predate the design-system rollout still render. The shim normalises
  // Radix's onCheckedChange(checked) signature to native onChange(event) —
  // same pattern the hermes-kanban plugin uses for the same reason.
  const Checkbox =
    C.Checkbox ||
    function (props) {
      const { checked, onCheckedChange, className, ...rest } = props;
      return h(
        "input",
        Object.assign(
          {
            type: "checkbox",
            checked: !!checked,
            className: className,
            onChange: function (e) {
              if (onCheckedChange) onCheckedChange(e.target.checked);
            },
          },
          rest
        )
      );
    };

  const CATALOGUE_LIMITS = [10, 25, 50, 100];
  const SEARCH_DEBOUNCE_MS = 350;

  const money = (value) =>
    value === null || value === undefined
      ? "—"
      : "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  const tokens = (value) => {
    if (!value) return "0";
    if (value >= 1e9) return (value / 1e9).toFixed(1) + "B";
    if (value >= 1e6) return (value / 1e6).toFixed(1) + "M";
    if (value >= 1e3) return (value / 1e3).toFixed(1) + "K";
    return String(value);
  };

  // Three-angle UI audit, finding 1: a row whose provider does not resolve
  // (e.g. a NULL billing_provider coalescing to "") contributes nothing to
  // ghost_cost_usd while its tokens still count in `totals` -- reachable in
  // production, not a hypothetical. `unpriced.affects_total` is true exactly
  // when that happened; the caller must place this next to the headline it
  // qualifies, never leave it to the table alone.
  function unpricedCaveat(unpriced) {
    if (!unpriced || !unpriced.affects_total) return null;
    const modelWord = unpriced.models === 1 ? "model" : "models";
    return (
      "Excludes " +
      unpriced.models +
      " unpriced " +
      modelWord +
      " (" +
      tokens(unpriced.tokens) +
      " tokens) that could not be priced -- the real cost is higher than shown."
    );
  }

  // Three-angle UI audit, finding 2: every /summary, /whatif and /catalogue
  // row already carries `pricing_source`, previously rendered nowhere.
  // Hermes' own offline snapshot (consulted first for openai, anthropic,
  // minimax, minimax-cn) can carry dated rates -- e.g. claude-haiku-4-5 is
  // priced from a several-months-old snapshot even while the models.dev
  // cache elsewhere on this page refreshes hourly. The freshness line only
  // measures that models.dev cache, so this per-row label is what lets a
  // reader tell which rows it does not describe.
  const PRICING_SOURCE_LABELS = {
    "models.dev": "models.dev",
    official_docs_snapshot: "official docs snapshot",
    hermes: "hermes",
    unknown: "unknown",
  };

  function pricingSourceLabel(source) {
    return PRICING_SOURCE_LABELS[source] || source || "unknown";
  }

  // The switch-model control (v0.2 T11). POST /switch-model is the plugin's
  // only write against the host — everything else on this page is read-only.
  // The response always carries the same nine keys (ok, confirm_required,
  // detail, warning, confirm_message, guard_ran, previous, current, target);
  // every caller below reads all nine somewhere.
  function switchModelRequest(body) {
    return SDK.fetchJSON(BASE + "/switch-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  // The confirmation message shown before anything is sent (v0.2 T11). Always
  // states the projected monthly cost against the subscription — the thing
  // Hermes' own expensive-model guard structurally cannot see, since its
  // thresholds are per-million rates, not projected spend. Cache-aware and
  // no-cache are added only when they differ: a candidate whose cache
  // profile doesn't transfer costs the higher (no-cache) figure, and the
  // catalogue table's own notice already explains why — this is where that
  // caveat becomes concrete money.
  function buildSwitchConfirmMessage(row, subscriptionUsdPerMonth) {
    var text =
      "Switch to " +
      row.provider +
      "/" +
      row.model +
      " — projected " +
      money(row.monthly_usd) +
      "/month against your " +
      money(subscriptionUsdPerMonth) +
      " subscription.";
    var haveBoth =
      row.cache_aware_usd !== null &&
      row.cache_aware_usd !== undefined &&
      row.no_cache_usd !== null &&
      row.no_cache_usd !== undefined;
    if (haveBoth && row.cache_aware_usd !== row.no_cache_usd) {
      text += " Cache-aware " + money(row.cache_aware_usd) + ", no cache " + money(row.no_cache_usd) + ".";
    }
    return text;
  }

  // `previous.provider` can legitimately be "" — the handler's raw-config
  // reader (`_current()` in plugin_api.py) returns "" when `model:` was a
  // bare scalar with no provider set. Guard every display of a
  // {model, provider} pair so that case reads as a plain model name rather
  // than a stray leading "/".
  function modelRef(entry) {
    if (!entry) return "unknown";
    return entry.provider ? entry.provider + "/" + entry.model : entry.model || "unknown";
  }

  // Total column count of the catalogue table (CATALOGUE_COLUMNS' six sortable
  // headers plus the four plain trailing headers: Long-context bound,
  // Capabilities, Context, Action). Kept as one constant so the switch
  // confirmation row's colSpan can never drift out of sync with the header.
  var TOTAL_CATALOGUE_COLUMNS = CATALOGUE_COLUMNS.length + 4;

  // The per-row Switch/Cancel toggle button. Disabled whenever another
  // request is in flight (switchUi.busy) or another row's confirmation is
  // open (switchUi.confirmRowKey set to something else) — only one
  // confirmation flow is ever live at a time, and a double-click can't fire
  // two switches because `busy` flips synchronously before the fetch starts.
  function SwitchActionCell({ row, switchUi }) {
    var rowKey = row.provider + "/" + row.model;
    var isOpen = switchUi.confirmRowKey === rowKey;
    var disabled = switchUi.busy || (switchUi.confirmRowKey !== null && !isOpen);
    return h(
      C.Button,
      {
        type: "button",
        variant: isOpen ? "ghost" : "default",
        disabled: disabled,
        onClick: () => switchUi.onToggle(row),
      },
      isOpen ? "Cancel" : "Switch"
    );
  }

  // The inline confirmation panel for a row's Switch action, rendered as an
  // extra <tr> directly under the row being switched. Three phases:
  //   "confirm" — our own pre-send confirmation (cost projection).
  //   "guard"   — the host's own expensive-model guard fired; nothing was
  //               written. `confirmGuard.message` is the host's exact text,
  //               shown verbatim; the user must take a second, explicit
  //               action ("Switch anyway") — this never auto-retries.
  //   "error"   — ok:false; `confirmError` is the host's `detail`, shown
  //               verbatim, since the endpoint writes it to be actionable.
  function SwitchConfirmRow({ row, switchUi }) {
    var phase = switchUi.confirmPhase;
    var body;
    if (phase === "guard") {
      var guard = switchUi.confirmGuard || {};
      body = h(
        "div",
        { className: "hca-switch-confirm" },
        h("p", null, guard.message || "Hermes flagged this model as unusually expensive."),
        guard.target ? h("p", { className: "hca-notice" }, "Target: " + modelRef(guard.target)) : null,
        h(
          "div",
          { className: "hca-switch-actions" },
          h(
            C.Button,
            { type: "button", variant: "default", disabled: switchUi.busy, onClick: switchUi.onGuardConfirm },
            "Switch anyway"
          ),
          h(C.Button, { type: "button", variant: "ghost", disabled: switchUi.busy, onClick: switchUi.onCancel }, "Cancel")
        )
      );
    } else if (phase === "error") {
      body = h(
        "div",
        { className: "hca-switch-confirm" },
        h("p", null, switchUi.confirmError || "The switch failed for an unknown reason."),
        h(
          "div",
          { className: "hca-switch-actions" },
          h(C.Button, { type: "button", variant: "default", disabled: switchUi.busy, onClick: switchUi.onRetry }, "Try again"),
          h(C.Button, { type: "button", variant: "ghost", disabled: switchUi.busy, onClick: switchUi.onCancel }, "Cancel")
        )
      );
    } else {
      body = h(
        "div",
        { className: "hca-switch-confirm" },
        h("p", null, buildSwitchConfirmMessage(row, switchUi.subscriptionUsdPerMonth)),
        h(
          "div",
          { className: "hca-switch-actions" },
          h(
            C.Button,
            { type: "button", variant: "default", disabled: switchUi.busy, onClick: switchUi.onConfirm },
            switchUi.busy ? "Switching…" : "Confirm switch"
          ),
          h(C.Button, { type: "button", variant: "ghost", disabled: switchUi.busy, onClick: switchUi.onCancel }, "Cancel")
        )
      );
    }
    return h("tr", { className: "hca-switch-confirm-row" }, h("td", { colSpan: TOTAL_CATALOGUE_COLUMNS }, body));
  }

  // The persistent post-switch banner (v0.2 T11). Deliberately NOT tied to a
  // table row: a successful switch triggers a catalogue refetch, which can
  // reorder, re-page, or filter the switched-to row out of view entirely —
  // but the "what changed" summary and the one-click revert must survive
  // that, since a one-way door on a production agent is not acceptable.
  // Handles both outcomes that can land here: a completed switch (row-switch
  // or revert), and a revert attempt that hit the host's own guard.
  function SwitchOutcomeBanner({ outcome, busy, onRevert, onCancelRevertGuard, onDismiss }) {
    if (!outcome) return null;
    var prev = outcome.previous;
    var cur = outcome.current;
    return h(
      "div",
      { className: "hca-switch-banner" },
      cur ? h("p", null, "Switched model: " + modelRef(prev) + " → " + modelRef(cur) + ".") : null,
      // warning on a success must be visible, never swallowed into a plain
      // success message — shown verbatim, bold weight (no new colour).
      outcome.warning ? h("p", { className: "hca-switch-warning" }, outcome.warning) : null,
      outcome.guard_ran === false && cur
        ? h(
            "p",
            { className: "hca-switch-warning" },
            "The cost guard did not run for this switch — it was not checked against its published rate."
          )
        : null,
      outcome.detail ? h("p", null, outcome.detail) : null,
      outcome.revertGuard
        ? h(
            "div",
            { className: "hca-switch-actions-block" },
            h("p", null, outcome.revertGuard.message),
            outcome.revertGuard.target
              ? h("p", { className: "hca-notice" }, "Target: " + modelRef(outcome.revertGuard.target))
              : null,
            h(
              "div",
              { className: "hca-switch-actions" },
              h(
                C.Button,
                { type: "button", variant: "default", disabled: busy, onClick: () => onRevert(prev, true) },
                "Revert anyway"
              ),
              h(C.Button, { type: "button", variant: "ghost", disabled: busy, onClick: onCancelRevertGuard }, "Cancel")
            )
          )
        : h(
            "div",
            { className: "hca-switch-actions" },
            prev
              ? h(
                  C.Button,
                  { type: "button", variant: "default", disabled: busy, onClick: () => onRevert(prev, false) },
                  "Revert to " + modelRef(prev)
                )
              : null,
            h(C.Button, { type: "button", variant: "ghost", disabled: busy, onClick: onDismiss }, "Dismiss")
          )
    );
  }

  // Capability toggles for the catalogue search panel. Each is a hard
  // *requirement* when checked; unchecked imposes NO constraint — it never
  // means "must lack this capability". `tool_call` is the one filter that
  // starts checked: 1,137 of the 5,754 real models can't call a tool at all,
  // so they can't run the agent and comparing them on price is meaningless.
  // The user can still switch it off out of curiosity. The `hint` text is
  // surfaced as a title tooltip on each control so the on/off semantics are
  // legible even without reading the panel's caption line.
  const CAPABILITY_TOGGLES = [
    {
      key: "tool_call",
      label: "Tool calling",
      hint:
        "Checked: only show models that can call tools (required to run the agent). " +
        "Unchecked: no constraint — also shows models that can't call tools.",
    },
    {
      key: "vision",
      label: "Vision",
      hint: "Checked: only show models that accept image input. Unchecked: no constraint.",
    },
    {
      key: "reasoning",
      label: "Reasoning",
      hint: "Checked: only show models with a reasoning mode. Unchecked: no constraint.",
    },
    {
      key: "open_weights",
      label: "Open weights",
      hint: "Checked: only show models with open weights. Unchecked: no constraint.",
    },
  ];

  // Capability badge labels for one candidate row. Plain, factual strings —
  // this reflects what models.dev reports, never a quality or performance
  // judgement (models.dev carries no such signal for any of the 5,754 models).
  function capabilityBadgeLabels(capabilities) {
    if (!capabilities) return [];
    const labels = [];
    if (capabilities.tool_call) labels.push("tools");
    if (capabilities.vision) labels.push("vision");
    if (capabilities.reasoning) labels.push("reasoning");
    if (capabilities.open_weights) labels.push("open weights");
    return labels;
  }

  // Short description of the filters the server actually applied, read from
  // the echoed `filters` envelope (not local UI state) so the text always
  // matches the results on screen, including the debounced min-context value
  // and the normalised (trimmed/lowercased/deduped/sorted) providers list.
  function activeFilterSummary(filters) {
    if (!filters) return "no filters active";
    const parts = [];
    if (filters.tool_call) parts.push("tool calling required");
    if (filters.vision) parts.push("vision required");
    if (filters.reasoning) parts.push("reasoning required");
    if (filters.open_weights) parts.push("open weights required");
    if (filters.min_context) parts.push("context ≥ " + tokens(filters.min_context));
    if (filters.hide_free) parts.push("free models hidden");
    if (filters.providers && filters.providers.length) {
      const verb = filters.providers_mode === "exclude" ? "excluding" : "only";
      parts.push(verb + " " + filters.providers.join(", "));
    }
    return parts.length ? parts.join(", ") : "no filters active";
  }

  // Generic JSON GET against a fully-built plugin URL (path + querystring).
  // Callers own the querystring so a change to *any* param (days, sort,
  // order, limit, query, ...) produces a new url, which is this hook's
  // only dependency — that keeps the stale-response guard correct without
  // this hook needing to know which params exist.
  function useEndpoint(url) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);

    const load = useCallback(() => {
      setError(null);
      let ignore = false;
      SDK.fetchJSON(BASE + url)
        .then((result) => {
          if (!ignore) setData(result);
        })
        .catch((err) => {
          if (!ignore) setError(String(err));
        });
      return () => {
        ignore = true;
      };
    }, [url]);

    useEffect(load, [load]);
    return { data, error, reload: load };
  }

  // Debounces a fast-changing value (keystrokes) so effects that key off it
  // (here: the catalogue refetch) don't fire once per keystroke against a
  // 5,754-model catalogue.
  function useDebouncedValue(value, delayMs) {
    const [debounced, setDebounced] = useState(value);
    useEffect(() => {
      const timer = setTimeout(() => setDebounced(value), delayMs);
      return () => clearTimeout(timer);
    }, [value, delayMs]);
    return debounced;
  }

  function Notice({ text }) {
    return h("p", { className: "hca-notice" }, text);
  }

  // Compact card, reused by the /cost tab and the analytics:bottom slot.
  function SummaryCard() {
    const { data, error } = useEndpoint("/summary?days=30");
    if (error || !data || data.usage_available === false) return null;

    const verdict =
      data.ghost_cost_usd > data.subscription_usd_per_month
        ? "The subscription is cheaper at this volume."
        : "A pay-as-you-go API would be cheaper at this volume.";

    const unpricedText = unpricedCaveat(data.unpriced);

    return h(
      C.Card,
      null,
      h(C.CardHeader, null, h(C.CardTitle, null, "Cost arbitrage — last 30 days")),
      h(
        C.CardContent,
        null,
        h(
          "div",
          { className: "hca-row" },
          h("span", { className: "hca-big" }, money(data.ghost_cost_usd)),
          h("span", null, "at API rates vs " + money(data.subscription_usd_per_month) + " subscription")
        ),
        unpricedText ? h("p", { className: "hca-notice" }, unpricedText) : null,
        h("p", null, verdict),
        h(Notice, { text: data.notice })
      )
    );
  }

  function ModelTable({ rows }) {
    return h(
      "table",
      { className: "hca-table" },
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          h("th", { className: "hca-cell-left" }, "Model"),
          h("th", null, "Sessions"),
          h("th", null, "Input"),
          h("th", null, "Cache read"),
          h("th", null, "Output"),
          h("th", null, "Cost")
        )
      ),
      h(
        "tbody",
        null,
        rows.map((row) =>
          h(
            "tr",
            { key: row.model + row.billing_provider },
            h(
              "td",
              { className: "hca-cell-left" },
              row.model,
              row.billing_provider !== row.priced_as_provider
                ? h(C.Badge, { className: "hca-badge" }, "priced as " + row.priced_as_provider)
                : null,
              row.status === "ok"
                ? h(C.Badge, { className: "hca-badge" }, pricingSourceLabel(row.pricing_source))
                : null,
              row.status === "no_pricing"
                ? h(C.Badge, { className: "hca-badge" }, "pricing unknown")
                : row.status === "ok" && row.cache_status === "unknown"
                ? h(C.Badge, { className: "hca-badge" }, "no cache pricing")
                : null
            ),
            h("td", { className: "hca-num" }, row.sessions),
            h("td", { className: "hca-num" }, tokens(row.input_tokens)),
            h("td", { className: "hca-num" }, tokens(row.cache_read_tokens)),
            h("td", { className: "hca-num" }, tokens(row.output_tokens)),
            h("td", { className: "hca-num" }, money(row.headline_usd))
          )
        )
      )
    );
  }

  // Column definitions for the catalogue table. `sortKey` must match one of
  // the server's whitelisted `sort` values (see CATALOGUE_SORT_FIELDS in
  // plugin_api.py): model, provider, monthly, cache_aware, no_cache,
  // break_even. Every column here is sortable, so every header click
  // refetches from the server (sorting is server-side over 5,754 rows).
  const CATALOGUE_COLUMNS = [
    { key: "provider", label: "Provider", left: true },
    { key: "model", label: "Model", left: true },
    { key: "monthly", label: "Monthly" },
    { key: "cache_aware", label: "Cache-aware" },
    { key: "no_cache", label: "No cache" },
    { key: "break_even", label: "Break-even" },
  ];

  function CatalogueHeaderCell({ column, sort, order, onSort }) {
    const active = sort === column.key;
    const arrow = active ? (order === "desc" ? " ▼" : " ▲") : "";
    return h(
      "th",
      {
        className: column.left ? "hca-cell-left" : undefined,
        "aria-sort": active ? (order === "desc" ? "descending" : "ascending") : "none",
      },
      h(
        "button",
        { type: "button", className: "hca-sort-btn", onClick: () => onSort(column.key) },
        column.label + arrow
      )
    );
  }

  // The long-context upper bound cell (v0.2 Task 4). `tier_threshold_tokens`
  // and `long_context_usd` are `None` together whenever the model publishes
  // no tier — rendered as the plain words "not applicable", never `$0` and
  // never a bare dash (this deployment's `—` convention elsewhere means "a
  // known-priced value could not be computed", which this is not: there is
  // simply no tier to bound).
  //
  // `long_context_usd` is what the *whole measured usage* would cost if
  // every single call in it had landed above the threshold — never a split
  // of which calls actually did, because the sessions table only stores
  // aggregate token counts. When the workload's own observed average context
  // per call (`avgContextPerCall`, from `avg_context_per_call` in the
  // envelope) sits below the threshold, that is said plainly underneath the
  // figure rather than leaving a big number to speak for itself.
  function TierBoundCell({ row, avgContextPerCall }) {
    if (row.tier_threshold_tokens === null || row.tier_threshold_tokens === undefined) {
      return h("span", { className: "hca-notice" }, "not applicable");
    }
    const rarelyApplies = typeof avgContextPerCall === "number" && avgContextPerCall < row.tier_threshold_tokens;
    return h(
      "div",
      { className: "hca-tier-cell" },
      h("span", null, money(row.long_context_usd) + " above " + tokens(row.tier_threshold_tokens)),
      rarelyApplies
        ? h(
            "span",
            { className: "hca-notice" },
            "tier rarely applies at your measured ~" + tokens(Math.round(avgContextPerCall)) + " avg"
          )
        : null
    );
  }

  // Renders one page of catalogue rows (same row shape /whatif returns).
  // `cheaper_than_subscription` is marked with a green accent — but never
  // color alone: every marked row also carries a "cheaper" text badge, so
  // the signal survives color-blindness and monochrome displays.
  //
  // The trailing "Long-context bound" / "Capabilities" / "Context" columns
  // are plain <th>, not CatalogueHeaderCell — the server's sort whitelist
  // (CATALOGUE_SORT_FIELDS in plugin_api.py) has no key for any of them, so
  // none can be made sortable without a matching backend change.
  function CatalogueTable({ rows, sort, order, onSort, avgContextPerCall, switchUi }) {
    return h(
      "div",
      { className: "hca-table-wrap" },
      h(
        "table",
        { className: "hca-table" },
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            CATALOGUE_COLUMNS.map((column) =>
              h(CatalogueHeaderCell, { key: column.key, column, sort, order, onSort })
            ),
            h("th", { className: "hca-cell-left" }, "Long-context bound"),
            h("th", { className: "hca-cell-left" }, "Capabilities"),
            h("th", null, "Context"),
            h("th", { className: "hca-cell-left" }, "Action")
          )
        ),
        h(
          "tbody",
          null,
          rows.map((row) => {
            const badgeLabels = capabilityBadgeLabels(row.capabilities);
            const contextLimit = row.capabilities ? row.capabilities.context_limit : null;
            const rowKey = row.provider + "/" + row.model;
            const tr = h(
              "tr",
              {
                className: row.cheaper_than_subscription ? "hca-row-cheaper" : undefined,
              },
              h("td", { className: "hca-cell-left" }, row.provider),
              h(
                "td",
                { className: "hca-cell-left" },
                row.model,
                row.status === "ok"
                  ? h(C.Badge, { className: "hca-badge" }, pricingSourceLabel(row.pricing_source))
                  : null,
                row.status === "no_pricing" ? h(C.Badge, { className: "hca-badge" }, "pricing unknown") : null
              ),
              h(
                "td",
                { className: "hca-num" },
                money(row.monthly_usd),
                row.cheaper_than_subscription
                  ? h(C.Badge, { className: "hca-badge hca-cheaper-badge" }, "✓ cheaper")
                  : null
              ),
              h("td", { className: "hca-num" }, money(row.cache_aware_usd)),
              h("td", { className: "hca-num" }, money(row.no_cache_usd)),
              h(
                "td",
                { className: "hca-num" },
                row.break_even_volume_ratio === null || row.break_even_volume_ratio === undefined
                  ? "—"
                  : Math.round(row.break_even_volume_ratio * 100) + "% of volume"
              ),
              h(
                "td",
                { className: "hca-cell-left" },
                h(TierBoundCell, { row, avgContextPerCall })
              ),
              h(
                "td",
                { className: "hca-cell-left" },
                h(
                  "div",
                  { className: "hca-cap-badges" },
                  badgeLabels.length
                    ? badgeLabels.map((label) => h(C.Badge, { key: label, className: "hca-cap-badge" }, label))
                    : h("span", { className: "hca-notice" }, "none")
                )
              ),
              h("td", { className: "hca-num" }, contextLimit === null || contextLimit === undefined ? "—" : tokens(contextLimit)),
              h("td", { className: "hca-cell-left" }, h(SwitchActionCell, { row, switchUi }))
            );
            if (switchUi.confirmRowKey !== rowKey) {
              return h(React.Fragment, { key: rowKey }, tr);
            }
            return h(
              React.Fragment,
              { key: rowKey },
              tr,
              h(SwitchConfirmRow, { row, switchUi })
            );
          })
        )
      )
    );
  }

  // Pricing-cache freshness line + refresh button. `pricingData` is the
  // shared {updated_at, age_hours, available} envelope that now rides on
  // /summary, /whatif and /catalogue alike.
  // Three-angle UI audit, finding 2: this line used to read "Pricing data
  // updated ... ago", which claims freshness for every figure on the page.
  // It only ever measured one thing -- the mtime of the local models.dev
  // cache file. Rows priced from Hermes' own offline snapshot (openai,
  // anthropic, minimax, minimax-cn; see pricingSourceLabel above) are not
  // touched by this refresh at all and can be dated by months, so the
  // wording now names what it measures and points at the per-row badge for
  // what it doesn't.
  function PricingFreshness({ pricingData, onRefresh, refreshing, refreshError }) {
    let text;
    if (!pricingData || pricingData.available === false) {
      text = "models.dev cache freshness is unknown.";
    } else if (pricingData.updated_at && SDK.utils && typeof SDK.utils.isoTimeAgo === "function") {
      text =
        "models.dev cache updated " +
        SDK.utils.isoTimeAgo(pricingData.updated_at) +
        " -- rows priced from Hermes' own offline snapshot (see the source badge) aren't covered by this figure.";
    } else if (typeof pricingData.age_hours === "number") {
      text =
        "models.dev cache is " +
        pricingData.age_hours.toFixed(1) +
        "h old -- rows priced from Hermes' own offline snapshot (see the source badge) aren't covered by this figure.";
    } else {
      text = "models.dev cache freshness is unknown.";
    }

    return h(
      "div",
      null,
      h(
        "div",
        { className: "hca-row" },
        h("span", { className: "hca-notice" }, text),
        h(
          C.Button,
          { variant: "ghost", disabled: refreshing, onClick: onRefresh },
          refreshing ? "Refreshing…" : "Refresh pricing"
        )
      ),
      refreshError ? h(Notice, { text: refreshError }) : null
    );
  }

  // Capability search panel. Every control here is optional and editable —
  // this is a search filter, not a fixed toolbar with hard constraints.
  // `tool_call` starts checked (see CAPABILITY_TOGGLES); everything else
  // starts unchecked/empty, i.e. "no constraint". The caption line states the
  // on/off semantics in plain words once, up front, rather than leaving the
  // reader to infer it from checkbox conventions.
  function CapabilityFilters({ values, onToggle, minContextInput, onMinContextChange }) {
    return h(
      "div",
      { className: "hca-filters" },
      h(
        "p",
        { className: "hca-notice" },
        "Checked capabilities are required to appear below. Unchecked capabilities apply no constraint " +
          "— they do not exclude models that lack them."
      ),
      h(
        "div",
        { className: "hca-filter-toggles" },
        CAPABILITY_TOGGLES.map((toggle) =>
          h(
            "label",
            { key: toggle.key, className: "hca-filter-toggle", title: toggle.hint },
            h(Checkbox, {
              checked: !!values[toggle.key],
              onCheckedChange: (checked) => onToggle(toggle.key, checked === true),
            }),
            toggle.label
          )
        ),
        h(
          "label",
          {
            className: "hca-filter-toggle",
            title:
              "Minimum published context window, in tokens. 0 or blank applies no constraint. " +
              "A model with no published context limit never satisfies a threshold above 0.",
          },
          "Min. context",
          h(C.Input, {
            type: "number",
            min: "0",
            step: "1000",
            inputMode: "numeric",
            placeholder: "0",
            value: minContextInput,
            onChange: (e) => onMinContextChange(e.target.value),
            className: "hca-min-context",
          })
        )
      )
    );
  }

  // Provider include/exclude checkbox panel (v0.2 Task 8). Built from
  // GET /providers, fetched once per `days` change by the caller — this
  // component itself does no fetching. `providersData.providers` arrives
  // pre-sorted pinned-first-then-by-count from the server; this component
  // only ever filters that array (by the in-panel search box, client-side —
  // 172 rows is cheap to filter in JS) and never re-sorts it, so the pinned
  // providers stay on top exactly as the server put them.
  //
  // Mode semantics, spelled out once here rather than left to checkbox
  // convention: in "Include only" mode, only checked providers are shown; in
  // "Exclude" mode, checked providers are hidden and everything else is
  // shown. An *empty* checklist is "no constraint" in EITHER mode — it never
  // means "show nothing". That distinction matters most as the catalogue
  // grows over time (it has gained models — 9 in one hourly refresh — since
  // this UI was built): an include list only ever shows what you explicitly
  // checked, so a brand-new provider stays invisible until you check it by
  // hand; an exclude list has the opposite property, since it shows
  // everything you *haven't* checked, so a brand-new provider shows up on
  // its own unless you go back and exclude it.
  function ProviderPanel({ providersData, providersError, selected, mode, onToggleProvider, onSelectAll, onClearAll, onModeChange }) {
    const [search, setSearch] = useState("");
    const rows = (providersData && providersData.providers) || [];
    const needle = search.trim().toLowerCase();
    const visible = needle ? rows.filter((row) => row.provider.toLowerCase().includes(needle)) : rows;
    const selectedSet = useMemo(() => new Set(selected), [selected]);

    return h(
      "div",
      { className: "hca-provider-panel" },
      h(
        "p",
        { className: "hca-notice" },
        "\"Include only\" shows just the checked providers; \"Exclude\" hides the checked providers and " +
          "shows everything else. Either way, an empty checklist applies no constraint — nothing is hidden. " +
          "This matters as the catalogue grows: an include list never auto-adds a new provider, so it stays " +
          "hidden until you check it by hand, while an exclude list shows a new provider automatically unless " +
          "you go back and check it off."
      ),
      h(
        "div",
        { className: "hca-provider-mode" },
        h(
          C.Button,
          {
            type: "button",
            variant: mode === "include" ? "default" : "ghost",
            onClick: () => onModeChange("include"),
            title: "Show only the checked providers. An empty checklist shows every provider.",
          },
          "Include only"
        ),
        h(
          C.Button,
          {
            type: "button",
            variant: mode === "exclude" ? "default" : "ghost",
            onClick: () => onModeChange("exclude"),
            title:
              "Hide the checked providers, show every other provider — including any added to the " +
              "catalogue later. An empty checklist hides nothing.",
          },
          "Exclude"
        )
      ),
      h(
        "div",
        { className: "hca-provider-controls" },
        h(C.Input, {
          type: "search",
          placeholder: "Filter providers…",
          value: search,
          onChange: (e) => setSearch(e.target.value),
          className: "hca-provider-search",
        }),
        h(
          C.Button,
          { type: "button", variant: "ghost", onClick: () => onSelectAll(visible.map((row) => row.provider)) },
          "Select all"
        ),
        h(C.Button, { type: "button", variant: "ghost", onClick: onClearAll }, "Clear")
      ),
      providersError ? h("p", null, "Could not load providers: " + providersError) : null,
      h(
        "div",
        { className: "hca-provider-list" },
        visible.length === 0
          ? h("p", { className: "hca-notice" }, "No providers match.")
          : visible.map((row) =>
              h(
                "label",
                { key: row.provider, className: "hca-provider-item" },
                h(Checkbox, {
                  checked: selectedSet.has(row.provider),
                  onCheckedChange: (checked) => onToggleProvider(row.provider, checked === true),
                }),
                h("span", null, row.provider),
                row.pinned ? h(C.Badge, { className: "hca-badge" }, "pinned") : null,
                h("span", { className: "hca-notice" }, row.model_count.toLocaleString("en-US"))
              )
            )
      )
    );
  }

  // Prev/next pagination, driven entirely by the server's `page`/`pages`
  // (never a locally recomputed page number) so "page X of Y" and the
  // disabled state always agree with what's actually on screen. Renders
  // nothing when `pages` is 0 (no matches) rather than showing "page 1 of 0".
  function Pagination({ page, pages, onPrev, onNext }) {
    if (!pages) return null;
    return h(
      "div",
      { className: "hca-pagination" },
      h(C.Button, { type: "button", variant: "ghost", disabled: page <= 1, onClick: onPrev }, "Previous"),
      h("span", { className: "hca-notice" }, "Page " + page + " of " + pages),
      h(C.Button, { type: "button", variant: "ghost", disabled: page >= pages, onClick: onNext }, "Next")
    );
  }

  // The catalogue card: search, limit, sortable headers, freshness/refresh,
  // and the "showing N of M" line. Replaces the old static What-if table —
  // the same server-side query/sort/limit contract that used to only cover
  // 7 pinned candidates now spans the whole 5,754-model models.dev cache.
  function CatalogueCard({ days }) {
    const [searchInput, setSearchInput] = useState("");
    const debouncedQuery = useDebouncedValue(searchInput, SEARCH_DEBOUNCE_MS);
    const [sort, setSort] = useState("monthly");
    const [order, setOrder] = useState("asc");
    const [limit, setLimit] = useState(25);
    const [refreshing, setRefreshing] = useState(false);
    const [refreshError, setRefreshError] = useState(null);

    // Switch-model control state (v0.2 T11). Exactly one row's confirmation
    // panel can be open at a time (`confirmRowKey`); `switchBusyRef` is
    // checked synchronously — before React re-renders the `disabled` prop —
    // so a genuine double-click cannot fire two POSTs. `switchOutcome` is
    // deliberately separate from the per-row panel: it must survive the
    // catalogue refetch a successful switch triggers, even if that refetch
    // reorders, re-pages, or filters the switched-to row out of view.
    const [confirmRowKey, setConfirmRowKey] = useState(null);
    const [confirmRow, setConfirmRow] = useState(null);
    const [confirmPhase, setConfirmPhase] = useState("confirm"); // "confirm" | "guard" | "error"
    const [confirmGuard, setConfirmGuard] = useState(null); // { message, target }
    const [confirmError, setConfirmError] = useState(null);
    const [confirmForced, setConfirmForced] = useState(false); // last submit used confirm_expensive
    const [switchBusy, setSwitchBusy] = useState(false);
    const switchBusyRef = useRef(false);
    const [switchOutcome, setSwitchOutcome] = useState(null);

    const closeConfirm = useCallback(() => {
      setConfirmRowKey(null);
      setConfirmRow(null);
      setConfirmPhase("confirm");
      setConfirmGuard(null);
      setConfirmError(null);
      setConfirmForced(false);
    }, []);

    const openConfirm = useCallback(
      (row) => {
        setConfirmRowKey(row.provider + "/" + row.model);
        setConfirmRow(row);
        setConfirmPhase("confirm");
        setConfirmGuard(null);
        setConfirmError(null);
        setConfirmForced(false);
      },
      []
    );

    // Single call path shared by the row-switch flow and the revert flow.
    // Reads all nine response keys somewhere across this function and its
    // callers: ok, confirm_required, detail, warning, confirm_message,
    // guard_ran, previous, current, target.
    const runSwitch = useCallback(
      (target, confirmExpensive, handlers) => {
        if (switchBusyRef.current) return;
        switchBusyRef.current = true;
        setSwitchBusy(true);
        switchModelRequest({
          provider: target.provider,
          model: target.model,
          confirm_expensive: !!confirmExpensive,
        })
          .then((result) => {
            switchBusyRef.current = false;
            setSwitchBusy(false);
            if (result && result.confirm_required) {
              handlers.onGuard(result);
              return;
            }
            if (result && result.ok) {
              handlers.onSuccess(result);
              // Refetch so the tab does not keep showing stale state.
              catalogue.reload();
              return;
            }
            handlers.onError((result && result.detail) || "The switch failed for an unknown reason.");
          })
          .catch((err) => {
            switchBusyRef.current = false;
            setSwitchBusy(false);
            handlers.onError(String(err));
          });
        // catalogue.reload identity changes with `url`; same non-issue as
        // handleRefresh above — this handler only needs the latest reload.
      },
      [catalogue.reload]
    );

    const handleConfirmSubmit = useCallback(() => {
      if (!confirmRow) return;
      const target = { provider: confirmRow.provider, model: confirmRow.model };
      setConfirmForced(false);
      runSwitch(target, false, {
        onGuard: (result) => {
          setConfirmPhase("guard");
          setConfirmGuard({ message: result.confirm_message, target: result.target });
        },
        onSuccess: (result) => {
          closeConfirm();
          setSwitchOutcome({
            ok: true,
            previous: result.previous,
            current: result.current,
            warning: result.warning,
            guard_ran: result.guard_ran,
            detail: null,
            revertGuard: null,
          });
        },
        onError: (detail) => {
          setConfirmPhase("error");
          setConfirmError(detail);
        },
      });
    }, [confirmRow, runSwitch, closeConfirm]);

    const handleGuardConfirm = useCallback(() => {
      if (!confirmRow) return;
      const target = { provider: confirmRow.provider, model: confirmRow.model };
      setConfirmForced(true);
      runSwitch(target, true, {
        onGuard: (result) => {
          // Defensive: the host should not fire the guard twice in a row once
          // confirm_expensive is set, but if it does, stay in guard phase
          // rather than silently proceeding.
          setConfirmPhase("guard");
          setConfirmGuard({ message: result.confirm_message, target: result.target });
        },
        onSuccess: (result) => {
          closeConfirm();
          setSwitchOutcome({
            ok: true,
            previous: result.previous,
            current: result.current,
            warning: result.warning,
            guard_ran: result.guard_ran,
            detail: null,
            revertGuard: null,
          });
        },
        onError: (detail) => {
          setConfirmPhase("error");
          setConfirmError(detail);
        },
      });
    }, [confirmRow, runSwitch, closeConfirm]);

    const handleRetryConfirm = useCallback(() => {
      if (confirmForced) {
        handleGuardConfirm();
      } else {
        handleConfirmSubmit();
      }
    }, [confirmForced, handleGuardConfirm, handleConfirmSubmit]);

    // Revert is one-click by design — no cost-projection confirmation of our
    // own, since it only undoes a switch this session already confirmed.
    // The host's own expensive-model guard still applies unconditionally
    // (confirmExpensive lets a "Revert anyway" resubmit it explicitly).
    const handleRevert = useCallback(
      (previous, confirmExpensive) => {
        if (!previous) return;
        runSwitch({ provider: previous.provider, model: previous.model }, confirmExpensive, {
          onGuard: (result) => {
            setSwitchOutcome((prev) =>
              Object.assign({}, prev, {
                revertGuard: { message: result.confirm_message, target: result.target },
              })
            );
          },
          onSuccess: (result) => {
            setSwitchOutcome({
              ok: true,
              previous: result.previous,
              current: result.current,
              warning: result.warning,
              guard_ran: result.guard_ran,
              detail: null,
              revertGuard: null,
            });
          },
          onError: (detail) => {
            setSwitchOutcome((prev) =>
              Object.assign({}, prev || {}, { ok: false, detail: detail, revertGuard: null })
            );
          },
        });
      },
      [runSwitch]
    );

    const handleCancelRevertGuard = useCallback(() => {
      setSwitchOutcome((prev) => (prev ? Object.assign({}, prev, { revertGuard: null }) : null));
    }, []);

    const handleDismissOutcome = useCallback(() => setSwitchOutcome(null), []);

    // Capability filters. `tool_call` matches the server's default (true);
    // the rest match the server's "off" default (no constraint). Kept as one
    // object so a single toggle handler covers all four boolean filters.
    const [filters, setFilters] = useState({
      tool_call: true,
      vision: false,
      reasoning: false,
      open_weights: false,
    });
    const handleToggle = useCallback((key, value) => {
      setFilters((prev) => Object.assign({}, prev, { [key]: value }));
    }, []);

    // min_context is a numeric text field, same debounce treatment as the
    // search box — a refetch per keystroke against 5,754 models would be the
    // same waste sorting/searching already avoid.
    const [minContextInput, setMinContextInput] = useState("");
    const debouncedMinContextInput = useDebouncedValue(minContextInput, SEARCH_DEBOUNCE_MS);
    const minContext = useMemo(() => {
      const parsed = Number(debouncedMinContextInput);
      return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
    }, [debouncedMinContextInput]);

    // Provider include/exclude filter (v0.2 Task 8). `selectedProviders` is
    // never re-sorted client-side — it's just the set of names the user has
    // checked, in whatever order they were checked. `providersMode` decides
    // whether that set is an include list or an exclude list; see
    // ProviderPanel's docstring for the full semantics (an empty set is "no
    // constraint" in either mode). Discrete choices, not typing — no
    // debounce, unlike the search box and min-context field above.
    const [selectedProviders, setSelectedProviders] = useState([]);
    const [providersMode, setProvidersMode] = useState("include");
    const handleToggleProvider = useCallback((name, checked) => {
      setSelectedProviders((prev) => {
        if (checked) return prev.includes(name) ? prev : prev.concat([name]);
        return prev.filter((existing) => existing !== name);
      });
    }, []);
    const handleSelectAllProviders = useCallback((names) => {
      setSelectedProviders((prev) => {
        const merged = prev.slice();
        names.forEach((name) => {
          if (!merged.includes(name)) merged.push(name);
        });
        return merged;
      });
    }, []);
    const handleClearProviders = useCallback(() => setSelectedProviders([]), []);

    // Hide-free toggle (v0.2 Task 8). Default on — the user asked for this
    // because free ($0-priced) models otherwise flood the top of an
    // ascending sort. Switchable off, never a hard constraint.
    const [hideFree, setHideFree] = useState(true);

    // GET /providers is fetched once per `days` change, not on every
    // catalogue refetch: this hook's own url depends only on `days`, so it
    // re-runs solely when that changes, independent of sort/search/filters/
    // paging on the catalogue hook below.
    const providersFacet = useEndpoint("/providers?days=" + days);

    // offset (the current page) is intentionally NOT part of this key: it's
    // the one thing paging itself is allowed to change without a reset. Every
    // other query-shaping input funnels through here, so "reset offset to 0
    // on any search/sort/order/limit/filter change" has exactly one place to
    // get right instead of one per setter.
    const queryKey = useMemo(
      () =>
        JSON.stringify([
          days,
          sort,
          order,
          limit,
          debouncedQuery,
          filters,
          minContext,
          selectedProviders,
          providersMode,
          hideFree,
        ]),
      [days, sort, order, limit, debouncedQuery, filters, minContext, selectedProviders, providersMode, hideFree]
    );
    const [offset, setOffset] = useState(0);
    const isFirstQueryKey = useRef(true);
    useEffect(() => {
      if (isFirstQueryKey.current) {
        isFirstQueryKey.current = false;
        return;
      }
      setOffset(0);
    }, [queryKey]);

    const url = useMemo(() => {
      const params = new URLSearchParams();
      params.set("days", String(days));
      params.set("sort", sort);
      params.set("order", order);
      params.set("limit", String(limit));
      params.set("offset", String(offset));
      if (debouncedQuery) params.set("query", debouncedQuery);
      params.set("tool_call", String(filters.tool_call));
      params.set("vision", String(filters.vision));
      params.set("reasoning", String(filters.reasoning));
      params.set("open_weights", String(filters.open_weights));
      params.set("min_context", String(minContext));
      if (selectedProviders.length) params.set("providers", selectedProviders.join(","));
      params.set("providers_mode", providersMode);
      params.set("hide_free", String(hideFree));
      return "/catalogue?" + params.toString();
    }, [
      days,
      sort,
      order,
      limit,
      offset,
      debouncedQuery,
      filters,
      minContext,
      selectedProviders,
      providersMode,
      hideFree,
    ]);

    const catalogue = useEndpoint(url);

    // Excluded-count feedback: how many models the *last filter/search/limit
    // change* added or removed from total_matched. This needs no second
    // request — it just remembers the previous response's total_matched in a
    // ref and diffs against the new one whenever `catalogue.data` changes.
    // (An absolute "N of 5,754 unfiltered" figure would need a second
    // uncached query against the full catalogue on every filter change; this
    // delta gives the same "feel the effect of the toggle" signal for free.)
    const prevTotalRef = useRef(null);
    const [totalDelta, setTotalDelta] = useState(null);
    useEffect(() => {
      if (!catalogue.data || typeof catalogue.data.total_matched !== "number") return;
      const current = catalogue.data.total_matched;
      setTotalDelta(prevTotalRef.current === null ? null : current - prevTotalRef.current);
      prevTotalRef.current = current;
    }, [catalogue.data]);

    const handleSort = useCallback(
      (key) => {
        if (sort === key) {
          setOrder(order === "asc" ? "desc" : "asc");
        } else {
          setSort(key);
          setOrder("asc");
        }
      },
      [sort, order]
    );

    // Page size (limit) doubles as the step: moving one page forward/back is
    // exactly +/- the current limit. Clamped at 0 on the way back so a fast
    // double-click can't push offset negative.
    const handlePrevPage = useCallback(() => {
      setOffset((prev) => Math.max(0, prev - limit));
    }, [limit]);
    const handleNextPage = useCallback(() => {
      setOffset((prev) => prev + limit);
    }, [limit]);

    const handleRefresh = useCallback(() => {
      setRefreshing(true);
      setRefreshError(null);
      SDK.fetchJSON(BASE + "/refresh-pricing", { method: "POST" })
        .then((result) => {
          setRefreshing(false);
          if (result && result.ok) {
            catalogue.reload();
          } else {
            setRefreshError((result && result.detail) || "Refresh failed for an unknown reason.");
          }
        })
        .catch((err) => {
          setRefreshing(false);
          setRefreshError(String(err));
        });
      // catalogue.reload identity changes with `url`; that's fine, this
      // handler only needs the latest reload, not a stable identity.
    }, [catalogue.reload]);

    const data = catalogue.data;

    const switchUi = {
      confirmRowKey,
      confirmPhase,
      confirmGuard,
      confirmError,
      busy: switchBusy,
      subscriptionUsdPerMonth: data ? data.subscription_usd_per_month : null,
      onToggle: (row) => {
        const key = row.provider + "/" + row.model;
        if (confirmRowKey === key) {
          closeConfirm();
        } else {
          openConfirm(row);
        }
      },
      onConfirm: handleConfirmSubmit,
      onGuardConfirm: handleGuardConfirm,
      onRetry: handleRetryConfirm,
      onCancel: closeConfirm,
    };

    return h(
      C.Card,
      null,
      h(C.CardHeader, null, h(C.CardTitle, null, "Catalogue — all priced models")),
      h(
        C.CardContent,
        null,
        h(PricingFreshness, {
          pricingData: data ? data.pricing_data : null,
          onRefresh: handleRefresh,
          refreshing,
          refreshError,
        }),
        h(C.Separator, null),
        h(SwitchOutcomeBanner, {
          outcome: switchOutcome,
          busy: switchBusy,
          onRevert: handleRevert,
          onCancelRevertGuard: handleCancelRevertGuard,
          onDismiss: handleDismissOutcome,
        }),
        catalogue.error ? h("p", null, "Could not load the catalogue: " + catalogue.error) : null,

        data && data.usage_available === false
          ? h(
              "div",
              null,
              h(
                "p",
                null,
                "The session database could not be read, so no catalogue figures can be shown."
              ),
              data.usage_unavailable_reason ? h(Notice, { text: data.usage_unavailable_reason }) : null
            )
          : null,

        data && data.usage_available !== false
          ? h(
              React.Fragment,
              null,
              data.models_dev_available === false
                ? h(Notice, {
                    text:
                      "Provider rates could not be loaded, so some candidates below cannot be priced.",
                  })
                : null,
              h(
                "div",
                { className: "hca-actions" },
                h(C.Input, {
                  type: "search",
                  placeholder: "Search provider or model…",
                  value: searchInput,
                  onChange: (e) => setSearchInput(e.target.value),
                  className: "hca-search",
                }),
                h(
                  "label",
                  { className: "hca-page-size-label" },
                  "Page size",
                  h(
                    C.Select,
                    { value: String(limit), onValueChange: (v) => setLimit(Number(v)) },
                    CATALOGUE_LIMITS.map((n) =>
                      h(C.SelectOption, { key: n, value: String(n) }, n + " per page")
                    )
                  )
                )
              ),
              h(CapabilityFilters, {
                values: filters,
                onToggle: handleToggle,
                minContextInput,
                onMinContextChange: setMinContextInput,
              }),
              h(
                "div",
                { className: "hca-display-options" },
                h(
                  "label",
                  {
                    className: "hca-filter-toggle",
                    title:
                      "Checked (default): hide models whose published rates are exactly $0 for both " +
                      "input and output. Unchecked: show them too. Based on the model's published rate " +
                      "card, never on the cost computed for your current usage window (an empty usage " +
                      "window would otherwise price every model at $0 and hide the whole catalogue).",
                  },
                  h(Checkbox, {
                    checked: hideFree,
                    onCheckedChange: (checked) => setHideFree(checked === true),
                  }),
                  "Hide free models"
                )
              ),
              h(ProviderPanel, {
                providersData: providersFacet.data,
                providersError: providersFacet.error,
                selected: selectedProviders,
                mode: providersMode,
                onToggleProvider: handleToggleProvider,
                onSelectAll: handleSelectAllProviders,
                onClearAll: handleClearProviders,
                onModeChange: setProvidersMode,
              }),
              h(
                "p",
                { className: "hca-notice" },
                "Showing " +
                  data.returned.toLocaleString("en-US") +
                  " of " +
                  data.total_matched.toLocaleString("en-US") +
                  " models" +
                  (debouncedQuery ? ' matching "' + debouncedQuery + '"' : "") +
                  " (" +
                  activeFilterSummary(data.filters) +
                  "), repriced against your last " +
                  days +
                  "-day usage vs a " +
                  money(data.subscription_usd_per_month) +
                  "/month subscription."
              ),
              totalDelta
                ? h(
                    "p",
                    { className: "hca-notice" },
                    totalDelta > 0
                      ? "▲ " + totalDelta.toLocaleString("en-US") + " more models match than before this change."
                      : "▼ " +
                          Math.abs(totalDelta).toLocaleString("en-US") +
                          " fewer models match than before this change."
                  )
                : null,
              // Three-angle UI audit, finding 3 (the subtlest of the three):
              // Monthly and Cache-aware reprice the whole measured usage
              // vector -- 86% cache-read, 0% cache-write in production, an
              // OpenAI-specific automatic-prefix-caching artifact, not a
              // property of the work itself -- against every candidate's
              // published cache rate. A candidate that cannot actually reach
              // that hit rate (Anthropic in particular bills cache creation
              // at 1.25x input and needs markers Hermes only sends on its own
              // protocol) would in practice cost far closer to No cache. That
              // column is already the honest upper bound; what was missing is
              // that the green "cheaper" highlight follows the optimistic
              // figure, so it's said once, here, rather than once per row.
              h(Notice, {
                text:
                  "Monthly and Cache-aware price every candidate on your current provider's measured " +
                  "cache-hit profile, which is a property of that provider's caching behaviour, not of your " +
                  "work, and may not transfer to a different provider or model — No cache is the bound if it " +
                  "doesn't. The green \"cheaper\" highlight follows Monthly, so a green row assumes that " +
                  "profile carries over.",
              }),
              h(Notice, {
                text:
                  "Long-context bound: what your combined usage would cost if every single call had " +
                  "landed above that model's tier threshold. It is an upper bound, not an estimate — the " +
                  "underlying data records total tokens per window, not per-call context size, so the real " +
                  "split above/below a threshold can't be known. It never changes the Monthly, Cache-aware " +
                  "or No cache figures, and reads \"not applicable\" rather than $0 for a model that " +
                  "publishes no tier." +
                  (typeof data.avg_context_per_call === "number"
                    ? " Your measured average context per call across this window is ~" +
                      tokens(Math.round(data.avg_context_per_call)) +
                      "."
                    : ""),
              }),
              h(CatalogueTable, {
                rows: data.candidates || [],
                sort,
                order,
                onSort: handleSort,
                avgContextPerCall: data.avg_context_per_call,
                switchUi,
              }),
              h(Pagination, {
                page: data.page,
                pages: data.pages,
                onPrev: handlePrevPage,
                onNext: handleNextPage,
              })
            )
          : null
      )
    );
  }

  function CostTab() {
    const [days, setDays] = useState(30);
    const summary = useEndpoint("/summary?days=" + days);

    return h(
      "div",
      { className: "hca-page" },
      h(
        "div",
        { className: "hca-row" },
        h("h1", null, "Cost"),
        h(
          "div",
          { className: "hca-actions" },
          [7, 30, 90].map((value) =>
            h(
              C.Button,
              {
                key: value,
                onClick: () => setDays(value),
                variant: value === days ? "default" : "ghost",
              },
              value + "d"
            )
          )
        )
      ),

      summary.error
        ? h(C.Card, null, h(C.CardContent, null, "Could not load usage: " + summary.error))
        : null,

      summary.data && summary.data.usage_available === false
        ? h(
            C.Card,
            null,
            h(C.CardHeader, null, h(C.CardTitle, null, "Usage data unavailable")),
            h(
              C.CardContent,
              null,
              h(
                "p",
                null,
                "The session database could not be read, so no cost figures can be " +
                  "shown for this window."
              ),
              summary.data.usage_unavailable_reason
                ? h("p", { className: "hca-notice" }, summary.data.usage_unavailable_reason)
                : null
            )
          )
        : null,

      summary.data && summary.data.usage_available !== false
        ? h(
            C.Card,
            null,
            h(C.CardHeader, null, h(C.CardTitle, null, "Ghost cost — last " + days + " days")),
            h(
              C.CardContent,
              null,
              h(
                "div",
                { className: "hca-row" },
                h("span", { className: "hca-big" }, money(summary.data.ghost_cost_usd)),
                h(
                  "span",
                  null,
                  "projected monthly " +
                    money(summary.data.monthly_projection_usd) +
                    " vs " +
                    money(summary.data.subscription_usd_per_month) +
                    " subscription"
                )
              ),
              (function () {
                const unpricedText = unpricedCaveat(summary.data.unpriced);
                return unpricedText ? h("p", { className: "hca-notice" }, unpricedText) : null;
              })(),
              h(Notice, { text: summary.data.notice }),
              h(C.Separator, null),
              h(ModelTable, { rows: summary.data.models || [] })
            )
          )
        : null,

      h(CatalogueCard, { days })
    );
  }

  REGISTRY.register(PLUGIN, CostTab);
  // NOTE: the runtime signature is registerSlot(plugin, slot, component).
  // The sdk.d.ts declaration claims (slot, name, component) and is wrong —
  // following it registers into a slot named after the plugin and renders
  // nothing. Verified against web/src/plugins/slots.ts.
  REGISTRY.registerSlot(PLUGIN, "analytics:bottom", SummaryCard);
})();
