import { useState } from "react";
import { ShieldCheck, Plus, Trash2, Save, Edit2, X } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";
const GATEWAY_BASE = `${API_BASE}/api/proxy/gateway/crm-agent`;

interface AgentQuota {
  agent_id: string;
  rate_limit_rpm: number | null;
  budget_limit_usd: number | null;
  max_tokens_per_request: number | null;
  allowed_models: string[];
  allowed_providers: string[];
  alert_threshold_pct: number;
  spend_usd: number;
}

const DEFAULT_QUOTAS: AgentQuota[] = [
  { agent_id: "research-bot", rate_limit_rpm: 30, budget_limit_usd: 5.0, max_tokens_per_request: 2048, allowed_models: ["claude-haiku", "gpt-4o-mini"], allowed_providers: ["anthropic", "openai"], alert_threshold_pct: 90, spend_usd: 1.82 },
  { agent_id: "ops-bot", rate_limit_rpm: 60, budget_limit_usd: 50.0, max_tokens_per_request: 8192, allowed_models: ["*"], allowed_providers: ["*"], alert_threshold_pct: 80, spend_usd: 12.40 },
  { agent_id: "devops-bot", rate_limit_rpm: 100, budget_limit_usd: 25.0, max_tokens_per_request: 16384, allowed_models: ["claude-sonnet", "claude-haiku"], allowed_providers: ["anthropic", "bedrock"], alert_threshold_pct: 90, spend_usd: 8.15 },
  { agent_id: "gov-bot", rate_limit_rpm: 20, budget_limit_usd: 100.0, max_tokens_per_request: 4096, allowed_models: ["*"], allowed_providers: ["bedrock"], alert_threshold_pct: 80, spend_usd: 3.20 },
  { agent_id: "intern-bot", rate_limit_rpm: 10, budget_limit_usd: 1.0, max_tokens_per_request: 1024, allowed_models: ["claude-haiku", "nova-lite"], allowed_providers: ["bedrock"], alert_threshold_pct: 90, spend_usd: 0.87 },
  { agent_id: "analytics-bot", rate_limit_rpm: 40, budget_limit_usd: 10.0, max_tokens_per_request: 4096, allowed_models: ["claude-haiku", "gpt-4o-mini", "nova-lite"], allowed_providers: ["anthropic", "openai", "bedrock"], alert_threshold_pct: 90, spend_usd: 4.55 },
];

