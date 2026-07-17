import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Gauge, Download } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";

interface Row {
  key: string;
  calls: number;
  tokens: number;
  tier: string;
  next_tier: { tier: string; calls_to_next: number } | null;
}

interface Summary {
  group_by: string;
  total_governed_calls: number;
  total_tokens: number;
  distinct_subjects: number;
  overall_tier: string;
  period_days: number;
  breakdown: Row[];
  tiers: { tier: string; min_calls: number }[];
}

async function fetchSummary(groupBy: string, period: number): Promise<Summary> {
  const res = await fetch(`${API_BASE}/api/metering/summary?group_by=${groupBy}&period_days=${period}`);
  return res.json();
}

const TIER_BADGE: Record<string, string> = {
  free: "bg-stone-100 text-stone-600",
  pro: "bg-sky-50 text-sky-700",
  enterprise: "bg-violet-50 text-violet-700",
};

export function Metering() {
  const [groupBy, setGroupBy] = useState("agent");
  const [period, setPeriod] = useState(30);
  const { data } = useQuery({
    queryKey: ["metering", groupBy, period],
    queryFn: () => fetchSummary(groupBy, period),
  });

  const d = data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
            <Gauge className="h-6 w-6 text-emerald-500" /> Metering
          </h1>
          <p className="mt-1 text-sm text-stone-500">Governed tool calls per {groupBy}. You only pay for what you govern.</p>
        </div>
        <div className="flex items-center gap-2">
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value)} className="input">
            <option value="agent">by Agent</option>
            <option value="gateway">by Gateway</option>
            <option value="tool">by Tool</option>
          </select>
          <select value={period} onChange={(e) => setPeriod(Number(e.target.value))} className="input">
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
          <a href={`${API_BASE}/api/metering/export?group_by=${groupBy}&period_days=${period}`}
             className="btn-secondary" title="Export CSV">
            <Download className="h-4 w-4" /> CSV
          </a>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-4">
        <div className="card p-5"><p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Governed calls</p><p className="mt-2 text-3xl font-bold text-stone-900">{(d?.total_governed_calls ?? 0).toLocaleString()}</p></div>
        <div className="card p-5"><p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Tokens</p><p className="mt-2 text-3xl font-bold text-stone-900">{(d?.total_tokens ?? 0).toLocaleString()}</p></div>
        <div className="card p-5"><p className="text-xs font-semibold uppercase tracking-wider text-stone-500">{groupBy}s</p><p className="mt-2 text-3xl font-bold text-stone-900">{d?.distinct_subjects ?? 0}</p></div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Overall tier</p>
          <p className="mt-2"><span className={`rounded-full px-2.5 py-1 text-lg font-bold uppercase ${TIER_BADGE[d?.overall_tier ?? "free"]}`}>{d?.overall_tier ?? "free"}</span></p>
        </div>
      </div>

      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5 capitalize">{groupBy}</th>
              <th className="px-6 py-3.5">Governed calls</th>
              <th className="px-6 py-3.5">Tokens</th>
              <th className="px-6 py-3.5">Tier</th>
              <th className="px-6 py-3.5">To next tier</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {(d?.breakdown ?? []).map((r) => (
              <tr key={r.key} className="transition hover:bg-stone-50/50">
                <td className="px-6 py-4 text-sm font-medium text-stone-800">{r.key}</td>
                <td className="px-6 py-4 text-sm text-stone-600">{r.calls.toLocaleString()}</td>
                <td className="px-6 py-4 text-sm text-stone-500">{r.tokens.toLocaleString()}</td>
                <td className="px-6 py-4"><span className={`rounded-full px-2 py-0.5 text-xs font-medium ${TIER_BADGE[r.tier]}`}>{r.tier}</span></td>
                <td className="px-6 py-4 text-xs text-stone-500">
                  {r.next_tier ? `${r.next_tier.calls_to_next.toLocaleString()} → ${r.next_tier.tier}` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {(d?.breakdown.length ?? 0) === 0 && (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <Gauge className="h-7 w-7 text-stone-300" />
            <p className="text-sm text-stone-500">No governed activity in this period.</p>
          </div>
        )}
      </div>
    </div>
  );
}
