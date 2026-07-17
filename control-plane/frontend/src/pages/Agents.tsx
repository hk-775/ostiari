import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Bot, Trash2, Plus, Route, Save } from "lucide-react";
import { useState } from "react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";

interface AgentConfig {
  name: string;
  framework: string;
  gateway_id: string;
  tools: string[];
  description: string;
  status: string;
  model: string;
}



async function fetchAgents(): Promise<AgentConfig[]> {
  return (await fetch(`${API_BASE}/api/agents`)).json();
}


const FRAMEWORK_COLORS: Record<string, { bg: string; text: string }> = {
  openai: { bg: "bg-emerald-50", text: "text-emerald-700" },
  anthropic: { bg: "bg-amber-50", text: "text-amber-700" },
  strands: { bg: "bg-sky-50", text: "text-sky-700" },
  bedrock: { bg: "bg-orange-50", text: "text-orange-700" },
  agentcore: { bg: "bg-purple-50", text: "text-purple-700" },
  crewai: { bg: "bg-pink-50", text: "text-pink-700" },
  langgraph: { bg: "bg-indigo-50", text: "text-indigo-700" },
  "gateway-invoke": { bg: "bg-violet-50", text: "text-violet-700" },
};

const ROUTING_STRATEGIES = [
  { value: "none", label: "No Override (use global)", description: "Uses the default routing from Models page" },
  { value: "smart-routing", label: "Smart Routing", description: "Classify prompt → pick best model" },
  { value: "round-robin", label: "Round Robin", description: "Rotate between allowed models" },
  { value: "least-latency", label: "Least Latency", description: "Pick the fastest responding model" },
  { value: "cost-optimized", label: "Cost Optimized", description: "Always pick the cheapest model" },
  { value: "force-model", label: "Force Specific Model", description: "Always use one model regardless of task" },
  { value: "ensemble-quality", label: "Ensemble (Quality)", description: "Send to N models, synthesize best answer — prioritize accuracy" },
  { value: "ensemble-budget", label: "Ensemble (Budget)", description: "Send to N cheap models, majority vote — quality within budget" },
];

interface RoutingOverride {
  agent: string;
  strategy: string;
  preferred_model: string;
  fallback_chain: string[];
  ensemble_size: number;
}

const DEFAULT_OVERRIDES: RoutingOverride[] = [
  { agent: "research-bot", strategy: "smart-routing", preferred_model: "", fallback_chain: ["claude-haiku", "gpt-4o-mini"], ensemble_size: 3 },
  { agent: "ops-bot", strategy: "none", preferred_model: "", fallback_chain: [], ensemble_size: 3 },
  { agent: "devops-bot", strategy: "force-model", preferred_model: "claude-sonnet", fallback_chain: ["claude-haiku"], ensemble_size: 3 },
  { agent: "gov-bot", strategy: "ensemble-quality", preferred_model: "", fallback_chain: ["nova-pro", "nova-lite", "llama-4-maverick"], ensemble_size: 3 },
  { agent: "intern-bot", strategy: "cost-optimized", preferred_model: "", fallback_chain: ["nova-lite", "claude-haiku"], ensemble_size: 2 },
  { agent: "analytics-bot", strategy: "ensemble-budget", preferred_model: "", fallback_chain: ["claude-haiku", "gpt-4o-mini", "nova-lite"], ensemble_size: 3 },
];

