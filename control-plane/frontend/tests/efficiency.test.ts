import assert from "node:assert/strict";
import test from "node:test";
import {
  analyzeEfficiency,
  CostSummary,
} from "../src/lib/efficiency";

function summary(overrides: Partial<CostSummary> = {}): CostSummary {
  return {
    total_cost_usd: 0,
    total_tokens: 0,
    total_requests: 0,
    by_model: [],
    by_agent: [],
    ...overrides,
  };
}

test("empty efficiency data remains finite and actionable", () => {
  const analysis = analyzeEfficiency(summary());

  assert.equal(analysis.hasUsage, false);
  assert.equal(analysis.avgTokensPerReq, 0);
  assert.equal(analysis.costPerReq, 0);
  assert.equal(analysis.tokenEfficiency, 0);
  assert.equal(analysis.costEfficiency, 0);
  assert.equal(analysis.routingEfficiency, 0);
  assert.equal(analysis.overallScore, 0);
  assert.equal(analysis.insights.length, 1);
  assert.match(analysis.insights[0].message, /No usage recorded yet/);
});

test("efficient multi-model traffic produces positive guidance", () => {
  const analysis = analyzeEfficiency(
    summary({
      total_cost_usd: 0.5,
      total_tokens: 100_000,
      total_requests: 100,
      by_model: [
        { model: "small", cost: 0.1, tokens: 60_000, requests: 70 },
        { model: "large", cost: 0.4, tokens: 40_000, requests: 30 },
      ],
      by_agent: [
        { agent_id: "agent-a", cost: 0.25, tokens: 50_000, requests: 50 },
        { agent_id: "agent-b", cost: 0.25, tokens: 50_000, requests: 50 },
      ],
    }),
  );

  assert.equal(analysis.hasUsage, true);
  assert.equal(analysis.avgTokensPerReq, 1000);
  assert.equal(analysis.costPerReq, 0.005);
  assert.equal(analysis.routingEfficiency, 85);
  assert.equal(analysis.insights.every((insight) => insight.type === "good"), true);
});

test("wasteful traffic identifies token, cost, and agent outliers", () => {
  const analysis = analyzeEfficiency(
    summary({
      total_cost_usd: 2,
      total_tokens: 250_000,
      total_requests: 100,
      by_model: [
        { model: "large", cost: 2, tokens: 250_000, requests: 100 },
      ],
      by_agent: [
        { agent_id: "normal", cost: 0.2, tokens: 20_000, requests: 80 },
        { agent_id: "expensive", cost: 1.8, tokens: 230_000, requests: 20 },
      ],
    }),
  );

  assert.equal(analysis.avgTokensPerReq, 2500);
  assert.equal(analysis.costPerReq, 0.02);
  assert.equal(analysis.tokenEfficiency, 0);
  assert.equal(analysis.costEfficiency, 0);
  assert.equal(analysis.insights.every((insight) => insight.type === "warning"), true);
  assert.match(
    analysis.insights.map((insight) => insight.message).join("\n"),
    /expensive/,
  );
});
