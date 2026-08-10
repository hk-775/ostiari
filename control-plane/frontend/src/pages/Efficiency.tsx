import { useQuery } from "@tanstack/react-query";
import { Gauge, AlertTriangle, CheckCircle, TrendingDown } from "lucide-react";
import { fetchAPI } from "../lib/api";

interface CostSummary {
  total_cost_usd: number;
  total_tokens: number;
  total_requests: number;
  by_model: { model: string; cost: number; tokens: number; requests: number }[];
  by_agent: { agent_id: string; cost: number; tokens: number; requests: number }[];
}

async function fetchData(): Promise<CostSummary> {
  return fetchAPI<CostSummary>("/api/costs/summary?period_days=7");
}

function ScoreBar({ label, score, color }: { label: string; score: number; color: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-40 text-sm text-stone-500">{label}</span>
      <div className="flex-1 h-5 bg-stone-100 rounded-full relative">
        <div className={`h-5 rounded-full ${color}`} style={{ width: `${score}%` }} />
        <span className="absolute inset-0 flex items-center justify-center text-xs text-white font-medium">{score}%</span>
      </div>
    </div>
  );
}

export function Efficiency() {
  const { data } = useQuery({ queryKey: ["efficiency"], queryFn: fetchData });

  if (!data) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Efficiency</h1>
          <p className="mt-1 text-sm text-stone-500">Loading...</p>
        </div>
      </div>
    );
  }

  const avgTokensPerReq = data.total_requests > 0 ? Math.round(data.total_tokens / data.total_requests) : 0;
  const costPerReq = data.total_requests > 0 ? data.total_cost_usd / data.total_requests : 0;

  // Efficiency scoring (heuristic)
  const tokenEfficiency = Math.min(100, Math.max(0, 100 - (avgTokensPerReq - 500) / 20));
  const costEfficiency = Math.min(100, Math.max(0, 100 - (costPerReq - 0.002) * 10000));
  const routingEfficiency = data.by_model.length > 1 ? 85 : 50; // multiple models = better routing
  const overallScore = Math.round((tokenEfficiency + costEfficiency + routingEfficiency) / 3);

  // Identify potential waste
  const insights: { type: "warning" | "good"; message: string }[] = [];

  if (avgTokensPerReq > 2000) {
    insights.push({ type: "warning", message: `High avg tokens per request (${avgTokensPerReq}). Consider using shorter prompts or smaller context windows.` });
  } else {
    insights.push({ type: "good", message: `Token usage per request is efficient (avg ${avgTokensPerReq} tokens).` });
  }

  if (data.by_model.length === 1) {
    insights.push({ type: "warning", message: `Only 1 model in use. Enable routing rules to send simple tasks to cheaper models.` });
  } else {
    insights.push({ type: "good", message: `Using ${data.by_model.length} models — routing is distributing across cost tiers.` });
  }

  const expensiveAgents = data.by_agent.filter(a => a.requests > 5 && (a.cost / a.requests) > costPerReq * 2);
  if (expensiveAgents.length > 0) {
    insights.push({ type: "warning", message: `Agent(s) ${expensiveAgents.map(a => a.agent_id).join(", ")} cost 2x+ the average. Review their prompts or route to cheaper models.` });
  } else {
    insights.push({ type: "good", message: "No agents significantly over-spending relative to the fleet average." });
  }

  if (data.total_requests > 0 && costPerReq > 0.01) {
    insights.push({ type: "warning", message: `Average cost per request ($${costPerReq.toFixed(4)}) is high. Consider using Haiku for simple tasks.` });
  } else if (data.total_requests > 0) {
    insights.push({ type: "good", message: `Average cost per request ($${costPerReq.toFixed(4)}) is within efficient range.` });
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Efficiency</h1>
        <p className="mt-1 text-sm text-stone-500">Token usage, cost optimization, and prompt quality insights</p>
      </div>

      {/* Overall Score */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-4">
        <div className="stat-card text-center">
          <div className={`inline-flex h-16 w-16 items-center justify-center rounded-full ${overallScore >= 70 ? "bg-emerald-50 text-emerald-600" : overallScore >= 40 ? "bg-amber-50 text-amber-600" : "bg-rose-50 text-rose-600"}`}>
            <span className="text-2xl font-bold">{overallScore}</span>
          </div>
          <p className="mt-2 text-xs text-stone-500">Overall Score</p>
        </div>
        <div className="stat-card">
          <p className="text-xs text-stone-500">Avg Tokens/Request</p>
          <p className="text-xl font-semibold tracking-tight text-stone-900 mt-1">{avgTokensPerReq.toLocaleString()}</p>
          <p className="text-xs text-stone-400 mt-1">{data.total_requests} total requests</p>
        </div>
        <div className="stat-card">
          <p className="text-xs text-stone-500">Cost/Request</p>
          <p className="text-xl font-semibold tracking-tight text-stone-900 mt-1">${costPerReq.toFixed(4)}</p>
          <p className="text-xs text-stone-400 mt-1">last 7 days</p>
        </div>
        <div className="stat-card">
          <p className="text-xs text-stone-500">Models Used</p>
          <p className="text-xl font-semibold tracking-tight text-stone-900 mt-1">{data.by_model.length}</p>
          <p className="text-xs text-stone-400 mt-1">{data.by_model.length > 1 ? "good diversity" : "consider adding"}</p>
        </div>
      </div>

      {/* Efficiency Scores */}
      <div className="card p-6 space-y-3">
        <h3 className="text-sm font-medium text-stone-700">Efficiency Breakdown</h3>
        <ScoreBar label="Token Efficiency" score={Math.round(tokenEfficiency)} color={tokenEfficiency >= 70 ? "bg-emerald-500" : tokenEfficiency >= 40 ? "bg-amber-500" : "bg-red-500"} />
        <ScoreBar label="Cost Efficiency" score={Math.round(costEfficiency)} color={costEfficiency >= 70 ? "bg-emerald-500" : costEfficiency >= 40 ? "bg-amber-500" : "bg-red-500"} />
        <ScoreBar label="Routing Diversity" score={routingEfficiency} color={routingEfficiency >= 70 ? "bg-emerald-500" : routingEfficiency >= 40 ? "bg-amber-500" : "bg-red-500"} />
      </div>

      {/* Insights */}
      <div className="card p-6">
        <h3 className="text-sm font-medium text-stone-700 mb-3">Optimization Insights</h3>
        <div className="space-y-2">
          {insights.map((insight, i) => (
            <div key={i} className={`flex items-start gap-3 rounded-xl p-3 ${insight.type === "warning" ? "bg-amber-50 border border-amber-200" : "bg-emerald-50 border border-emerald-200"}`}>
              {insight.type === "warning" ? (
                <AlertTriangle className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
              ) : (
                <CheckCircle className="h-4 w-4 text-emerald-600 mt-0.5 shrink-0" />
              )}
              <p className="text-sm text-stone-700">{insight.message}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Per-Agent Efficiency */}
      {data.by_agent.length > 0 && (
        <div className="card p-6">
          <h3 className="text-sm font-medium text-stone-700 mb-3">Per-Agent Token Efficiency</h3>
          <div className="space-y-2">
            {data.by_agent.map(a => {
              const avgTok = a.requests > 0 ? Math.round(a.tokens / a.requests) : 0;
              const maxAvg = Math.max(...data.by_agent.map(x => x.requests > 0 ? x.tokens / x.requests : 0), 1);
              return (
                <div key={a.agent_id} className="flex items-center gap-3">
                  <span className="w-28 text-xs text-stone-500 truncate">{a.agent_id}</span>
                  <div className="flex-1 h-4 bg-stone-100 rounded">
                    <div className="h-4 rounded bg-amber-500" style={{ width: `${(avgTok / maxAvg) * 100}%` }} />
                  </div>
                  <span className="w-24 text-right text-xs text-stone-700">{avgTok} tok/req</span>
                  <span className="w-16 text-right text-xs text-stone-400">{a.requests} req</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
