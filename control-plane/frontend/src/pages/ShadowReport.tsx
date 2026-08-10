import { useQuery } from "@tanstack/react-query";
import { EyeOff, ShieldAlert, ShieldCheck, RefreshCw } from "lucide-react";
import { fetchAPI } from "../lib/api";

interface OffendingAction {
  action: string;
  count: number;
  max_score: number;
  reasons: string[];
}

interface ShadowReportData {
  total_shadow_calls: number;
  would_block_count: number;
  would_allow_count: number;
  block_rate: number;
  offending_actions: OffendingAction[];
}

async function fetchReport(): Promise<ShadowReportData> {
  return fetchAPI<ShadowReportData>("/api/traces/shadow-report");
}

function Stat({ label, value, tone }: { label: string; value: string; tone: string }) {
  return (
    <div className="card p-5">
      <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">{label}</p>
      <p className={`mt-2 text-3xl font-bold ${tone}`}>{value}</p>
    </div>
  );
}

export function ShadowReport() {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["shadow-report"],
    queryFn: fetchReport,
    refetchInterval: 5000,
  });

  const d = data ?? {
    total_shadow_calls: 0, would_block_count: 0, would_allow_count: 0,
    block_rate: 0, offending_actions: [],
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
            <EyeOff className="h-6 w-6 text-amber-500" /> Shadow Report
          </h1>
          <p className="mt-1 text-sm text-stone-500">
            What enforce mode <span className="font-medium">would</span> have blocked — observed without blocking anything.
          </p>
        </div>
        <button onClick={() => refetch()} className="btn-secondary" disabled={isFetching}>
          <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} /> Refresh
        </button>
      </div>

      <div className="grid grid-cols-4 gap-4">
        <Stat label="Shadow Calls" value={String(d.total_shadow_calls)} tone="text-stone-900" />
        <Stat label="Would Block" value={String(d.would_block_count)} tone="text-rose-600" />
        <Stat label="Would Allow" value={String(d.would_allow_count)} tone="text-emerald-600" />
        <Stat label="Block Rate" value={`${Math.round(d.block_rate * 100)}%`} tone="text-amber-600" />
      </div>

      <div className="card overflow-hidden">
        <div className="border-b border-stone-100 px-6 py-4">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-stone-800">
            <ShieldAlert className="h-4 w-4 text-rose-500" /> Actions that would be blocked
          </h2>
        </div>
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5">Action</th>
              <th className="px-6 py-3.5">Count</th>
              <th className="px-6 py-3.5">Max Risk</th>
              <th className="px-6 py-3.5">Reasons</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {d.offending_actions.map((a) => (
              <tr key={a.action} className="transition hover:bg-stone-50/50">
                <td className="px-6 py-4 font-mono text-sm text-stone-800">{a.action}</td>
                <td className="px-6 py-4 text-sm text-stone-600">{a.count}</td>
                <td className="px-6 py-4">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                    a.max_score >= 70 ? "bg-rose-50 text-rose-700"
                    : a.max_score >= 40 ? "bg-amber-50 text-amber-700"
                    : "bg-stone-100 text-stone-600"
                  }`}>{a.max_score}</span>
                </td>
                <td className="px-6 py-4 text-sm text-stone-500">{a.reasons.join(", ") || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!isLoading && d.offending_actions.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-12 text-center">
            <ShieldCheck className="h-8 w-8 text-emerald-400" />
            <p className="text-sm text-stone-500">
              No would-block events yet. Put a gateway in <span className="font-medium">shadow</span> mode
              and route traffic to see what enforcement would catch.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
