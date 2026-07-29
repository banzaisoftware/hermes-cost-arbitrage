// Evaluate dashboard/dist/index.js the way a browser would, against a stubbed
// host SDK, and assert the plugin actually registers.
//
// `node --check` parses but never executes, so it is blind to the whole class
// of errors that kills this bundle at load time — a `const` read before its
// declaration throws a ReferenceError from its temporal dead zone, which is
// valid syntax. Two such errors shipped in one commit: one took the entire
// /cost tab off the dashboard, because it threw before REGISTRY.register ran.
//
// This is deliberately not a rendering test. It evaluates the module and calls
// each registered component once with stub hooks, which is enough to catch a
// dependency array or a top-level statement reading something declared later.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "dashboard", "dist", "index.js"), "utf8");

const noop = () => undefined;
const identity = (fn) => fn;

const SDK = {
  React: {
    createElement: (type, props, ...children) => ({ type, props, children }),
    Fragment: Symbol("Fragment"),
  },
  hooks: {
    useState: (initial) => [typeof initial === "function" ? initial() : initial, noop],
    useEffect: noop,
    useCallback: identity,
    useMemo: (fn) => fn(),
    useRef: (initial) => ({ current: initial === undefined ? null : initial }),
  },
  // Any component the bundle asks for resolves to a marker; createElement above
  // does not care what `type` is.
  components: new Proxy({}, { get: (_t, name) => `Host<${String(name)}>` }),
  fetchJSON: async () => ({}),
};

const registered = { tabs: [], slots: [] };
globalThis.window = {
  __HERMES_PLUGIN_SDK__: SDK,
  __HERMES_PLUGINS__: {
    register: (plugin, component) => registered.tabs.push({ plugin, component }),
    registerSlot: (plugin, slot, component) => registered.slots.push({ plugin, slot, component }),
  },
};

const fail = (message) => {
  console.error("BUNDLE SMOKE FAILED: " + message);
  process.exit(1);
};

try {
  new Function(source)();
} catch (error) {
  fail(`the bundle threw while loading — ${error.constructor.name}: ${error.message}`);
}

if (registered.tabs.length !== 1) {
  fail(`expected exactly one registered tab, got ${registered.tabs.length}`);
}
if (registered.slots.length !== 1) {
  fail(`expected exactly one registered slot, got ${registered.slots.length}`);
}
// registerSlot's real runtime signature is (plugin, slot, component); the
// SDK's own .d.ts declares (slot, name, component) and is wrong. Following the
// declaration registers into a slot named after the plugin and renders nothing,
// silently — so pin the order here.
if (registered.slots[0].slot !== "analytics:bottom") {
  fail(`slot registered as "${registered.slots[0].slot}", expected "analytics:bottom"`);
}

// Render depth-first, calling every nested function component the tree yields.
// Calling only the registered components is not enough: createElement merely
// *references* a nested component, so its body never runs — which is how a
// dependency array reading a `const` declared further down survived a smoke
// test that stopped at the top level.
const MAX_DEPTH = 12;
const rendered = new Set();
// Every function component encountered during the default-state walk, keyed
// by name, so specific ones (e.g. SwitchOutcomeBanner) can be re-invoked
// below with hand-built props to reach states the default hook stubs never
// produce — the default useState stub always yields switchOutcome === null.
const componentsByName = new Map();

function renderDeep(node, path, depth) {
  if (depth > MAX_DEPTH || node === null || node === undefined || node === false) return;
  if (Array.isArray(node)) {
    node.forEach((child) => renderDeep(child, path, depth + 1));
    return;
  }
  if (typeof node !== "object" || !("type" in node)) return;

  let output = node;
  if (typeof node.type === "function") {
    const name = node.type.name || "anonymous";
    rendered.add(name);
    componentsByName.set(name, node.type);
    try {
      output = node.type(node.props || {});
    } catch (error) {
      fail(`rendering <${name}> (via ${path}) threw — ${error.constructor.name}: ${error.message}`);
    }
    renderDeep(output, `${path} > ${name}`, depth + 1);
    return;
  }
  renderDeep(node.children, path, depth + 1);
}

for (const { plugin, component } of [...registered.tabs, ...registered.slots]) {
  renderDeep({ type: component, props: {} }, plugin, 0);
}

if (rendered.size < 3) {
  fail(`only ${rendered.size} component(s) actually executed — the walk is not reaching nested components`);
}

