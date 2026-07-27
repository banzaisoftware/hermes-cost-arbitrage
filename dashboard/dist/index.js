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
  const { useState, useEffect, useCallback, useMemo } = SDK.hooks;
  const C = SDK.components;

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

  // Renders one page of catalogue rows (same row shape /whatif returns).
  // `cheaper_than_subscription` is marked with a green accent — but never
  // color alone: every marked row also carries a "cheaper" text badge, so
  // the signal survives color-blindness and monochrome displays.
  function CatalogueTable({ rows, sort, order, onSort }) {
    return h(
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
          )
        )
      ),
      h(
        "tbody",
        null,
        rows.map((row) =>
          h(
            "tr",
            {
              key: row.provider + "/" + row.model,
              className: row.cheaper_than_subscription ? "hca-row-cheaper" : undefined,
            },
            h("td", { className: "hca-cell-left" }, row.provider),
            h(
              "td",
              { className: "hca-cell-left" },
              row.model,
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
            )
          )
        )
      )
    );
  }

  // Pricing-cache freshness line + refresh button. `pricingData` is the
  // shared {updated_at, age_hours, available} envelope that now rides on
  // /summary, /whatif and /catalogue alike.
  function PricingFreshness({ pricingData, onRefresh, refreshing, refreshError }) {
    let text;
    if (!pricingData || pricingData.available === false) {
      text = "Pricing cache freshness is unknown.";
    } else if (pricingData.updated_at && SDK.utils && typeof SDK.utils.isoTimeAgo === "function") {
      text = "Pricing data updated " + SDK.utils.isoTimeAgo(pricingData.updated_at) + ".";
    } else if (typeof pricingData.age_hours === "number") {
      text = "Pricing data is " + pricingData.age_hours.toFixed(1) + "h old.";
    } else {
      text = "Pricing cache freshness is unknown.";
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

    const url = useMemo(() => {
      const params = new URLSearchParams();
      params.set("days", String(days));
      params.set("sort", sort);
      params.set("order", order);
      params.set("limit", String(limit));
      if (debouncedQuery) params.set("query", debouncedQuery);
      return "/catalogue?" + params.toString();
    }, [days, sort, order, limit, debouncedQuery]);

    const catalogue = useEndpoint(url);

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
                  C.Select,
                  { value: String(limit), onValueChange: (v) => setLimit(Number(v)) },
                  CATALOGUE_LIMITS.map((n) =>
                    h(C.SelectOption, { key: n, value: String(n) }, n + " rows")
                  )
                )
              ),
              h(
                "p",
                { className: "hca-notice" },
                "Showing " +
                  data.returned.toLocaleString("en-US") +
                  " of " +
                  data.total_matched.toLocaleString("en-US") +
                  (debouncedQuery ? ' matching "' + debouncedQuery + '"' : "") +
                  ", repriced against your last " +
                  days +
                  "-day usage vs a " +
                  money(data.subscription_usd_per_month) +
                  "/month subscription."
              ),
              h(CatalogueTable, { rows: data.candidates || [], sort, order, onSort: handleSort })
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
