import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { TrendingUp, ShieldAlert, Save, RotateCcw, Info } from "lucide-react";
import { api } from "../lib/api";

const usd = (n: number) =>
  n >= 1000 ? `$${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K` : `$${n.toFixed(0)}`;
const usdFull = (n: number) => `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

export function Roi() {
  const qc = useQueryClient();
  const [weight, setWeight] = useState(true);
  const { data: report } = useQuery({
    queryKey: ["roi-report", weight],
    queryFn: () => api.roi.report(weight),
    refetchInterval: 5000,
  });
  const { data: model } = useQuery({ queryKey: ["roi-cost-model"], queryFn: api.roi.costModel });

  // Local editable copy of the cost model.
  const [entries, setEntries] = useState<{ pattern: string; cost: number }[]>([]);
  const [fallback, setFallback] = useState(10000);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (model) {
      setEntries(model.entries);
      setFallback(model.fallback);
    }
  }, [model]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["roi-cost-model"] });
    qc.invalidateQueries({ queryKey: ["roi-report"] });
  };
  const save = useMutation({
    mutationFn: () => api.roi.setCostModel(entries, fallback),
    onSuccess: () => { invalidate(); setSaved(true); setTimeout(() => setSaved(false), 2000); },
  });
  const reset = useMutation({ mutationFn: () => api.roi.resetCostModel(), onSuccess: invalidate });

  const setCost = (i: number, cost: number) =>
    setEntries((prev) => prev.map((e, idx) => (idx === i ? { ...e, cost } : e)));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
          <TrendingUp className="h-6 w-6 text-emerald-500" /> ROI / Savings
        </h1>
        <p className="mt-1 text-sm text-stone-500">
          Estimated damage prevented by blocked actions. Counts and risk scores are measured;
          the dollar values are <span className="font-medium">your cost assumptions</span> — edit them below.
        </p>
      </div>

      {/* Hero */}
      <div className="card bg-gradient-to-br from-emerald-50 to-white p-8 text-center">
        <p className="text-xs font-semibold uppercase tracking-widest text-emerald-600">Estimated damage prevented</p>
        <p className="mt-2 text-5xl font-bold tracking-tight text-stone-900">
          {usdFull(report?.total_prevented_usd ?? 0)}
        </p>
        <p className="mt-3 text-sm text-stone-500">
          <span className="font-semibold text-stone-700">{report?.blocked_count ?? 0}</span> unsafe actions blocked
          across <span className="font-semibold text-stone-700">{report?.distinct_actions ?? 0}</span> action types
        </p>
        <label className="mt-4 inline-flex items-center gap-2 text-xs text-stone-500">
          <input type="checkbox" checked={weight} onChange={(e) => setWeight(e.target.checked)} />
          Risk-weight by score (a barely-over-threshold block counts less than a max-risk one)
        </label>
      </div>

      {/* Per-action breakdown */}
      <div className="card overflow-hidden">
        <div className="flex items-center justify-between border-b border-stone-100 px-6 py-4">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-stone-800">
            <ShieldAlert className="h-4 w-4 text-rose-500" /> Prevented by action type
          </h2>
        </div>
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5">Action</th>
              <th className="px-6 py-3.5">Blocked</th>
              <th className="px-6 py-3.5">Unit cost</th>
              <th className="px-6 py-3.5">Max risk</th>
              <th className="px-6 py-3.5 text-right">Prevented</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {(report?.actions ?? []).map((a) => (
              <tr key={a.action} className="transition hover:bg-stone-50/50">
                <td className="px-6 py-4 font-mono text-sm text-stone-800">{a.action}</td>
                <td className="px-6 py-4 text-sm text-stone-600">{a.count}</td>
                <td className="px-6 py-4 text-sm text-stone-500">{usd(a.unit_cost)}</td>
                <td className="px-6 py-4 text-sm text-stone-500">{a.max_score}</td>
                <td className="px-6 py-4 text-right font-semibold text-emerald-700">{usdFull(a.prevented_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(report?.actions?.length ?? 0) === 0 && (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <ShieldAlert className="h-7 w-7 text-stone-300" />
            <p className="text-sm text-stone-500">No blocked actions yet — nothing to price.</p>
          </div>
        )}
      </div>

      {/* Editable cost model */}
      <div className="card overflow-hidden">
        <div className="flex items-center justify-between border-b border-stone-100 px-6 py-4">
          <div>
            <h2 className="text-sm font-semibold text-stone-800">Cost assumptions</h2>
            <p className="mt-0.5 flex items-center gap-1 text-xs text-stone-500">
              <Info className="h-3 w-3" /> Estimated incident cost per blocked action type. Patterns match top-down.
              {model?.customized === false && " (defaults)"}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => reset.mutate()} disabled={reset.isPending} className="btn-secondary">
              <RotateCcw className="h-4 w-4" /> Reset
            </button>
            <button onClick={() => save.mutate()} disabled={save.isPending} className="btn-sky">
              <Save className="h-4 w-4" /> {saved ? "Saved" : "Save"}
            </button>
          </div>
        </div>
        <table className="w-full">
          <tbody className="divide-y divide-stone-50">
            {entries.map((e, i) => (
              <tr key={e.pattern}>
                <td className="px-6 py-3 font-mono text-sm text-stone-700">{e.pattern}</td>
                <td className="px-6 py-3 text-right">
                  <span className="text-stone-400">$</span>
                  <input
                    type="number" min={0} value={e.cost}
                    onChange={(ev) => setCost(i, Number(ev.target.value))}
                    className="ml-1 w-32 rounded border border-stone-200 px-2 py-1 text-right text-sm"
                  />
                </td>
              </tr>
            ))}
            <tr className="bg-stone-50/50">
              <td className="px-6 py-3 text-sm font-medium text-stone-600">fallback (unmatched)</td>
              <td className="px-6 py-3 text-right">
                <span className="text-stone-400">$</span>
                <input
                  type="number" min={0} value={fallback}
                  onChange={(ev) => setFallback(Number(ev.target.value))}
                  className="ml-1 w-32 rounded border border-stone-200 px-2 py-1 text-right text-sm"
                />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
