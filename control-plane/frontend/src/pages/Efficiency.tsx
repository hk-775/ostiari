import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle } from "lucide-react";
import { fetchAPI } from "../lib/api";
import { analyzeEfficiency, CostSummary } from "../lib/efficiency";

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

  const {
    hasUsage,
    avgTokensPerReq,
    costPerReq,
    tokenEfficiency,
    costEfficiency,
    routingEfficiency,
    overallScore,
    insights,
  } = analyzeEfficiency(data);

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
            <span className="text-2xl font-bold">
              {hasUsage ? overallScore : "—"}
            </span>
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
        {hasUsage ? (
          <>
            <ScoreBar label="Token Efficiency" score={Math.round(tokenEfficiency)} color={tokenEfficiency >= 70 ? "bg-emerald-500" : tokenEfficiency >= 40 ? "bg-amber-500" : "bg-red-500"} />
            <ScoreBar label="Cost Efficiency" score={Math.round(costEfficiency)} color={costEfficiency >= 70 ? "bg-emerald-500" : costEfficiency >= 40 ? "bg-amber-500" : "bg-red-500"} />
            <ScoreBar label="Routing Diversity" score={routingEfficiency} color={routingEfficiency >= 70 ? "bg-emerald-500" : routingEfficiency >= 40 ? "bg-amber-500" : "bg-red-500"} />
          </>
        ) : (
          <p className="text-sm text-stone-500">
            Efficiency scores will appear after the first governed request is reported.
          </p>
        )}
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
