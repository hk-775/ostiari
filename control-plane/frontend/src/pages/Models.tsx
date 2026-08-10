import { useState, useEffect, useCallback } from "react";
import { useLocation } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Brain, Plus, Trash2, Pencil, X, Wrench, Eye, Server, Shield, Lock, ArrowUp, ArrowDown, Save, Clock } from "lucide-react";
import { fetchAPI } from "../lib/api";

interface ProviderMapping {
  provider: string;
  model_id: string;
  weight: number;
  fallback_order: number;
}

interface ModelConfig {
  name: string;
  description: string;
  routing_strategy: string;
  providers: ProviderMapping[];
  input_cost_per_1k: number;
  output_cost_per_1k: number;
  max_tokens: number;
  supports_tools: boolean;
  supports_vision: boolean;
  category: string;
}

interface AgentAccess {
  agent: string;
  models: string[];
  providers: string[];
  budget: number;
  spend: number;
  alert_threshold: number;
  note?: string;
}

interface RoutingRule {
  model: string;
  routing_strategy: string;
  providers: { provider: string; weight: number; fallback_order: number }[];
}

interface BudgetResetConfig {
  schedule: "manual" | "daily" | "weekly" | "monthly";
  next_reset?: string;
}

const ALL_PROVIDERS = ["anthropic", "openai", "bedrock", "azure", "vertex", "cohere"];

const GATEWAY_PATH = "/api/proxy/gateway/crm-agent";

async function fetchModels(): Promise<ModelConfig[]> {
  return fetchAPI<ModelConfig[]>("/api/models");
}

async function fetchAgentAccess(): Promise<AgentAccess[]> {
  try {
    const data = await fetchAPI<{ enabled: boolean; agents?: Record<string, unknown>[] }>(
      `${GATEWAY_PATH}/config/agent-auth`,
    );
    // The gateway returns { enabled, agents: [{agent_id, allowed_models, ...}] }.
    // Map it to the page's AgentAccess shape. If disabled/empty, return [] so
    // the UI falls back to its illustrative defaults.
    if (!data?.enabled || !Array.isArray(data.agents)) return [];
    return data.agents.map((a: Record<string, unknown>) => ({
      agent: (a.agent_id as string) ?? "",
      models: (a.allowed_models as string[]) ?? ["*"],
      providers: (a.allowed_providers as string[]) ?? ["*"],
      budget: (a.budget_usd as number) ?? 0,
      spend: (a.spend_usd as number) ?? 0,
      alert_threshold: 90,
    }));
  } catch {
    return [];
  }
}

// Persist a per-agent access list to the gateway in its agent-auth schema
// ({ enabled, agents: { name: { allowed_models, ... } } }). The page holds an
// AgentAccess[] list; this maps it to what the gateway actually expects.
async function saveAgentAccess(list: AgentAccess[]): Promise<void> {
  const agents: Record<string, unknown> = {};
  for (const a of list) {
    agents[a.agent] = {
      allowed_tools: ["*"],
      allowed_models: a.models,
      allowed_providers: a.providers,
      budget_usd: a.budget,
      description: a.note ?? "",
    };
  }
  await fetchAPI(`${GATEWAY_PATH}/config/agent-auth`, {
    method: "POST",
    body: JSON.stringify({ enabled: true, agents }),
  });
}

async function fetchBudgetReset(): Promise<BudgetResetConfig> {
  try {
    return await fetchAPI<BudgetResetConfig>(`${GATEWAY_PATH}/config/budget-reset`);
  } catch {
    return { schedule: "manual" };
  }
}

const CATEGORY_COLORS: Record<string, string> = {
  reasoning: "bg-violet-50 text-violet-700",
  general: "bg-sky-50 text-sky-700",
  speed: "bg-emerald-50 text-emerald-700",
};

const PROVIDER_COLORS: Record<string, string> = {
  anthropic: "bg-amber-50 text-amber-700",
  bedrock: "bg-orange-50 text-orange-700",
  openai: "bg-emerald-50 text-emerald-700",
  vertex: "bg-blue-50 text-blue-700",
  "bedrock-mantle": "bg-purple-50 text-purple-700",
};

const EMPTY_FORM: ModelConfig = {
  name: "", description: "", routing_strategy: "round-robin",
  providers: [{ provider: "anthropic", model_id: "", weight: 1.0, fallback_order: 0 }],
  input_cost_per_1k: 0, output_cost_per_1k: 0, max_tokens: 4096,
  supports_tools: true, supports_vision: false, category: "general",
};

const DEFAULT_CLASSIFICATION_RULES: Record<string, string[]> = {
  code_generation: ["code", "function", "implement", "class", "method", "debug", "refactor", "api", "bug", "test"],
  creative: ["write", "story", "poem", "marketing", "blog", "draft", "compose", "narrative"],
  analysis: ["analyze", "compare", "evaluate", "summarize", "research", "review", "assess"],
  simple_qa: ["what is", "how do", "explain", "define", "tell me", "describe"],
  data: ["query", "sql", "csv", "json", "parse", "transform", "aggregate", "filter"],
};

