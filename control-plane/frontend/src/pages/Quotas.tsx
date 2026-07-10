import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, Plus, Trash2, AlertTriangle, Upload } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";

interface Quota {
  id: number;
  name: string;
  scope: string;
  scope_id: string;
  rate_limit_rpm: number | null;
  budget_limit_usd: number | null;
  max_tokens_per_request: number | null;
  allowed_models: string[];
  current_spend: number;
  current_rpm: number;
}

async function fetchQuotas(): Promise<Quota[]> {
  const res = await fetch(`${API_BASE}/api/quotas`);
  if (res.status === 404) return [];
  return res.json();
}

export function Quotas() {
  const queryClient = useQueryClient();
  const { data: quotas = [] } = useQuery({ queryKey: ["quotas"], queryFn: fetchQuotas });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", scope: "gateway", scope_id: "", rate_limit_rpm: "", budget_limit_usd: "", max_tokens_per_request: "", allowed_models: "" });

  const createMutation = useMutation({
    mutationFn: async (data: typeof form) => {
      const payload: any = { name: data.name, scope: data.scope, scope_id: data.scope_id };
      if (data.rate_limit_rpm) payload.rate_limit_rpm = parseInt(data.rate_limit_rpm);
      if (data.budget_limit_usd) payload.budget_limit_usd = parseFloat(data.budget_limit_usd);
      if (data.max_tokens_per_request) payload.max_tokens_per_request = parseInt(data.max_tokens_per_request);
      if (data.allowed_models) payload.allowed_models = data.allowed_models.split(",").map((s: string) => s.trim());
      const res = await fetch(`${API_BASE}/api/quotas`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      return res.json();
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["quotas"] }); setShowForm(false); },
  });

  const deleteMutation = useMutation({
    mutationFn: async (id: number) => { await fetch(`${API_BASE}/api/quotas/${id}`, { method: "DELETE" }); },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["quotas"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Quotas</h1>
          <p className="mt-1 text-sm text-stone-500">Rate limits, budget caps, and model restrictions per gateway/agent/project</p>
        </div>
        <button onClick={() => setShowForm(true)} className="btn-amber">
          <Plus className="h-4 w-4" /> Add Quota
        </button>
      </div>

      {showForm && (
        <form onSubmit={(e) => { e.preventDefault(); createMutation.mutate(form); }} className="card p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Quota name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" />
            <select value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })} className="input">
              <option value="gateway">Per Gateway</option>
              <option value="agent">Per Agent</option>
              <option value="project">Per Project</option>
              <option value="global">Global</option>
            </select>
            <input placeholder="Scope ID (gateway/agent/project name)" value={form.scope_id} onChange={(e) => setForm({ ...form, scope_id: e.target.value })} className="input" />
            <input placeholder="Rate limit (requests/min)" value={form.rate_limit_rpm} onChange={(e) => setForm({ ...form, rate_limit_rpm: e.target.value })} className="input" />
            <input placeholder="Budget limit (USD)" value={form.budget_limit_usd} onChange={(e) => setForm({ ...form, budget_limit_usd: e.target.value })} className="input" />
            <input placeholder="Max tokens per request" value={form.max_tokens_per_request} onChange={(e) => setForm({ ...form, max_tokens_per_request: e.target.value })} className="input" />
          </div>
          <input placeholder="Allowed models (comma-separated, empty = all)" value={form.allowed_models} onChange={(e) => setForm({ ...form, allowed_models: e.target.value })} className="input w-full" />
          <div className="flex gap-2">
            <button type="submit" className="btn-amber">Create</button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">Cancel</button>
          </div>
        </form>
      )}

      <div className="grid gap-4">
        {quotas.map((q) => {
          const budgetPct = q.budget_limit_usd ? (q.current_spend / q.budget_limit_usd) * 100 : 0;
          const isNearLimit = budgetPct > 80;
          return (
            <div key={q.id} className={`card p-6 ${isNearLimit ? "!border-amber-300" : ""}`}>
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-teal-50">
                    <ShieldCheck className={`h-4 w-4 ${isNearLimit ? "text-amber-600" : "text-teal-600"}`} />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-stone-900">{q.name}</p>
                    <p className="text-xs text-stone-500">{q.scope}: {q.scope_id}</p>
                  </div>
                </div>
                <div className="flex gap-1">
                  <button onClick={async () => { await fetch(`${API_BASE}/api/quotas/${q.id}/push`, { method: "POST" }); }} title="Push to gateway" className="rounded-xl p-2 text-stone-400 hover:bg-amber-50 hover:text-amber-600 transition">
                    <Upload className="h-4 w-4" />
                  </button>
                  <button onClick={() => deleteMutation.mutate(q.id)} title="Delete" className="rounded-xl p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
              <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-4">
                {q.rate_limit_rpm != null && (
                  <div className="rounded-xl bg-stone-50 border border-stone-100 p-3">
                    <p className="text-xs text-stone-500">Rate Limit</p>
                    <p className="text-sm font-medium text-stone-900">{q.rate_limit_rpm} RPM</p>
                    <p className="text-xs text-stone-400">current: {q.current_rpm} RPM</p>
                  </div>
                )}
                {q.budget_limit_usd != null && (
                  <div className="rounded-xl bg-stone-50 border border-stone-100 p-3">
                    <p className="text-xs text-stone-500">Budget</p>
                    <p className="text-sm font-medium text-stone-900">${q.budget_limit_usd}</p>
                    <div className="mt-1 h-2 w-full rounded-full bg-stone-200">
                      <div className={`h-2 rounded-full ${budgetPct > 90 ? "bg-red-500" : budgetPct > 70 ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${Math.min(budgetPct, 100)}%` }} />
                    </div>
                    <p className="text-xs text-stone-400 mt-0.5">${q.current_spend.toFixed(2)} spent ({budgetPct.toFixed(0)}%)</p>
                  </div>
                )}
                {q.max_tokens_per_request != null && (
                  <div className="rounded-xl bg-stone-50 border border-stone-100 p-3">
                    <p className="text-xs text-stone-500">Max Tokens/Req</p>
                    <p className="text-sm font-medium text-stone-900">{q.max_tokens_per_request.toLocaleString()}</p>
                  </div>
                )}
                {q.allowed_models.length > 0 && (
                  <div className="rounded-xl bg-stone-50 border border-stone-100 p-3">
                    <p className="text-xs text-stone-500">Allowed Models</p>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {q.allowed_models.map(m => (
                        <span key={m} className="badge bg-violet-50 text-violet-700 ring-1 ring-inset ring-violet-200">{m}</span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
              {isNearLimit && (
                <div className="mt-3 flex items-center gap-2 text-amber-600">
                  <AlertTriangle className="h-4 w-4" />
                  <span className="text-xs">Approaching budget limit ({budgetPct.toFixed(0)}% used)</span>
                </div>
              )}
            </div>
          );
        })}
        {quotas.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-12">
            <ShieldCheck className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No quotas configured. Add one to enforce rate limits or budget caps.</p>
          </div>
        )}
      </div>
    </div>
  );
}
