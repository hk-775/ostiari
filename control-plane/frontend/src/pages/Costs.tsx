import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { DollarSign, TrendingUp, Zap, BarChart3 } from "lucide-react";
import { fetchAPI } from "../lib/api";

interface CostSummary {
  total_cost_usd: number;
  total_tokens: number;
  total_requests: number;
  by_model: { model: string; cost: number; tokens: number; requests: number }[];
  by_gateway: { gateway_id: string; cost: number; tokens: number; requests: number }[];
  by_agent: { agent_id: string; cost: number; tokens: number; requests: number }[];
  daily_costs: { date: string; cost: number; tokens: number; requests: number }[];
}

async function fetchSummary(days: number): Promise<CostSummary> {
  return fetchAPI<CostSummary>(`/api/costs/summary?period_days=${days}`);
}

function StatCard({ label, value, sub, icon: Icon }: { label: string; value: string; sub: string; icon: any }) {
  return (
    <div className="stat-card">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-50">
          <Icon className="h-4 w-4 text-sky-600" />
        </div>
        <div>
          <p className="text-2xl font-semibold tracking-tight text-stone-900">{value}</p>
          <p className="text-sm text-stone-500">{label}</p>
          <p className="text-xs text-stone-400">{sub}</p>
        </div>
      </div>
    </div>
  );
}

function BarRow({ label, value, maxValue, color }: { label: string; value: number; maxValue: number; color: string }) {
  const pct = maxValue > 0 ? (value / maxValue) * 100 : 0;
  return (
    <div className="flex items-center gap-3 py-1.5">
      <span className="w-36 truncate text-sm text-stone-700" title={label}>{label}</span>
      <div className="flex-1">
        <div className="h-4 w-full rounded-full bg-stone-100">
          <div className={`h-4 rounded-full ${color}`} style={{ width: `${Math.max(pct, 2)}%` }} />
        </div>
      </div>
      <span className="w-20 text-right text-sm text-stone-500">${value.toFixed(4)}</span>
    </div>
  );
}

export function Costs() {
  const [days, setDays] = useState(7);
  const { data: summary } = useQuery({
    queryKey: ["costs", days],
    queryFn: () => fetchSummary(days),
  });

  if (!summary) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Cost Dashboard</h1>
          <p className="mt-1 text-sm text-stone-500">Loading...</p>
        </div>
      </div>
    );
  }

  const maxModelCost = Math.max(...summary.by_model.map(m => m.cost), 0.001);
  const maxGatewayCost = Math.max(...summary.by_gateway.map(s => s.cost), 0.001);
  const maxAgentCost = Math.max(...summary.by_agent.map(a => a.cost), 0.001);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Cost Dashboard</h1>
          <p className="mt-1 text-sm text-stone-500">LLM spend across your gateway fleet</p>
        </div>
        <div className="flex gap-1">
          {[1, 7, 14, 30].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={`rounded-xl px-3 py-2 text-sm transition ${days === d ? "bg-orange-600 text-white font-medium shadow-sm" : "border border-stone-200 text-stone-500 hover:bg-orange-50 hover:border-orange-200 hover:text-orange-700"}`}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard label="Total Spend" value={`$${summary.total_cost_usd.toFixed(2)}`} sub={`last ${days} days`} icon={DollarSign} />
        <StatCard label="Total Tokens" value={summary.total_tokens.toLocaleString()} sub={`${summary.total_requests} requests`} icon={Zap} />
        <StatCard label="Avg Cost/Request" value={`$${summary.total_requests > 0 ? (summary.total_cost_usd / summary.total_requests).toFixed(4) : "0"}`} sub="per LLM call" icon={TrendingUp} />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* By Model */}
        <div className="card p-6">
          <h3 className="mb-4 text-sm font-medium text-stone-700">Spend by Model</h3>
          {summary.by_model.length === 0 && <p className="text-sm text-stone-500">No data</p>}
          {summary.by_model.map(m => (
            <BarRow key={m.model} label={m.model} value={m.cost} maxValue={maxModelCost} color="bg-amber-500" />
          ))}
        </div>

        {/* By Gateway */}
        <div className="card p-6">
          <h3 className="mb-4 text-sm font-medium text-stone-700">Spend by Gateway</h3>
          {summary.by_gateway.length === 0 && <p className="text-sm text-stone-500">No data</p>}
          {summary.by_gateway.map(s => (
            <BarRow key={s.gateway_id} label={s.gateway_id.replace("-agent","").split("-").map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ").replace("Devops","DevOps").replace("Crm","CRM") + " Gateway"} value={s.cost} maxValue={maxGatewayCost} color="bg-cyan-500" />
          ))}
        </div>

        {/* By Agent */}
        <div className="card p-6">
          <h3 className="mb-4 text-sm font-medium text-stone-700">Spend by Agent</h3>
          {summary.by_agent.length === 0 && <p className="text-sm text-stone-500">No data</p>}
          {summary.by_agent.map(a => (
            <BarRow key={a.agent_id} label={a.agent_id} value={a.cost} maxValue={maxAgentCost} color="bg-emerald-500" />
          ))}
        </div>

        {/* Daily Trend */}
        <div className="card p-6">
          <h3 className="mb-4 text-sm font-medium text-stone-700">Daily Spend</h3>
          {summary.daily_costs.length === 0 && <p className="text-sm text-stone-500">No data</p>}
          <div className="space-y-1">
            {summary.daily_costs.map(d => {
              const maxDaily = Math.max(...summary.daily_costs.map(x => x.cost), 0.001);
              const pct = (d.cost / maxDaily) * 100;
              return (
                <div key={d.date} className="flex items-center gap-3">
                  <span className="w-20 text-xs text-stone-500">{d.date.slice(5)}</span>
                  <div className="flex-1">
                    <div className="h-3 w-full rounded-full bg-stone-100">
                      <div className="h-3 rounded-full bg-violet-500" style={{ width: `${Math.max(pct, 2)}%` }} />
                    </div>
                  </div>
                  <span className="w-16 text-right text-xs text-stone-500">${d.cost.toFixed(3)}</span>
                  <span className="w-12 text-right text-xs text-stone-400">{d.requests}req</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {summary.total_requests === 0 && (
        <div className="flex flex-col items-center gap-3 py-12">
          <BarChart3 className="h-8 w-8 text-stone-300" />
          <p className="text-sm text-stone-500">No usage data yet. Cost data is recorded when gateways with the LLM Gateway module process requests.</p>
          <p className="text-xs text-stone-400">Gateways report usage via POST /api/costs/record</p>
        </div>
      )}
    </div>
  );
}