// SwitchOutcomeBanner's probe advisory line (Task 3) is only reachable with a
// non-null `outcome` carrying a `probe` field, which the default walk above
// never produces — the stubbed useState always returns its initial value, and
// switchOutcome starts out null. Drive the component directly instead.
const SwitchOutcomeBanner = componentsByName.get("SwitchOutcomeBanner");
if (!SwitchOutcomeBanner) {
  fail("SwitchOutcomeBanner was never rendered during the walk — cannot exercise its probe states");
}

// Collect every rendered <p> element's text (as SDK.React.createElement joins
// it: a children array) so assertions below can check for exact, verbatim
// content rather than merely "it didn't throw".
function collectParagraphTexts(node, acc) {
  if (node === null || node === undefined || node === false) return acc;
  if (Array.isArray(node)) {
    node.forEach((child) => collectParagraphTexts(child, acc));
    return acc;
  }
  if (typeof node !== "object" || !("type" in node)) return acc;
  if (node.type === "p") acc.push((node.children || []).join(""));
  collectParagraphTexts(node.children, acc);
  return acc;
}

const baseOutcome = {
  ok: true,
  previous: { model: "gpt-4o-mini", provider: "openai" },
  current: { model: "kimi-k2.6", provider: "nvidia" },
  warning: null,
  guard_ran: true,
  detail: null,
  revertGuard: null,
};
const bannerHandlers = { busy: false, onRevert: noop, onCancelRevertGuard: noop, onDismiss: noop };

// Regression case: `probe` missing or null entirely (an older cached page, or
// a branch that never reached the probe). This is the exact shape that white-
// screened the tab twice before — a null dereference or a TDZ read on `probe`
// would throw here, not just fail an assertion.
for (const probeValue of [null, undefined]) {
  let tree;
  try {
    tree = SwitchOutcomeBanner({ outcome: Object.assign({}, baseOutcome, { probe: probeValue }), ...bannerHandlers });
  } catch (error) {
    fail(`SwitchOutcomeBanner threw with probe: ${probeValue} — ${error.constructor.name}: ${error.message}`);
  }
  const texts = collectParagraphTexts(tree, []);
  if (texts.some((t) => t.indexOf("Entitlement probe") !== -1)) {
    fail(`SwitchOutcomeBanner rendered a probe advisory line with probe: ${probeValue}`);
  }
}

// Probe-warning case: the switch succeeded but the probe came back throttled.
// The advisory line must name the status and carry the provider's message
// verbatim.
{
  const probe = {
    status: "throttled",
    http_status: 429,
    provider_message: "rate limit exceeded, retry after 30s",
    reason: "The provider throttled the probe call.",
  };
  let tree;
  try {
    tree = SwitchOutcomeBanner({ outcome: Object.assign({}, baseOutcome, { probe }), ...bannerHandlers });
  } catch (error) {
    fail(`SwitchOutcomeBanner threw in the probe-warning state — ${error.constructor.name}: ${error.message}`);
  }
  const texts = collectParagraphTexts(tree, []);
  const advisory = texts.find((t) => t.indexOf("Entitlement probe") !== -1);
  if (!advisory) {
    fail("SwitchOutcomeBanner did not render an advisory line for a throttled probe on a successful switch");
  }
  if (advisory.indexOf("throttled") === -1) {
    fail(`probe advisory line does not name the status "throttled": ${JSON.stringify(advisory)}`);
  }
  if (advisory.indexOf(probe.provider_message) === -1) {
    fail(`probe advisory line does not carry provider_message verbatim: ${JSON.stringify(advisory)}`);
  }
}

// A clean probe (`status: "callable"`) must stay silent — the advisory is for
// operator doubt, not for confirming the ordinary case.
{
  const tree = SwitchOutcomeBanner({
    outcome: Object.assign({}, baseOutcome, {
      probe: { status: "callable", http_status: 200, provider_message: "", reason: "ok" },
    }),
    ...bannerHandlers,
  });
  const texts = collectParagraphTexts(tree, []);
  if (texts.some((t) => t.indexOf("Entitlement probe") !== -1)) {
    fail('SwitchOutcomeBanner rendered a probe advisory line for probe.status "callable"');
  }
}

console.log(
  `BUNDLE SMOKE OK — tab "${registered.tabs[0].plugin}", slot "${registered.slots[0].slot}", ` +
    `${rendered.size} components rendered: ${[...rendered].sort().join(", ")}`
);
