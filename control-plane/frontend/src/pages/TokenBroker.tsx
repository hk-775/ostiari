import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Coins, Save, RotateCcw, Info, TrendingDown } from "lucide-react";
import { api } from "../lib/api";

const usd = (n: number) => `$${n.toLocaleString(undefined, { maximumFractionDigits: n < 1 ? 4 : 2 })}`;

export function TokenBroker() {
  const qc = useQueryClient();
  const { data: report } = useQuery({
    queryKey: ["broker-report"],
    queryFn: () => api.tokenBroker.report(30),
    refetchInterval: 5000,
  });
  const { data: config } = useQuery({ queryKey: ["broker-config"], queryFn: api.tokenBroker.config });

  const [discount, setDiscount] = useState(0.25);
  const [markup, setMarkup] = useState(0.12);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (config) { setDiscount(config.bulk_discount); setMarkup(config.markup); }
  }, [config]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["broker-config"] });
    qc.invalidateQueries({ queryKey: ["broker-report"] });
  };
  const save = useMutation({
    mutationFn: () => api.tokenBroker.setConfig(discount, markup),
    onSuccess: () => { invalidate(); setSaved(true); setTimeout(() => setSaved(false), 2000); },
  });
  const reset = useMutation({ mutationFn: () => api.tokenBroker.resetConfig(), onSuccess: invalidate });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
          <Coins className="h-6 w-6 text-amber-500" /> Token Broker
        </h1>
        <p className="mt-1 text-sm text-stone-500">
          Route LLM traffic through a bulk-discounted token pool. The customer pays less than
          list; you keep the spread. Token counts are measured; discount and markup are your terms.
        </p>
      </div>

      {/* Hero: two numbers — customer savings and our margin */}
      <div className="grid grid-cols-2 gap-4">
        <div className="card bg-gradient-to-br from-sky-50 to-white p-8 text-center">
          <p className="text-xs font-semibold uppercase tracking-widest text-sky-600">Customer savings</p>
          <p className="mt-2 text-4xl font-bold tracking-tight text-stone-900">
            {usd(report?.customer_savings_usd ?? 0)}
          </p>
          <p className="mt-2 text-sm text-stone-500">
            {report?.savings_pct ?? 0}% below list · {usd(report?.total_retail_usd ?? 0)} retail →
            {" "}{usd(report?.total_charged_usd ?? 0)} charged
          </p>
        </div>
        <div className="card bg-gradient-to-br from-emerald-50 to-white p-8 text-center">
          <p className="text-xs font-semibold uppercase tracking-widest text-emerald-600">Our margin</p>
          <p className="mt-2 text-4xl font-bold tracking-tight text-stone-900">
            {usd(report?.margin_usd ?? 0)}
          </p>
          <p className="mt-2 text-sm text-stone-500">
            {usd(report?.total_charged_usd ?? 0)} charged − {usd(report?.total_our_cost_usd ?? 0)} bulk cost
            · {(report?.total_tokens ?? 0).toLocaleString()} tokens
          </p>
        </div>
      </div>

      {/* Terms — editable discount + markup */}
      <div className="card p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-stone-800">
            <TrendingDown className="h-4 w-4 text-amber-500" /> Broker terms
            {config?.customized === false && <span className="text-xs font-normal text-stone-400">(defaults)</span>}
          </h2>
          <div className="flex items-center gap-2">
            <button onClick={() => reset.mutate()} disabled={reset.isPending} className="btn-secondary">
              <RotateCcw className="h-4 w-4" /> Reset
            </button>
            <button onClick={() => save.mutate()} disabled={save.isPending} className="btn-sky">
              <Save className="h-4 w-4" /> {saved ? "Saved" : "Save"}
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-6">
          <label className="text-sm text-stone-700">
            <div className="flex justify-between"><span>Bulk discount</span><span className="font-mono">{Math.round(discount * 100)}%</span></div>
            <input type="range" min={0} max={0.9} step={0.01} value={discount}
              onChange={(e) => setDiscount(Number(e.target.value))} className="mt-2 w-full" />
            <p className="mt-1 text-xs text-stone-400">Off retail via volume agreements — what we pay the provider.</p>
          </label>
          <label className="text-sm text-stone-700">
            <div className="flex justify-between"><span>Markup</span><span className="font-mono">{Math.round(markup * 100)}%</span></div>
            <input type="range" min={0} max={1} step={0.01} value={markup}
              onChange={(e) => setMarkup(Number(e.target.value))} className="mt-2 w-full" />
            <p className="mt-1 text-xs text-stone-400">Over our bulk cost — what we add. Keep discount×markup &lt; 1 so the customer still saves.</p>
          </label>
        </div>
        <p className="mt-4 flex items-center gap-1 text-xs text-stone-500">
          <Info className="h-3 w-3" /> Effective customer price ≈ {Math.round((1 - discount) * (1 + markup) * 100)}% of list.
          {(1 - discount) * (1 + markup) >= 1 && (
            <span className="ml-1 font-medium text-rose-600">Markup exceeds discount — customer would pay more than retail.</span>
          )}
        </p>
      </div>

      {/* Per-model breakdown */}
      <div className="card overflow-hidden">
        <div className="border-b border-stone-100 px-6 py-4">
          <h2 className="text-sm font-semibold text-stone-800">By model</h2>
        </div>
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5">Model</th>
              <th className="px-6 py-3.5">Calls</th>
              <th className="px-6 py-3.5">Tokens</th>
              <th className="px-6 py-3.5">Retail</th>
              <th className="px-6 py-3.5">Charged</th>
              <th className="px-6 py-3.5">Savings</th>
              <th className="px-6 py-3.5 text-right">Margin</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {(report?.models ?? []).map((m) => (
              <tr key={m.model} className="transition hover:bg-stone-50/50">
                <td className="px-6 py-4 font-mono text-sm text-stone-800">{m.model}</td>
                <td className="px-6 py-4 text-sm text-stone-600">{m.calls}</td>
                <td className="px-6 py-4 text-sm text-stone-500">{m.tokens.toLocaleString()}</td>
                <td className="px-6 py-4 text-sm text-stone-500">{usd(m.retail_usd)}</td>
                <td className="px-6 py-4 text-sm text-stone-700">{usd(m.charged_usd)}</td>
                <td className="px-6 py-4 text-sm text-sky-700">{usd(m.customer_savings_usd)}</td>
                <td className="px-6 py-4 text-right font-semibold text-emerald-700">{usd(m.margin_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(report?.models?.length ?? 0) === 0 && (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <Coins className="h-7 w-7 text-stone-300" />
            <p className="text-sm text-stone-500">No usage in this period to broker.</p>
          </div>
        )}
      </div>
    </div>
  );
}