export function AgentQuotas() {
  const [quotas, setQuotas] = useState<AgentQuota[]>(DEFAULT_QUOTAS);
  const [editing, setEditing] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<AgentQuota>({ agent_id: "", rate_limit_rpm: 30, budget_limit_usd: 10, max_tokens_per_request: 4096, allowed_models: [], allowed_providers: [], alert_threshold_pct: 90, spend_usd: 0 });
  const [saved, setSaved] = useState(false);

  const saveAll = async () => {
    try {
      await fetch(`${GATEWAY_BASE}/config/agent-auth`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: true,
          agents: Object.fromEntries(quotas.map(q => [q.agent_id, {
            allowed_tools: ["*"],
            allowed_models: q.allowed_models,
            allowed_providers: q.allowed_providers,
            budget_usd: q.budget_limit_usd,
          }])),
        }),
      });
    } catch {}
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const startEdit = (q: AgentQuota) => {
    setForm({ ...q });
    setEditing(q.agent_id);
    setShowForm(true);
  };

  const saveForm = () => {
    if (editing) {
      setQuotas(prev => prev.map(q => q.agent_id === editing ? { ...form } : q));
    } else {
      setQuotas(prev => [...prev, { ...form, spend_usd: 0 }]);
    }
    setShowForm(false);
    setEditing(null);
  };

  const deleteQuota = (agent_id: string) => {
    setQuotas(prev => prev.filter(q => q.agent_id !== agent_id));
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-stone-900">Agent Quotas</h1>
          <p className="mt-1 text-sm text-stone-500">Per-agent rate limits, budgets, and token caps</p>
        </div>
        <div className="flex items-center gap-2">
          {saved && <span className="text-xs text-emerald-600 font-medium">Saved!</span>}
          <button onClick={saveAll} className="btn-primary"><Save className="h-4 w-4" /> Push to Gateway</button>
          <button onClick={() => { setForm({ agent_id: "", rate_limit_rpm: 30, budget_limit_usd: 10, max_tokens_per_request: 4096, allowed_models: [], allowed_providers: [], alert_threshold_pct: 90, spend_usd: 0 }); setEditing(null); setShowForm(true); }} className="btn-primary">
            <Plus className="h-4 w-4" /> Add Agent
          </button>
        </div>
      </div>

      {/* Add/Edit Form */}
      {showForm && (
        <div className="card p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-stone-800">{editing ? `Edit: ${editing}` : "Add Agent Quota"}</h3>
            <button onClick={() => { setShowForm(false); setEditing(null); }} className="text-stone-400 hover:text-stone-600"><X className="h-4 w-4" /></button>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-[10px] font-semibold text-stone-400 uppercase">Agent ID</label>
              <input value={form.agent_id} onChange={(e) => setForm({ ...form, agent_id: e.target.value })} className="input mt-1" placeholder="e.g., research-bot" disabled={!!editing} />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-stone-400 uppercase">Rate Limit (RPM)</label>
              <input type="number" value={form.rate_limit_rpm || ""} onChange={(e) => setForm({ ...form, rate_limit_rpm: parseInt(e.target.value) || null })} className="input mt-1" placeholder="60" />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-stone-400 uppercase">Budget ($)</label>
              <input type="number" step="0.01" value={form.budget_limit_usd || ""} onChange={(e) => setForm({ ...form, budget_limit_usd: parseFloat(e.target.value) || null })} className="input mt-1" placeholder="10.00" />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-stone-400 uppercase">Max Tokens / Request</label>
              <input type="number" value={form.max_tokens_per_request || ""} onChange={(e) => setForm({ ...form, max_tokens_per_request: parseInt(e.target.value) || null })} className="input mt-1" placeholder="4096" />
            </div>
            <div>
              <label className="text-[10px] font-semibold text-stone-400 uppercase">Alert Threshold (%)</label>
              <select value={form.alert_threshold_pct} onChange={(e) => setForm({ ...form, alert_threshold_pct: parseInt(e.target.value) })} className="input mt-1">
                <option value={80}>80%</option>
                <option value={90}>90%</option>
                <option value={100}>100% (no warning)</option>
              </select>
            </div>
            <div>
              <label className="text-[10px] font-semibold text-stone-400 uppercase">Allowed Providers</label>
              <input value={form.allowed_providers.join(", ")} onChange={(e) => setForm({ ...form, allowed_providers: e.target.value.split(",").map(s => s.trim()).filter(Boolean) })} className="input mt-1" placeholder="* or anthropic, openai, bedrock" />
            </div>
            <div className="col-span-2">
              <label className="text-[10px] font-semibold text-stone-400 uppercase">Allowed Models</label>
              <input value={form.allowed_models.join(", ")} onChange={(e) => setForm({ ...form, allowed_models: e.target.value.split(",").map(s => s.trim()).filter(Boolean) })} className="input mt-1" placeholder="* or claude-haiku, gpt-4o-mini" />
            </div>
          </div>
          <div className="flex gap-2">
            <button onClick={saveForm} className="btn-primary">Save</button>
            <button onClick={() => { setShowForm(false); setEditing(null); }} className="btn-secondary">Cancel</button>
          </div>
        </div>
      )}

      {/* Quota cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {quotas.map(q => {
          const budgetPct = q.budget_limit_usd ? Math.min(100, (q.spend_usd / q.budget_limit_usd) * 100) : 0;
          const barColor = budgetPct >= 90 ? "bg-rose-500" : budgetPct >= 70 ? "bg-amber-500" : "bg-emerald-500";

          return (
            <div key={q.agent_id} className="card p-5 space-y-3">
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-stone-800">{q.agent_id}</p>
                <div className="flex items-center gap-1">
                  <button onClick={() => startEdit(q)} className="rounded-lg p-1.5 text-stone-400 hover:bg-indigo-50 hover:text-indigo-600 transition"><Edit2 className="h-3.5 w-3.5" /></button>
                  <button onClick={() => deleteQuota(q.agent_id)} className="rounded-lg p-1.5 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition"><Trash2 className="h-3.5 w-3.5" /></button>
                </div>
              </div>

              {/* Budget bar */}
              {q.budget_limit_usd && (
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-semibold text-stone-400 uppercase">Budget</span>
                    <span className="text-[10px] text-stone-500">${q.spend_usd.toFixed(2)} / ${q.budget_limit_usd.toFixed(2)}</span>
                  </div>
                  <div className="h-2 w-full rounded-full bg-stone-100 overflow-hidden">
                    <div className={`h-full rounded-full transition-all ${barColor}`} style={{ width: `${budgetPct}%` }} />
                  </div>
                  <p className="text-[10px] text-stone-400 mt-0.5">{budgetPct.toFixed(0)}% used · Alert at {q.alert_threshold_pct}%</p>
                </div>
              )}

              {/* Limits */}
              <div className="grid grid-cols-2 gap-2 text-[10px]">
                <div className="rounded-lg bg-stone-50 px-2.5 py-1.5">
                  <span className="text-stone-400">Rate</span>
                  <p className="font-semibold text-stone-700">{q.rate_limit_rpm || "∞"} RPM</p>
                </div>
                <div className="rounded-lg bg-stone-50 px-2.5 py-1.5">
                  <span className="text-stone-400">Max Tokens</span>
                  <p className="font-semibold text-stone-700">{q.max_tokens_per_request?.toLocaleString() || "∞"}</p>
                </div>
              </div>

              {/* Models & Providers */}
              <div className="space-y-1.5">
                <div className="flex flex-wrap gap-1">
                  {q.allowed_models.slice(0, 3).map(m => (
                    <span key={m} className="rounded-full bg-violet-50 text-violet-700 px-2 py-0.5 text-[9px] font-medium">{m}</span>
                  ))}
                  {q.allowed_models.length > 3 && <span className="text-[9px] text-stone-400">+{q.allowed_models.length - 3}</span>}
                </div>
                <div className="flex flex-wrap gap-1">
                  {q.allowed_providers.map(p => (
                    <span key={p} className="rounded-full bg-sky-50 text-sky-700 px-2 py-0.5 text-[9px] font-medium">{p}</span>
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
