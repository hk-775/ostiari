import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, Plus, Trash2, AlertTriangle, Upload, Check, Clock, Pencil, X } from "lucide-react";
import { api } from "../lib/api";

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

interface BudgetAlert {
  gateway_id: string;
  threshold: string;
  spend_usd: number;
  budget_usd: number;
  timestamp: number;
}

async function fetchQuotas(): Promise<Quota[]> {
  const res = await fetch(`${API_BASE}/api/quotas`);
  if (res.status === 404) return [];
  return res.json();
}

async function fetchAlerts(): Promise<BudgetAlert[]> {
  const res = await fetch(`${API_BASE}/api/quotas/alerts`);
  if (!res.ok) return [];
  return res.json();
}

export function Quotas() {
  const queryClient = useQueryClient();
  const { data: quotas = [] } = useQuery({ queryKey: ["quotas"], queryFn: fetchQuotas });
  // Gateways report threshold crossings as they happen, so poll rather than
  // waiting for a navigation to reveal that a budget blew through 100%.
  const { data: alerts = [] } = useQuery({
    queryKey: ["budget-alerts"], queryFn: fetchAlerts, refetchInterval: 15_000,
  });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", scope: "gateway", scope_id: "", rate_limit_rpm: "", budget_limit_usd: "", max_tokens_per_request: "", allowed_models: "" });
  const [pushStatus, setPushStatus] = useState<Record<number, string>>({});
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState({ rate_limit_rpm: "", budget_limit_usd: "", max_tokens_per_request: "" });
  const [editError, setEditError] = useState("");

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

  const clearAlertsMutation = useMutation({
    mutationFn: async () => { await fetch(`${API_BASE}/api/quotas/alerts`, { method: "DELETE" }); },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["budget-alerts"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Gateway Quotas</h1>
          <p className="mt-1 text-sm text-stone-500">Per-gateway rate limits, budget caps, and token limits</p>
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
          <div className="flex gap-2">
            <button type="submit" className="btn-amber">Create</button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">Cancel</button>
          </div>
        </form>
      )}

      {alerts.length > 0 && (
        <div className="card p-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-amber-600" />
              <p className="text-sm font-medium text-stone-900">Budget Alerts</p>
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">{alerts.length}</span>
            </div>
            <button onClick={() => clearAlertsMutation.mutate()} className="btn-secondary text-xs">
              Acknowledge all
            </button>
          </div>
          <p className="mt-1 text-xs text-stone-500">
            Threshold crossings reported by gateways as they enforce budgets. Newest first.
          </p>
          <div className="mt-4 divide-y divide-stone-100">
            {alerts.map((a, i) => (
              <div key={`${a.gateway_id}-${a.threshold}-${a.timestamp}-${i}`} className="flex items-center justify-between py-2">
                <div className="flex items-center gap-3">
                  <span className={`rounded-lg px-2 py-0.5 text-xs font-semibold ${
                    a.threshold === "100%" ? "bg-rose-50 text-rose-700"
                      : a.threshold === "90%" ? "bg-orange-50 text-orange-700"
                      : "bg-amber-50 text-amber-700"
                  }`}>{a.threshold || "—"}</span>
                  <span className="text-sm text-stone-900">{a.gateway_id || "unknown gateway"}</span>
                </div>
                <div className="flex items-center gap-4 text-xs text-stone-500">
                  <span>${a.spend_usd.toFixed(4)} / ${a.budget_usd.toFixed(2)}</span>
                  <span className="tabular-nums">
                    {a.timestamp ? new Date(a.timestamp * 1000).toLocaleString() : ""}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
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
                  </div>
                </div>
                <div className="flex gap-1">
                  <button onClick={() => {
                    setEditError("");
                    if (editingId === q.id) { setEditingId(null); } else {
                      setEditingId(q.id);
                      setEditForm({ rate_limit_rpm: String(q.rate_limit_rpm || ""), budget_limit_usd: String(q.budget_limit_usd || ""), max_tokens_per_request: String(q.max_tokens_per_request || "") });
                    }
                  }} title="Edit" className="rounded-xl p-2 text-stone-400 hover:bg-indigo-50 hover:text-indigo-600 transition">
                    <Pencil className="h-4 w-4" />
                  </button>
                  <button onClick={async () => {
                    if (q.scope !== "gateway" || !q.scope_id) {
                      setPushStatus(prev => ({ ...prev, [q.id]: "error" }));
                      setTimeout(() => setPushStatus(prev => ({ ...prev, [q.id]: "" })), 2000);
                      return;
                    }
                    setPushStatus(prev => ({ ...prev, [q.id]: "pushing" }));
                    try {
                      const quotaPayload = {
                        rate_limit_rpm: q.rate_limit_rpm,
                        budget_limit_usd: q.budget_limit_usd,
                        max_tokens_per_request: q.max_tokens_per_request,
                        allowed_models: q.allowed_models,
                      };
                      const res = await api.gateways.pushConfig(q.scope_id, { quota: quotaPayload });
                      if (res.status === "applied") {
                        setPushStatus(prev => ({ ...prev, [q.id]: "done" }));
                      } else if (res.status === "queued") {
                        setPushStatus(prev => ({ ...prev, [q.id]: "queued" }));
                      } else {
                        setPushStatus(prev => ({ ...prev, [q.id]: "error" }));
                      }
                      setTimeout(() => setPushStatus(prev => ({ ...prev, [q.id]: "" })), 3000);
                    } catch {
                      setPushStatus(prev => ({ ...prev, [q.id]: "error" }));
                      setTimeout(() => setPushStatus(prev => ({ ...prev, [q.id]: "" })), 2000);
                    }
                  }} title="Push to gateway" className="rounded-xl p-2 text-stone-400 hover:bg-amber-50 hover:text-amber-600 transition">
                    {pushStatus[q.id] === "done" ? <Check className="h-4 w-4 text-emerald-600" /> :
                     pushStatus[q.id] === "queued" ? <Clock className="h-4 w-4 text-amber-500" /> :
                     <Upload className="h-4 w-4" />}
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
              </div>
              {isNearLimit && (
                <div className="mt-3 flex items-center gap-2 text-amber-600">
                  <AlertTriangle className="h-4 w-4" />
                  <span className="text-xs">Approaching budget limit ({budgetPct.toFixed(0)}% used)</span>
                </div>
              )}
              {editingId === q.id && (
                <div className="mt-4 rounded-xl border border-indigo-100 bg-indigo-50/30 p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <p className="text-xs font-semibold text-indigo-700">Edit Quota</p>
                    <button onClick={() => { setEditingId(null); setEditError(""); }} className="text-stone-400 hover:text-stone-600"><X className="h-3.5 w-3.5" /></button>
                  </div>
                  {editError && (
                    <div className="flex items-center gap-2 rounded-lg bg-rose-50 border border-rose-200 px-3 py-2">
                      <AlertTriangle className="h-3.5 w-3.5 text-rose-600 shrink-0" />
                      <span className="text-xs text-rose-700">{editError}</span>
                    </div>
                  )}
                  <div className="grid grid-cols-3 gap-3">
                    <div>
                      <label className="text-[10px] font-semibold text-stone-400 uppercase">Rate Limit (RPM)</label>
                      <input type="number" value={editForm.rate_limit_rpm} onChange={(e) => setEditForm({ ...editForm, rate_limit_rpm: e.target.value })} className="input mt-1 text-xs" placeholder="60" />
                    </div>
                    <div>
                      <label className="text-[10px] font-semibold text-stone-400 uppercase">Budget ($)</label>
                      <input type="number" step="0.01" value={editForm.budget_limit_usd} onChange={(e) => setEditForm({ ...editForm, budget_limit_usd: e.target.value })} className="input mt-1 text-xs" placeholder="25.00" />
                    </div>
                    <div>
                      <label className="text-[10px] font-semibold text-stone-400 uppercase">Max Tokens</label>
                      <input type="number" value={editForm.max_tokens_per_request} onChange={(e) => setEditForm({ ...editForm, max_tokens_per_request: e.target.value })} className="input mt-1 text-xs" placeholder="4096" />
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button onClick={async () => {
                      const payload: any = { name: q.name };
                      if (editForm.rate_limit_rpm) payload.rate_limit_rpm = parseInt(editForm.rate_limit_rpm);
                      if (editForm.budget_limit_usd) payload.budget_limit_usd = parseFloat(editForm.budget_limit_usd);
                      if (editForm.max_tokens_per_request) payload.max_tokens_per_request = parseInt(editForm.max_tokens_per_request);
                      // The response is checked. This used to fire and forget, so
                      // when the PUT route didn't exist the 405 was invisible: the
                      // panel closed and the list refetched unchanged, which reads
                      // as a successful save.
                      const res = await fetch(`${API_BASE}/api/quotas/${q.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
                      if (!res.ok) {
                        setEditError(`Save failed (HTTP ${res.status})`);
                        return;
                      }
                      setEditError("");
                      queryClient.invalidateQueries({ queryKey: ["quotas"] });
                      setEditingId(null);
                    }} className="btn-primary text-xs">Save</button>
                    <button onClick={() => setEditingId(null)} className="btn-secondary text-xs">Cancel</button>
                  </div>
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
