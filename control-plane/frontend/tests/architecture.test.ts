import assert from "node:assert/strict";
import test from "node:test";
import {
  DEPLOYMENT_VIEWS,
  GATE_CHAIN,
  PROFILE_NAMES,
  RUNTIME_SCENARIOS,
} from "../src/lib/architecture";

test("architecture exposes the complete deployment profile matrix", () => {
  const documented = new Set(DEPLOYMENT_VIEWS.flatMap((view) => view.profiles));
  for (const profile of PROFILE_NAMES) {
    assert.equal(documented.has(profile), true, `${profile} is missing from deployment views`);
  }
});

test("runtime scenarios have unique ids and executable step sequences", () => {
  assert.equal(
    new Set(RUNTIME_SCENARIOS.map((scenario) => scenario.id)).size,
    RUNTIME_SCENARIOS.length,
  );
  for (const scenario of RUNTIME_SCENARIOS) {
    assert.ok(scenario.steps.length >= 6, `${scenario.id} is too shallow`);
    assert.equal(scenario.steps[0].lane, "client");
    assert.equal(scenario.steps.at(-1)?.lane, "control");
  }
});

test("the canonical gate chain includes approval between risk and payment", () => {
  assert.deepEqual(
    GATE_CHAIN.map((gate) => gate.label),
    [
      "Delegation",
      "Authorization",
      "Quota",
      "Risk",
      "Approval",
      "Payment",
      "Execute",
      "Trace",
    ],
  );
});
