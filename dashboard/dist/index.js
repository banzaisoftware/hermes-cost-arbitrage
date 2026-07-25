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
  const { useState, useEffect, useCallback } = SDK.hooks;
  const C = SDK.components;

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

  function useEndpoint(path, days) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);

    const load = useCallback(() => {
      setError(null);
      SDK.fetchJSON(BASE + path + "?days=" + days)
        .then(setData)
        .catch((err) => setError(String(err)));
    }, [path, days]);

    useEffect(load, [load]);
    return { data, error, reload: load };
  }

  function Notice({ text }) {
    return h("p", { className: "hca-notice" }, text);
  }

  // Compact card, reused by the /cost tab and the analytics:bottom slot.
  function SummaryCard() {
    const { data, error } = useEndpoint("/summary", 30);
    if (error || !data) return null;

    const verdict =
      data.ghost_cost_usd > data.subscription_usd_per_month
        ? "The subscription is cheaper at this volume."
        : "A pay-as-you-go API would be cheaper at this volume.";

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
          h("th", null, "Model"),
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
              null,
              row.model,
              row.billing_provider !== row.priced_as_provider
                ? h(C.Badge, { className: "hca-badge" }, "priced as " + row.priced_as_provider)
                : null,
              row.cache_status === "unknown"
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

  function CandidateTable({ rows }) {
    return h(
      "table",
      { className: "hca-table" },
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          h("th", null, "Candidate"),
          h("th", null, "Monthly"),
          h("th", null, "Cache-aware"),
          h("th", null, "No cache"),
          h("th", null, "Break-even")
        )
      ),
      h(
        "tbody",
        null,
        rows.map((row) =>
          h(
            "tr",
            { key: row.provider + "/" + row.model },
            h(
              "td",
              null,
              row.model,
              h(C.Badge, null, row.provider),
              row.status === "no_pricing" ? h(C.Badge, null, "pricing unknown") : null
            ),
            h("td", { className: "hca-num" }, money(row.monthly_usd)),
            h("td", { className: "hca-num" }, money(row.cache_aware_usd)),
            h("td", { className: "hca-num" }, money(row.no_cache_usd)),
            h(
              "td",
              { className: "hca-num" },
              row.break_even_volume_ratio === null || row.break_even_volume_ratio === undefined
                ? "—"
                : Math.round(row.break_even_volume_ratio * 100) + "% of volume"
            )
          )
        )
      )
    );
  }

  function CostTab() {
    const [days, setDays] = useState(30);
    const summary = useEndpoint("/summary", days);
    const whatif = useEndpoint("/whatif", days);

    return h(
      "div",
      { className: "hca-page" },
      h(
        "div",
        { className: "hca-row" },
        h("h1", null, "Cost"),
        h(
          "div",
          null,
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

      summary.data
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
              h(Notice, { text: summary.data.notice }),
              h(C.Separator, null),
              h(ModelTable, { rows: summary.data.models || [] })
            )
          )
        : null,

      whatif.data
        ? h(
            C.Card,
            null,
            h(C.CardHeader, null, h(C.CardTitle, null, "What if — pinned candidates")),
            h(C.CardContent, null, h(CandidateTable, { rows: whatif.data.candidates || [] }))
          )
        : null
    );
  }

  REGISTRY.register(PLUGIN, CostTab);
  // NOTE: the runtime signature is registerSlot(plugin, slot, component).
  // The sdk.d.ts declaration claims (slot, name, component) and is wrong —
  // following it registers into a slot named after the plugin and renders
  // nothing. Verified against web/src/plugins/slots.ts.
  REGISTRY.registerSlot(PLUGIN, "analytics:bottom", SummaryCard);
})();
