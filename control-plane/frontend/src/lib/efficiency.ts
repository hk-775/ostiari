export interface CostSummary {
  total_cost_usd: number;
  total_tokens: number;
  total_requests: number;
  by_model: { model: string; cost: number; tokens: number; requests: number }[];
  by_agent: { agent_id: string; cost: number; tokens: number; requests: number }[];
}

export interface EfficiencyInsight {
  type: "warning" | "good";
  message: string;
}

export interface EfficiencyAnalysis {
  hasUsage: boolean;
  avgTokensPerReq: number;
  costPerReq: number;
  tokenEfficiency: number;
  costEfficiency: number;
  routingEfficiency: number;
  overallScore: number;
  insights: EfficiencyInsight[];
}

export function analyzeEfficiency(data: CostSummary): EfficiencyAnalysis {
  if (data.total_requests <= 0) {
    return {
      hasUsage: false,
      avgTokensPerReq: 0,
      costPerReq: 0,
      tokenEfficiency: 0,
      costEfficiency: 0,
      routingEfficiency: 0,
      overallScore: 0,
      insights: [
        {
          type: "warning",
          message:
            "No usage recorded yet. Verify gateway reporting before evaluating efficiency.",
        },
      ],
    };
  }

  const avgTokensPerReq =
    Math.round(data.total_tokens / data.total_requests);
  const costPerReq = data.total_cost_usd / data.total_requests;

  const tokenEfficiency = Math.min(
    100,
    Math.max(0, 100 - (avgTokensPerReq - 500) / 20),
  );
  const costEfficiency = Math.min(
    100,
    Math.max(0, 100 - (costPerReq - 0.002) * 10000),
  );
  const routingEfficiency =
    data.by_model.length > 1 ? 85 : data.by_model.length === 1 ? 50 : 0;
  const overallScore = Math.round(
    (tokenEfficiency + costEfficiency + routingEfficiency) / 3,
  );
  const insights: EfficiencyInsight[] = [];

  if (avgTokensPerReq > 2000) {
    insights.push({
      type: "warning",
      message: `High avg tokens per request (${avgTokensPerReq}). Consider using shorter prompts or smaller context windows.`,
    });
  } else {
    insights.push({
      type: "good",
      message: `Token usage per request is efficient (avg ${avgTokensPerReq} tokens).`,
    });
  }

  if (data.by_model.length === 0) {
    insights.push({
      type: "warning",
      message:
        "No model usage recorded. Verify gateway reporting before evaluating routing efficiency.",
    });
  } else if (data.by_model.length === 1) {
    insights.push({
      type: "warning",
      message:
        "Only 1 model in use. Enable routing rules to send simple tasks to cheaper models.",
    });
  } else {
    insights.push({
      type: "good",
      message: `Using ${data.by_model.length} models — routing is distributing across cost tiers.`,
    });
  }

  const expensiveAgents = data.by_agent.filter(
    (agent) =>
      agent.requests > 5 && agent.cost / agent.requests > costPerReq * 2,
  );
  if (expensiveAgents.length > 0) {
    insights.push({
      type: "warning",
      message: `Agent(s) ${expensiveAgents.map((agent) => agent.agent_id).join(", ")} cost 2x+ the average. Review their prompts or route to cheaper models.`,
    });
  } else {
    insights.push({
      type: "good",
      message:
        "No agents significantly over-spending relative to the fleet average.",
    });
  }

  if (data.total_requests > 0 && costPerReq > 0.01) {
    insights.push({
      type: "warning",
      message: `Average cost per request ($${costPerReq.toFixed(4)}) is high. Consider using Haiku for simple tasks.`,
    });
  } else if (data.total_requests > 0) {
    insights.push({
      type: "good",
      message: `Average cost per request ($${costPerReq.toFixed(4)}) is within efficient range.`,
    });
  }

  return {
    hasUsage: true,
    avgTokensPerReq,
    costPerReq,
    tokenEfficiency,
    costEfficiency,
    routingEfficiency,
    overallScore,
    insights,
  };
}
