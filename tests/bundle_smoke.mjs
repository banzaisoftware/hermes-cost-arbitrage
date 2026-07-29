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

console.log(
  `BUNDLE SMOKE OK — tab "${registered.tabs[0].plugin}", slot "${registered.slots[0].slot}", ` +
    `${rendered.size} components rendered: ${[...rendered].sort().join(", ")}`
);