const TASK_CATEGORY_COLORS: Record<string, string> = {
  code_generation: "bg-indigo-50 text-indigo-700 border-indigo-200",
  creative: "bg-pink-50 text-pink-700 border-pink-200",
  analysis: "bg-cyan-50 text-cyan-700 border-cyan-200",
  simple_qa: "bg-stone-50 text-stone-600 border-stone-200",
  data: "bg-amber-50 text-amber-700 border-amber-200",
};

function TaskClassificationRules({ models }: { models: ModelConfig[] }) {
  const [rules, setRules] = useState<Record<string, string[]>>(DEFAULT_CLASSIFICATION_RULES);
  const [modelMapping, setModelMapping] = useState<Record<string, string>>({
    code_generation: "claude-sonnet",
    creative: "claude-sonnet",
    analysis: "claude-opus",
    simple_qa: "claude-haiku",
    data: "gpt-4o-mini",
  });
  const [newKeyword, setNewKeyword] = useState<Record<string, string>>({});
  const [newCategory, setNewCategory] = useState("");
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState("");

  const addKeyword = (category: string) => {
    const kw = (newKeyword[category] || "").trim();
    if (!kw) return;
    setRules(prev => ({ ...prev, [category]: [...(prev[category] || []), kw] }));
    setNewKeyword(prev => ({ ...prev, [category]: "" }));
  };

  const removeKeyword = (category: string, keyword: string) => {
    setRules(prev => ({ ...prev, [category]: prev[category].filter(k => k !== keyword) }));
  };

  const addCategory = () => {
    const cat = newCategory.trim().toLowerCase().replace(/\s+/g, "_");
    if (!cat || rules[cat]) return;
    setRules(prev => ({ ...prev, [cat]: [] }));
    setModelMapping(prev => ({ ...prev, [cat]: models[0]?.name || "" }));
    setNewCategory("");
  };

  const removeCategory = (category: string) => {
    setRules(prev => { const next = { ...prev }; delete next[category]; return next; });
    setModelMapping(prev => { const next = { ...prev }; delete next[category]; return next; });
  };

  const saveRules = async () => {
    setSaveError("");
    try {
      await fetchAPI(`${GATEWAY_PATH}/config/task-classification`, {
        method: "POST",
        body: JSON.stringify({ rules, model_mapping: modelMapping }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Failed to save classification rules");
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Brain className="h-5 w-5 text-indigo-600" />
          <h2 className="text-lg font-semibold text-stone-900">Task Classification Rules</h2>
          <span className="badge bg-indigo-50 text-indigo-700 text-xs">Smart Routing</span>
        </div>
        <div className="flex items-center gap-2">
          {saveError && <span className="text-xs font-medium text-rose-600">{saveError}</span>}
          {saved && <span className="text-xs text-emerald-600 font-medium">Saved!</span>}
          <button onClick={saveRules} className="btn-primary"><Save className="h-4 w-4" /> Save Rules</button>
        </div>
      </div>
      <p className="text-xs text-stone-500">When a model uses "Smart Routing", prompts are classified by keywords and routed to the best model for that task type.</p>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(rules).map(([category, keywords]) => {
          const colors = TASK_CATEGORY_COLORS[category] || "bg-stone-50 text-stone-600 border-stone-200";
          return (
            <div key={category} className={`card p-4 space-y-3 border ${colors.split(" ").find(c => c.startsWith("border-")) || "border-stone-200"}`}>
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-stone-800">{category.replace(/_/g, " ")}</p>
                <button onClick={() => removeCategory(category)} className="text-stone-300 hover:text-rose-500"><Trash2 className="h-3.5 w-3.5" /></button>
              </div>

              {/* Target model */}
              <div>
                <label className="text-[10px] font-semibold text-stone-400 uppercase">Route to</label>
                <select
                  value={modelMapping[category] || ""}
                  onChange={(e) => setModelMapping(prev => ({ ...prev, [category]: e.target.value }))}
                  className="mt-0.5 w-full rounded-lg border border-stone-200 bg-white px-2 py-1 text-xs"
                >
                  {models.map(m => <option key={m.name} value={m.name}>{m.name} — {m.description}</option>)}
                </select>
              </div>

              {/* Keywords */}
              <div>
                <label className="text-[10px] font-semibold text-stone-400 uppercase">Keywords</label>
                <div className="flex flex-wrap gap-1 mt-1">
                  {keywords.map(kw => (
                    <span key={kw} className={`inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 text-[10px] font-medium ${colors}`}>
                      {kw}
                      <button onClick={() => removeKeyword(category, kw)} className="ml-0.5 hover:text-rose-600">×</button>
                    </span>
                  ))}
                </div>
                <div className="flex gap-1 mt-1.5">
                  <input
                    value={newKeyword[category] || ""}
                    onChange={(e) => setNewKeyword(prev => ({ ...prev, [category]: e.target.value }))}
                    onKeyDown={(e) => e.key === "Enter" && addKeyword(category)}
                    placeholder="Add keyword..."
                    className="flex-1 rounded-lg border border-stone-200 px-2 py-1 text-[10px]"
                  />
                  <button onClick={() => addKeyword(category)} className="rounded-lg bg-stone-100 px-2 py-1 text-[10px] hover:bg-stone-200">+</button>
                </div>
              </div>
            </div>
          );
        })}

        {/* Add new category */}
        <div className="card p-4 border border-dashed border-stone-300 flex flex-col items-center justify-center gap-2">
          <input
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addCategory()}
            placeholder="New category name..."
            className="w-full rounded-lg border border-stone-200 px-2 py-1.5 text-xs text-center"
          />
          <button onClick={addCategory} className="btn-secondary text-xs"><Plus className="h-3 w-3" /> Add Category</button>
        </div>
      </div>
    </div>
  );
}

export function Models() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const { data: models = [], isLoading } = useQuery({ queryKey: ["model-config"], queryFn: fetchModels });
  const { data: agentAccessList = [] } = useQuery({ queryKey: ["agent-access"], queryFn: fetchAgentAccess });
  const { data: budgetResetConfig = { schedule: "manual" as const } } = useQuery({ queryKey: ["budget-reset"], queryFn: fetchBudgetReset });
  const [showForm, setShowForm] = useState(false);
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [form, setForm] = useState<ModelConfig>({ ...EMPTY_FORM });
  const [modelError, setModelError] = useState("");

  useEffect(() => { setShowForm(false); setEditingModel(null); }, [location.key]);

  // Agent access state
  const [editingAgent, setEditingAgent] = useState<string | null>(null);
  const [agentForm, setAgentForm] = useState<AgentAccess>({ agent: "", models: [], providers: [], budget: 10, spend: 0, alert_threshold: 90 });
  const [showAgentForm, setShowAgentForm] = useState(false);
  const [agentAccessError, setAgentAccessError] = useState("");

  // Routing state
  const [routingRules, setRoutingRules] = useState<RoutingRule[]>([]);
  const [routingSaved, setRoutingSaved] = useState(false);
  const [routingError, setRoutingError] = useState("");

  // Budget reset state
  const [budgetReset, setBudgetReset] = useState<BudgetResetConfig>({ schedule: "manual" });
  const [budgetResetError, setBudgetResetError] = useState("");

  // Sync routing rules from models
  useEffect(() => {
    if (models.length > 0) {
      setRoutingRules(models.map(m => ({
        model: m.name,
        routing_strategy: m.routing_strategy,
        providers: m.providers.map(p => ({ provider: p.provider, weight: p.weight, fallback_order: p.fallback_order })),
      })));
    }
  }, [models]);

  // Sync budget reset from fetched config
  useEffect(() => {
    if (budgetResetConfig) {
      setBudgetReset(budgetResetConfig);
    }
  }, [budgetResetConfig]);

  const createMutation = useMutation({
    mutationFn: async (data: ModelConfig) => {
      const method = editingModel ? "PUT" : "POST";
      const path = editingModel ? `/api/models/${editingModel}` : "/api/models";
      await fetchAPI<ModelConfig>(path, { method, body: JSON.stringify(data) });
    },
    onMutate: () => setModelError(""),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-config"] });
      setShowForm(false);
      setEditingModel(null);
      setForm({ ...EMPTY_FORM });
    },
    onError: (error) => setModelError(error instanceof Error ? error.message : "Failed to save model"),
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => fetchAPI(`/api/models/${name}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["model-config"] }),
    onError: (error) => setModelError(error instanceof Error ? error.message : "Failed to delete model"),
  });

  const startEdit = (m: ModelConfig) => {
    setForm({ ...m });
    setEditingModel(m.name);
    setShowForm(true);
  };

  const addProvider = () => {
    setForm({ ...form, providers: [...form.providers, { provider: "bedrock", model_id: "", weight: 1.0, fallback_order: form.providers.length }] });
  };

  const removeProvider = (idx: number) => {
    setForm({ ...form, providers: form.providers.filter((_, i) => i !== idx) });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-stone-900">Agent Models</h1>
          <p className="mt-1 text-sm text-stone-500">Per-agent model access, provider restrictions, budgets, and routing</p>
        </div>
        <button onClick={() => { setForm({ ...EMPTY_FORM }); setEditingModel(null); setShowForm(true); }} className="btn-indigo">
          <Plus className="h-4 w-4" /> Add Model
        </button>
      </div>
      {modelError && <p className="text-sm font-medium text-rose-600">{modelError}</p>}

      {/* Add/Edit Form */}
      {showForm && (
        <div className="card p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-stone-800">{editingModel ? `Edit: ${editingModel}` : "Add New Model"}</h3>
            <button onClick={() => { setShowForm(false); setEditingModel(null); }} className="text-stone-400 hover:text-stone-600"><X className="h-4 w-4" /></button>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Model name (e.g., claude-sonnet)" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" disabled={!!editingModel} />
            <input placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="input" />
            <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} className="input">
              <option value="general">General</option>
              <option value="reasoning">Reasoning</option>
              <option value="speed">Speed</option>
            </select>
            <select value={form.routing_strategy} onChange={(e) => setForm({ ...form, routing_strategy: e.target.value })} className="input">
              <option value="round-robin">Round Robin</option>
              <option value="least-latency">Least Latency</option>
              <option value="cost-optimized">Cost Optimized</option>
              <option value="weighted">Weighted</option>
                    <option value="smart-routing">Smart Routing (task classification)</option>
                    <option value="ensemble">Ensemble (multi-model consensus)</option>
            </select>
            <input type="number" step="0.0001" placeholder="Input cost / 1k tokens" value={form.input_cost_per_1k || ""} onChange={(e) => setForm({ ...form, input_cost_per_1k: parseFloat(e.target.value) || 0 })} className="input" />
            <input type="number" step="0.0001" placeholder="Output cost / 1k tokens" value={form.output_cost_per_1k || ""} onChange={(e) => setForm({ ...form, output_cost_per_1k: parseFloat(e.target.value) || 0 })} className="input" />
            <input type="number" placeholder="Max tokens" value={form.max_tokens} onChange={(e) => setForm({ ...form, max_tokens: parseInt(e.target.value) || 4096 })} className="input" />
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-2 text-sm text-stone-600">
                <input type="checkbox" checked={form.supports_tools} onChange={(e) => setForm({ ...form, supports_tools: e.target.checked })} className="rounded" /> Tools
              </label>
              <label className="flex items-center gap-2 text-sm text-stone-600">
                <input type="checkbox" checked={form.supports_vision} onChange={(e) => setForm({ ...form, supports_vision: e.target.checked })} className="rounded" /> Vision
              </label>
            </div>
          </div>

          {/* Providers */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <p className="text-xs font-semibold text-stone-500 uppercase">Providers</p>
              <button type="button" onClick={addProvider} className="text-xs text-indigo-600 hover:text-indigo-700">+ Add provider</button>
            </div>
            {form.providers.map((p, i) => (
              <div key={i} className="flex gap-2 items-center">
                <select value={p.provider} onChange={(e) => { const providers = [...form.providers]; providers[i] = { ...p, provider: e.target.value }; setForm({ ...form, providers }); }} className="input w-36">
                  <option value="anthropic">Anthropic</option>
                  <option value="openai">OpenAI</option>
                  <option value="bedrock">Bedrock</option>
                  <option value="vertex">Vertex AI</option>
                  <option value="bedrock-mantle">Bedrock Mantle</option>
                  <option value="azure">Azure</option>
                  <option value="cohere">Cohere</option>
                </select>
                <input placeholder="Model ID" value={p.model_id} onChange={(e) => { const providers = [...form.providers]; providers[i] = { ...p, model_id: e.target.value }; setForm({ ...form, providers }); }} className="input flex-1" />
                <input type="number" step="0.1" placeholder="Weight" value={p.weight} onChange={(e) => { const providers = [...form.providers]; providers[i] = { ...p, weight: parseFloat(e.target.value) || 1 }; setForm({ ...form, providers }); }} className="input w-20" />
                {form.providers.length > 1 && (
                  <button onClick={() => removeProvider(i)} className="rounded-lg p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>

          <div className="flex gap-2">
            <button onClick={() => createMutation.mutate(form)} className="btn-indigo">{editingModel ? "Save Changes" : "Add Model"}</button>
            <button onClick={() => { setShowForm(false); setEditingModel(null); }} className="btn-secondary">Cancel</button>
          </div>
        </div>
      )}

      {/* Models List */}
      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold text-stone-500 uppercase tracking-wider">
              <th className="px-6 py-3.5">Model</th>
              <th className="px-6 py-3.5">Category</th>
              <th className="px-6 py-3.5">Providers</th>
              <th className="px-6 py-3.5">Pricing (per 1k)</th>
              <th className="px-6 py-3.5">Routing</th>
              <th className="px-6 py-3.5">Capabilities</th>
              <th className="px-6 py-3.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {models.map((m, idx) => (
              <tr key={m.name} className={`transition hover:bg-indigo-50 cursor-pointer ${idx % 2 === 1 ? "bg-stone-100" : "bg-white"}`} onClick={() => startEdit(m)}>
                <td className="px-6 py-4">
                  <div className="flex items-center gap-2">
                    <div className={`flex h-8 w-8 items-center justify-center rounded-lg ${CATEGORY_COLORS[m.category] || "bg-stone-100 text-stone-600"}`}>
                      <Brain className="h-4 w-4" />
                    </div>
                    <div>
                      <button className="text-sm font-medium text-stone-800 hover:text-indigo-600 transition text-left" onClick={() => startEdit(m)}>
                        {m.name}
                      </button>
                      <p className="text-xs text-stone-400">{m.description}</p>
                    </div>
                    <button className="rounded-lg p-1 text-stone-300 hover:text-indigo-600 hover:bg-indigo-50 transition" title="Edit model" onClick={() => startEdit(m)}>
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </td>
                <td className="px-6 py-4">
                  <span className={`badge ${CATEGORY_COLORS[m.category] || "bg-stone-100 text-stone-600"}`}>{m.category}</span>
                </td>
                <td className="px-6 py-4">
                  <div className="flex gap-1 flex-wrap">
                    {m.providers.map((p, i) => (
                      <span key={i} className={`badge text-xs ${PROVIDER_COLORS[p.provider] || "bg-stone-100 text-stone-600"}`}>{p.provider}</span>
                    ))}
                  </div>
                </td>
                <td className="px-6 py-4 text-xs text-stone-600 font-mono">
                  <span className="text-stone-400">in:</span>${m.input_cost_per_1k} <span className="text-stone-400">out:</span>${m.output_cost_per_1k}
                </td>
                <td className="px-6 py-4">
                  <select
                    value={m.routing_strategy}
                    onChange={async (e) => {
                      const updated = { ...m, routing_strategy: e.target.value };
                      setModelError("");
                      try {
                        await fetchAPI<ModelConfig>(`/api/models/${m.name}`, {
                          method: "PUT",
                          body: JSON.stringify(updated),
                        });
                        queryClient.invalidateQueries({ queryKey: ["model-config"] });
                      } catch (error) {
                        setModelError(error instanceof Error ? error.message : "Failed to update routing strategy");
                      }
                    }}
                    className="rounded-lg border border-stone-200 bg-white px-2 py-1 text-xs text-stone-700 cursor-pointer hover:border-indigo-300 focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-100"
                  >
                    <option value="round-robin">Round Robin</option>
                    <option value="least-latency">Least Latency</option>
                    <option value="cost-optimized">Cost Optimized</option>
                    <option value="weighted">Weighted</option>
                    <option value="smart-routing">Smart Routing (task classification)</option>
                    <option value="ensemble">Ensemble (multi-model consensus)</option>
                  </select>
                </td>
                <td className="px-6 py-4">
                  <div className="flex gap-1 flex-wrap">
                    {m.supports_tools && <span className="badge bg-teal-50 text-teal-700"><Wrench className="h-3 w-3 mr-0.5" />Tools</span>}
                    {m.supports_vision && <span className="badge bg-purple-50 text-purple-700"><Eye className="h-3 w-3 mr-0.5" />Vision</span>}
                    {!m.supports_tools && !m.supports_vision && <span className="text-xs text-stone-400">—</span>}
                  </div>
                </td>
                <td className="px-6 py-4">
                  <div className="flex justify-end gap-1">
                    <button onClick={() => startEdit(m)} title="Edit" className="rounded-lg p-2 text-stone-400 hover:bg-indigo-50 hover:text-indigo-600 transition">
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button onClick={() => deleteMutation.mutate(m.name)} title="Delete" className="rounded-lg p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {models.length === 0 && !isLoading && (
          <div className="flex flex-col items-center gap-3 py-12">
            <Brain className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No models configured</p>
          </div>
        )}
      </div>

      {/* Task Classification Rules — shown when any model uses smart-routing */}
      {models.some(m => m.routing_strategy === "smart-routing") && (
        <TaskClassificationRules models={models} />
      )}

      {/* Per-Agent Model Access */}
      <div id="per-agent-access" className="space-y-3 scroll-mt-16">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Shield className="h-5 w-5 text-violet-600" />
            <h2 className="text-lg font-semibold text-stone-900">Per-Agent Model Access</h2>
            <span className="badge bg-emerald-50 text-emerald-700 text-xs">Enforcing</span>
          </div>
          <button
            onClick={() => {
              setAgentForm({ agent: "", models: [], providers: [], budget: 10, spend: 0, alert_threshold: 90 });
              setEditingAgent(null);
              setShowAgentForm(true);
            }}
            className="btn-primary"
          >
            <Plus className="h-4 w-4" /> Add Agent
          </button>
        </div>
        <p className="text-xs text-stone-500">Controls which agents can access which models and providers. Configured via gateway agent-auth.</p>
        {agentAccessError && <p className="text-xs font-medium text-rose-600">{agentAccessError}</p>}

        {/* Add/Edit Agent Form */}
        {showAgentForm && (
          <div className="card p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-stone-800">{editingAgent ? `Edit: ${editingAgent}` : "Add New Agent Access"}</h3>
              <button onClick={() => { setShowAgentForm(false); setEditingAgent(null); }} className="text-stone-400 hover:text-stone-600"><X className="h-4 w-4" /></button>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <input
                placeholder="Agent name (e.g., research-bot)"
                value={agentForm.agent}
                onChange={(e) => setAgentForm({ ...agentForm, agent: e.target.value })}
                className="input"
                disabled={!!editingAgent}
              />
              <input
                type="number"
                step="0.01"
                placeholder="Budget ($)"
                value={agentForm.budget || ""}
                onChange={(e) => setAgentForm({ ...agentForm, budget: parseFloat(e.target.value) || 0 })}
                className="input"
              />
            </div>

            <div className="space-y-2">
              <p className="text-xs font-semibold text-stone-500 uppercase">Alert Threshold</p>
              <select
                value={agentForm.alert_threshold}
                onChange={(e) => setAgentForm({ ...agentForm, alert_threshold: parseInt(e.target.value) })}
                className="input w-40"
              >
                <option value={80}>80%</option>
                <option value={90}>90%</option>
                <option value={100}>100%</option>
              </select>
            </div>

            <div className="space-y-2">
              <p className="text-xs font-semibold text-stone-500 uppercase">Allowed Models</p>
              <div className="flex flex-wrap gap-2">
                <label className="flex items-center gap-1.5 text-xs text-stone-600">
                  <input
                    type="checkbox"
                    checked={agentForm.models.includes("*")}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setAgentForm({ ...agentForm, models: ["*"] });
                      } else {
                        setAgentForm({ ...agentForm, models: [] });
                      }
                    }}
                    className="rounded"
                  />
                  All models (*)
                </label>
                {models.map(m => (
                  <label key={m.name} className="flex items-center gap-1.5 text-xs text-stone-600">
                    <input
                      type="checkbox"
                      checked={agentForm.models.includes(m.name) || agentForm.models.includes("*")}
                      disabled={agentForm.models.includes("*")}
                      onChange={(e) => {
                        if (e.target.checked) {
                          setAgentForm({ ...agentForm, models: [...agentForm.models, m.name] });
                        } else {
                          setAgentForm({ ...agentForm, models: agentForm.models.filter(x => x !== m.name) });
                        }
                      }}
                      className="rounded"
                    />
                    {m.name}
                  </label>
                ))}
              </div>
            </div>

            <div className="space-y-2">
              <p className="text-xs font-semibold text-stone-500 uppercase">Allowed Providers</p>
              <div className="flex flex-wrap gap-2">
                <label className="flex items-center gap-1.5 text-xs text-stone-600">
                  <input
                    type="checkbox"
                    checked={agentForm.providers.includes("*")}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setAgentForm({ ...agentForm, providers: ["*"] });
                      } else {
                        setAgentForm({ ...agentForm, providers: [] });
                      }
                    }}
                    className="rounded"
                  />
                  All providers (*)
                </label>
                {ALL_PROVIDERS.map(p => (
                  <label key={p} className="flex items-center gap-1.5 text-xs text-stone-600">
                    <input
                      type="checkbox"
                      checked={agentForm.providers.includes(p) || agentForm.providers.includes("*")}
                      disabled={agentForm.providers.includes("*")}
                      onChange={(e) => {
                        if (e.target.checked) {
                          setAgentForm({ ...agentForm, providers: [...agentForm.providers, p] });
                        } else {
                          setAgentForm({ ...agentForm, providers: agentForm.providers.filter(x => x !== p) });
                        }
                      }}
                      className="rounded"
                    />
                    {p}
                  </label>
                ))}
              </div>
            </div>

            <div className="flex gap-2">
              <button
                onClick={async () => {
                  const updatedList = editingAgent
                    ? agentAccessList.map(a => a.agent === editingAgent ? agentForm : a)
                    : [...agentAccessList, agentForm];
                  setAgentAccessError("");
                  try {
                    await saveAgentAccess(updatedList);
                    queryClient.invalidateQueries({ queryKey: ["agent-access"] });
                    setShowAgentForm(false);
                    setEditingAgent(null);
                  } catch (error) {
                    setAgentAccessError(error instanceof Error ? error.message : "Failed to save agent access");
                  }
                }}
                className="btn-primary"
              >
                <Save className="h-4 w-4" /> {editingAgent ? "Save Changes" : "Add Agent"}
              </button>
              <button onClick={() => { setShowAgentForm(false); setEditingAgent(null); }} className="btn-secondary">Cancel</button>
            </div>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {(agentAccessList.length > 0 ? agentAccessList : [
            { agent: "research-bot", models: ["claude-haiku", "gpt-4o-mini"], providers: ["anthropic", "openai"], budget: 5.0, spend: 1.82, alert_threshold: 90 },
            { agent: "ops-bot", models: ["*"], providers: ["*"], budget: 50.0, spend: 12.40, alert_threshold: 90 },
            { agent: "devops-bot", models: ["claude-sonnet", "claude-haiku"], providers: ["anthropic", "bedrock"], budget: 25.0, spend: 8.15, alert_threshold: 80 },
            { agent: "gov-bot", models: ["*"], providers: ["bedrock"], budget: 100.0, spend: 3.20, alert_threshold: 90, note: "AWS only — no data leaves account" },
            { agent: "intern-bot", models: ["claude-haiku", "nova-lite"], providers: ["bedrock"], budget: 1.0, spend: 0.87, alert_threshold: 80 },
            { agent: "analytics-bot", models: ["claude-haiku", "gpt-4o-mini", "nova-lite"], providers: ["anthropic", "openai", "bedrock"], budget: 10.0, spend: 4.55, alert_threshold: 90 },
          ]).map((agent) => {
            const pct = (agent.spend / agent.budget) * 100;
            const barColor = pct >= 90 ? "bg-rose-500" : pct >= 70 ? "bg-amber-500" : "bg-emerald-500";
            return (
              <div key={agent.agent} className="card p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-semibold text-stone-800">{agent.agent}</p>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => {
                        setAgentForm({ ...agent, alert_threshold: agent.alert_threshold || 90 });
                        setEditingAgent(agent.agent);
                        setShowAgentForm(true);
                      }}
                      title="Edit"
                      className="rounded-lg p-1.5 text-stone-400 hover:bg-indigo-50 hover:text-indigo-600 transition"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={async () => {
                        const updatedList = agentAccessList.filter(a => a.agent !== agent.agent);
                        setAgentAccessError("");
                        try {
                          await saveAgentAccess(updatedList);
                          queryClient.invalidateQueries({ queryKey: ["agent-access"] });
                        } catch (error) {
                          setAgentAccessError(error instanceof Error ? error.message : "Failed to delete agent access");
                        }
                      }}
                      title="Delete"
                      className="rounded-lg p-1.5 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
                {agent.note && <p className="text-[10px] text-amber-600 font-medium">{agent.note}</p>}

                {/* Budget bar */}
                <div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] text-stone-400">${agent.spend.toFixed(2)} / ${agent.budget.toFixed(2)}</span>
                    <span className="text-[10px] text-stone-400">Alert: {agent.alert_threshold || 90}%</span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-stone-100 overflow-hidden mt-1">
                    <div className={`h-full rounded-full transition-all ${barColor}`} style={{ width: `${Math.min(100, pct)}%` }} />
                  </div>
                  <p className="text-[10px] text-stone-400 mt-0.5">{pct.toFixed(0)}% used · ${(agent.budget - agent.spend).toFixed(2)} remaining</p>
                </div>

                {/* Models */}
                <div className="flex items-center gap-1.5 flex-wrap">
                  <Brain className="h-3 w-3 text-stone-400" />
                  {agent.models.map(m => (
                    <span key={m} className="rounded-full bg-violet-50 text-violet-700 px-2 py-0.5 text-[10px] font-medium">{m}</span>
                  ))}
                </div>

                {/* Providers */}
                <div className="flex items-center gap-1.5 flex-wrap">
                  <Lock className="h-3 w-3 text-stone-400" />
                  {agent.providers.map(p => (
                    <span key={p} className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${PROVIDER_COLORS[p] || "bg-stone-100 text-stone-600"}`}>{p}</span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* LLM Routing Policy */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5 text-emerald-600" />
            <h2 className="text-lg font-semibold text-stone-900">LLM Routing Policy</h2>
          </div>
          <button
            onClick={async () => {
              setRoutingError("");
              try {
                await fetchAPI(`${GATEWAY_PATH}/config/llm`, {
                  method: "POST",
                  body: JSON.stringify({ routing_rules: routingRules }),
                });
                setRoutingSaved(true);
                setTimeout(() => setRoutingSaved(false), 2000);
              } catch (error) {
                setRoutingError(error instanceof Error ? error.message : "Failed to save routing policy");
              }
            }}
            className="btn-primary"
          >
            <Save className="h-4 w-4" /> Save Routing
            {routingSaved && <span className="ml-2 text-xs text-emerald-200">Saved!</span>}
          </button>
        </div>
        {routingError && <p className="text-xs font-medium text-rose-600">{routingError}</p>}
        <p className="text-xs text-stone-500">Configure routing strategies, provider fallback order, and weight distribution for A/B testing.</p>

        <div className="space-y-3">
          {routingRules.map((rule, ruleIdx) => (
            <div key={rule.model} className="card p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Brain className="h-4 w-4 text-violet-600" />
                  <p className="text-sm font-semibold text-stone-800">{rule.model}</p>
                </div>
                <select
                  value={rule.routing_strategy}
                  onChange={(e) => {
                    const updated = [...routingRules];
                    updated[ruleIdx] = { ...rule, routing_strategy: e.target.value };
                    setRoutingRules(updated);
                  }}
                  className="rounded-lg border border-stone-200 bg-white px-2 py-1 text-xs text-stone-700 cursor-pointer hover:border-indigo-300 focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-100"
                >
                  <option value="least-latency">Least Latency</option>
                  <option value="cost-optimized">Cost Optimized</option>
                  <option value="round-robin">Round Robin</option>
                  <option value="weighted">Weighted</option>
                    <option value="smart-routing">Smart Routing (task classification)</option>
                    <option value="ensemble">Ensemble (multi-model consensus)</option>
                </select>
              </div>

              {/* Provider list with fallback order and weights */}
              <div className="space-y-1.5">
                <p className="text-[10px] font-semibold text-stone-500 uppercase">Provider Fallback Order & Weights</p>
                {rule.providers
                  .sort((a, b) => a.fallback_order - b.fallback_order)
                  .map((prov, provIdx) => (
                  <div key={prov.provider + provIdx} className="flex items-center gap-2">
                    <span className="text-xs text-stone-400 w-4 text-center">{provIdx + 1}</span>
                    <span className={`badge text-xs ${PROVIDER_COLORS[prov.provider] || "bg-stone-100 text-stone-600"}`}>{prov.provider}</span>

                    {/* Weight slider */}
                    <div className="flex items-center gap-1 flex-1 ml-2">
                      <span className="text-[10px] text-stone-400 w-10">w: {prov.weight.toFixed(1)}</span>
                      <input
                        type="range"
                        min="0"
                        max="2"
                        step="0.1"
                        value={prov.weight}
                        onChange={(e) => {
                          const updated = [...routingRules];
                          const providers = [...updated[ruleIdx].providers];
                          providers[provIdx] = { ...providers[provIdx], weight: parseFloat(e.target.value) };
                          updated[ruleIdx] = { ...updated[ruleIdx], providers };
                          setRoutingRules(updated);
                        }}
                        className="flex-1 h-1.5 accent-violet-600"
                      />
                    </div>

                    {/* Reorder buttons */}
                    <div className="flex gap-0.5">
                      <button
                        disabled={provIdx === 0}
                        onClick={() => {
                          const updated = [...routingRules];
                          const providers = [...updated[ruleIdx].providers].sort((a, b) => a.fallback_order - b.fallback_order);
                          // Swap fallback_order with previous
                          const temp = providers[provIdx].fallback_order;
                          providers[provIdx] = { ...providers[provIdx], fallback_order: providers[provIdx - 1].fallback_order };
                          providers[provIdx - 1] = { ...providers[provIdx - 1], fallback_order: temp };
                          updated[ruleIdx] = { ...updated[ruleIdx], providers };
                          setRoutingRules(updated);
                        }}
                        className="rounded p-1 text-stone-400 hover:bg-stone-100 hover:text-stone-600 disabled:opacity-30 disabled:cursor-not-allowed transition"
                      >
                        <ArrowUp className="h-3 w-3" />
                      </button>
                      <button
                        disabled={provIdx === rule.providers.length - 1}
                        onClick={() => {
                          const updated = [...routingRules];
                          const providers = [...updated[ruleIdx].providers].sort((a, b) => a.fallback_order - b.fallback_order);
                          // Swap fallback_order with next
                          const temp = providers[provIdx].fallback_order;
                          providers[provIdx] = { ...providers[provIdx], fallback_order: providers[provIdx + 1].fallback_order };
                          providers[provIdx + 1] = { ...providers[provIdx + 1], fallback_order: temp };
                          updated[ruleIdx] = { ...updated[ruleIdx], providers };
                          setRoutingRules(updated);
                        }}
                        className="rounded p-1 text-stone-400 hover:bg-stone-100 hover:text-stone-600 disabled:opacity-30 disabled:cursor-not-allowed transition"
                      >
                        <ArrowDown className="h-3 w-3" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        {routingRules.length === 0 && (
          <div className="card p-8 flex flex-col items-center gap-2">
            <Server className="h-6 w-6 text-stone-300" />
            <p className="text-sm text-stone-500">No models configured for routing</p>
          </div>
        )}
      </div>

      {/* Budget Reset Schedule */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Clock className="h-5 w-5 text-amber-600" />
          <h2 className="text-lg font-semibold text-stone-900">Budget Reset Schedule</h2>
        </div>
        <p className="text-xs text-stone-500">Configure when agent budgets reset. Spend counters will be zeroed at the scheduled interval.</p>

        <div className="card p-4">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <label className="text-sm text-stone-600 font-medium">Reset Frequency:</label>
              <select
                value={budgetReset.schedule}
                onChange={(e) => setBudgetReset({ ...budgetReset, schedule: e.target.value as BudgetResetConfig["schedule"] })}
                className="input w-40"
              >
                <option value="manual">Manual</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </div>

            {budgetReset.schedule !== "manual" && (
              <div className="flex items-center gap-2 text-xs text-stone-500">
                <Clock className="h-3.5 w-3.5" />
                <span>
                  Next reset: {budgetReset.next_reset || (
                    budgetReset.schedule === "daily" ? "Tomorrow 00:00 UTC" :
                    budgetReset.schedule === "weekly" ? "Next Monday 00:00 UTC" :
                    "1st of next month 00:00 UTC"
                  )}
                </span>
              </div>
            )}

            <button
              onClick={async () => {
                setBudgetResetError("");
                try {
                  await fetchAPI(`${GATEWAY_PATH}/config/budget-reset`, {
                    method: "POST",
                    body: JSON.stringify(budgetReset),
                  });
                  queryClient.invalidateQueries({ queryKey: ["budget-reset"] });
                } catch (error) {
                  setBudgetResetError(error instanceof Error ? error.message : "Failed to save budget reset");
                }
              }}
              className="btn-primary ml-auto"
            >
              <Save className="h-4 w-4" /> Save Schedule
            </button>
          </div>
          {budgetResetError && <p className="mt-2 text-xs font-medium text-rose-600">{budgetResetError}</p>}
        </div>
      </div>
    </div>
  );
}
