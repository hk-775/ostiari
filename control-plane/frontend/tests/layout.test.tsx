import assert from "node:assert/strict";
import test from "node:test";
import { installBrowserEnvironment } from "./browserEnv";

installBrowserEnvironment();
const { PROTECTED_ROUTE_PATHS } = await import("../src/App");
const { allNavigationPaths, visibleNavigationSections } = await import(
  "../src/components/Layout"
);

function navigationLabels(role: "admin" | "operator" | "viewer"): Set<string> {
  return new Set(
    visibleNavigationSections(role).flatMap((section) => [
      section.label,
      ...section.items.map((item) => item.label),
    ]),
  );
}

test("viewer navigation is read-only", () => {
  const labels = navigationLabels("viewer");
  assert.equal(labels.has("Dashboard"), true);
  assert.equal(labels.has("Live Traces"), true);
  assert.equal(labels.has("Payments (x402)"), true);
  assert.equal(labels.has("Policies (per tool)"), false);
  assert.equal(labels.has("Agent Gateways"), false);
  assert.equal(labels.has("Sandbox"), false);
  assert.equal(labels.has("LLM Providers"), false);
});

test("operator navigation exposes write workflows but not admin settings", () => {
  const labels = navigationLabels("operator");
  assert.equal(labels.has("Policies (per tool)"), true);
  assert.equal(labels.has("Agent Gateways"), true);
  assert.equal(labels.has("Sandbox"), true);
  assert.equal(labels.has("LLM Providers"), false);
  assert.equal(labels.has("Users"), false);
});

test("admin navigation exposes every section", () => {
  const labels = navigationLabels("admin");
  assert.equal(labels.has("Policies (per tool)"), true);
  assert.equal(labels.has("Agent Gateways"), true);
  assert.equal(labels.has("Sandbox"), true);
  assert.equal(labels.has("LLM Providers"), true);
  assert.equal(labels.has("Users"), true);
});

test("every protected page is discoverable from navigation", () => {
  assert.deepEqual(
    new Set(allNavigationPaths()),
    new Set(PROTECTED_ROUTE_PATHS),
  );
});

test("efficiency insights are visible to read-only operators", () => {
  assert.equal(navigationLabels("viewer").has("Efficiency"), true);
});