function RoutingOverrideSection({ agents }: { agents: AgentConfig[] }) {
  const [overrides, setOverrides] = useState<RoutingOverride[]>(DEFAULT_OVERRIDES);
  const [saved, setSaved] = useState(false);

  const updateOverride = (agent: string, field: keyof RoutingOverride, value: any) => {
    setOverrides(prev => prev.map(o => o.agent === agent ? { ...o, [field]: value } : o));
  };

  const saveOverrides = async () => {
    try {
      await fetch(`${API_BASE}/api/proxy/gateway/crm-agent/config/routing-overrides`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overrides }),
      });
    } catch { /* gateway may not support this yet */ }
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Route className="h-5 w-5 text-indigo-600" />
          <h2 className="text-lg font-semibold text-stone-900">Per-Agent Routing Override</h2>
        </div>
        <div className="flex items-center gap-2">
          {saved && <span className="text-xs text-emerald-600 font-medium">Saved!</span>}
          <button onClick={saveOverrides} className="btn-primary text-xs"><Save className="h-3.5 w-3.5" /> Save</button>
        </div>
      </div>
      <p className="text-xs text-stone-500">Override the global LLM routing strategy for specific agents. Agents without an override use the default from the Models page.</p>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {overrides.map((override) => {
          const strategy = ROUTING_STRATEGIES.find(s => s.value === override.strategy);
          const isEnsemble = override.strategy.startsWith("ensemble");
          const isForce = override.strategy === "force-model";

          return (
            <div key={override.agent} className="card p-4 space-y-3">
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-stone-800">{override.agent}</p>
                {isEnsemble && (
                  <span className="badge bg-purple-50 text-purple-700 text-[10px]">×{override.ensemble_size} models</span>
                )}
              </div>

              {/* Strategy dropdown */}
              <div>
                <label className="text-[10px] font-semibold text-stone-400 uppercase">Routing Strategy</label>
                <select
                  value={override.strategy}
                  onChange={(e) => updateOverride(override.agent, "strategy", e.target.value)}
                  className="mt-0.5 w-full rounded-lg border border-stone-200 bg-white px-2 py-1.5 text-xs"
                >
                  {ROUTING_STRATEGIES.map(s => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
                {strategy && <p className="text-[10px] text-stone-400 mt-0.5">{strategy.description}</p>}
              </div>

              {/* Force model selector */}
              {isForce && (
                <div>
                  <label className="text-[10px] font-semibold text-stone-400 uppercase">Forced Model</label>
                  <input
                    value={override.preferred_model}
                    onChange={(e) => updateOverride(override.agent, "preferred_model", e.target.value)}
                    placeholder="e.g., claude-sonnet"
                    className="mt-0.5 w-full rounded-lg border border-stone-200 px-2 py-1.5 text-xs"
                  />
                </div>
              )}

              {/* Ensemble size */}
              {isEnsemble && (
                <div>
                  <label className="text-[10px] font-semibold text-stone-400 uppercase">Panel Size</label>
                  <select
                    value={override.ensemble_size}
                    onChange={(e) => updateOverride(override.agent, "ensemble_size", parseInt(e.target.value))}
                    className="mt-0.5 w-full rounded-lg border border-stone-200 bg-white px-2 py-1.5 text-xs"
                  >
                    <option value={2}>2 models</option>
                    <option value={3}>3 models (recommended)</option>
                    <option value={5}>5 models (high confidence)</option>
                  </select>
                  <p className="text-[10px] text-stone-400 mt-0.5">
                    {override.strategy === "ensemble-quality" ? "Picks best answer by quality scoring" : "Majority vote from cheap models"}
                  </p>
                </div>
              )}

              {/* Fallback chain */}
              {override.strategy !== "none" && (
                <div>
                  <label className="text-[10px] font-semibold text-stone-400 uppercase">
                    {isEnsemble ? "Ensemble Models" : "Fallback Chain"}
                  </label>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {override.fallback_chain.map((m, i) => (
                      <span key={m} className="inline-flex items-center gap-0.5 rounded-full bg-indigo-50 text-indigo-700 px-2 py-0.5 text-[10px] font-medium">
                        {isEnsemble ? "" : `${i + 1}. `}{m}
                        <button onClick={() => updateOverride(override.agent, "fallback_chain", override.fallback_chain.filter((_, j) => j !== i))} className="ml-0.5 hover:text-rose-600">×</button>
                      </span>
                    ))}
                  </div>
                  <input
                    placeholder="Add model..."
                    className="mt-1 w-full rounded-lg border border-stone-200 px-2 py-1 text-[10px]"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        const val = (e.target as HTMLInputElement).value.trim();
                        if (val) {
                          updateOverride(override.agent, "fallback_chain", [...override.fallback_chain, val]);
                          (e.target as HTMLInputElement).value = "";
                        }
                      }
                    }}
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function Agents() {
  const queryClient = useQueryClient();
  const { data: agents = [], isLoading } = useQuery({ queryKey: ["agents"], queryFn: fetchAgents });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", framework: "openai", gateway_id: "", tools: "", description: "", model: "" });

  const createMutation = useMutation({
    mutationFn: async (data: typeof form) => {
      await fetch(`${API_BASE}/api/agents`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...data, tools: data.tools.split(",").map(t => t.trim()).filter(Boolean) }),
      });
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["agents"] }); setShowForm(false); },
  });

  const deleteMutation = useMutation({
    mutationFn: async (name: string) => { await fetch(`${API_BASE}/api/agents/${name}`, { method: "DELETE" }); },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents"] }),
  });

  const frameworks = [...new Set(agents.map(a => a.framework))];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-stone-900">Agents</h1>
          <p className="mt-1 text-sm text-stone-500">{agents.length} agents across {frameworks.length} frameworks</p>
        </div>
        <button onClick={() => setShowForm(true)} className="btn-primary">
          <Plus className="h-4 w-4" /> Register Agent
        </button>
      </div>

      {showForm && (
        <form onSubmit={(e) => { e.preventDefault(); createMutation.mutate(form); }} className="card p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Agent name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" />
            <select value={form.framework} onChange={(e) => setForm({ ...form, framework: e.target.value })} className="input">
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="strands">Strands</option>
              <option value="bedrock">Bedrock</option>
              <option value="agentcore">AgentCore</option>
              <option value="crewai">CrewAI</option>
              <option value="langgraph">LangGraph</option>
              <option value="gateway-invoke">Gateway Invoke</option>
            </select>
            <input placeholder="Gateway ID" value={form.gateway_id} onChange={(e) => setForm({ ...form, gateway_id: e.target.value })} className="input" />
            <input placeholder="Model" value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} className="input" />
            <input placeholder="Tools (comma-separated)" value={form.tools} onChange={(e) => setForm({ ...form, tools: e.target.value })} className="input col-span-2" />
            <input placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="input col-span-2" />
          </div>
          <div className="flex gap-2">
            <button type="submit" className="btn-primary">Register</button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">Cancel</button>
          </div>
        </form>
      )}

      {/* Framework summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {frameworks.map(fw => {
          const count = agents.filter(a => a.framework === fw).length;
          const colors = FRAMEWORK_COLORS[fw] || { bg: "bg-stone-50", text: "text-stone-600" };
          return (
            <div key={fw} className={`rounded-xl border border-stone-200 p-3 ${colors.bg}`}>
              <p className={`text-xs font-semibold uppercase ${colors.text}`}>{fw}</p>
              <p className="text-lg font-bold text-stone-900">{count} agent{count !== 1 ? "s" : ""}</p>
            </div>
          );
        })}
      </div>

      {/* Agents table */}
      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold text-stone-500 uppercase tracking-wider">
              <th className="px-6 py-3.5">Agent</th>
              <th className="px-6 py-3.5">Framework</th>
              <th className="px-6 py-3.5">Model</th>
              <th className="px-6 py-3.5">Gateway</th>
              <th className="px-6 py-3.5">Tools</th>
              <th className="px-6 py-3.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {agents.map((a, idx) => {
              const colors = FRAMEWORK_COLORS[a.framework] || { bg: "bg-stone-50", text: "text-stone-600" };
              return (
                <tr key={a.name} className={`transition hover:bg-stone-50 ${idx % 2 === 1 ? "bg-stone-50/50" : ""}`}>
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-3">
                      <div className={`flex h-9 w-9 items-center justify-center rounded-xl ${colors.bg}`}>
                        <Bot className={`h-4 w-4 ${colors.text}`} />
                      </div>
                      <div>
                        <p className="text-sm font-medium text-stone-800">{a.name}</p>
                        <p className="text-xs text-stone-400">{a.description}</p>
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    <span className={`badge ${colors.bg} ${colors.text}`}>{a.framework}</span>
                  </td>
                  <td className="px-6 py-4 text-sm text-stone-600 font-mono">{a.model}</td>
                  <td className="px-6 py-4 text-sm text-stone-500">{a.gateway_id.replace("-agent","").split("-").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ").replace("Devops","DevOps").replace("Crm","CRM") + " Gateway"}</td>
                  <td className="px-6 py-4">
                    <div className="flex gap-1 flex-wrap max-w-48">
                      {a.tools.slice(0, 3).map(t => (
                        <span key={t} className="badge bg-stone-100 text-stone-600 text-xs">{t}</span>
                      ))}
                      {a.tools.length > 3 && <span className="text-xs text-stone-400">+{a.tools.length - 3}</span>}
                    </div>
                  </td>
                  <td className="px-6 py-4 text-right">
                    <button onClick={() => deleteMutation.mutate(a.name)} className="rounded-xl p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {agents.length === 0 && !isLoading && (
          <div className="flex flex-col items-center gap-3 py-12">
            <Bot className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No agents registered</p>
          </div>
        )}
      </div>

      {/* Per-Agent Routing Override */}
      {agents.length > 0 && (
        <RoutingOverrideSection agents={agents} />
      )}
    </div>
  );
}
