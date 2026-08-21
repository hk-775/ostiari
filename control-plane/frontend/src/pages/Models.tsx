import { useState, useEffect } from "react";
import { Link, useLocation } from "react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Brain, Plus, Trash2, Pencil, X, Wrench, Eye, ShieldCheck, Save, Clock, RotateCcw, Upload } from "lucide-react";
import { api, fetchAPI } from "../lib/api";

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

interface TaskClassificationConfig {
  rules: Record<string, string[]>;
  model_mapping: Record<string, string>;
}

interface BudgetResetConfig {
  schedule: "manual" | "daily" | "weekly" | "monthly";
  last_reset_at?: string | null;
  configured_at?: string | null;
  next_reset?: string | null;
}

interface RoutingControls {
  gateway_id: string;
  task_classification: TaskClassificationConfig;
  budget_reset: BudgetResetConfig;
}

interface PushResult {
  models: number;
  gateways: number;
  pushed: number;
  failed: number;
  skipped: number;
}

async function fetchModels(): Promise<ModelConfig[]> {
  return fetchAPI<ModelConfig[]>("/api/models");
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
  google_ai: "bg-blue-50 text-blue-700",
  azure: "bg-sky-50 text-sky-700",
  xai: "bg-stone-100 text-stone-700",
  together: "bg-rose-50 text-rose-700",
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

function TaskClassificationRules({
  models,
  gatewayId,
  initial,
}: {
  models: ModelConfig[];
  gatewayId: string;
  initial?: TaskClassificationConfig;
}) {
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

  useEffect(() => {
    if (!initial) return;
    setRules(Object.keys(initial.rules).length ? initial.rules : DEFAULT_CLASSIFICATION_RULES);
    setModelMapping(Object.keys(initial.model_mapping).length ? initial.model_mapping : {
      code_generation: "claude-sonnet",
      creative: "claude-sonnet",
      analysis: "claude-opus",
      simple_qa: "claude-haiku",
      data: "gpt-4o-mini",
    });
  }, [gatewayId, initial]);

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
      const result = await fetchAPI<{ pushed: boolean; push_error?: string }>(
        `/api/routing-controls/${encodeURIComponent(gatewayId)}/task-classification`,
        {
        method: "PUT",
        body: JSON.stringify({ rules, model_mapping: modelMapping }),
        },
      );
      if (!result.pushed) {
        setSaveError(`Saved, but gateway push failed: ${result.push_error || "unknown error"}`);
        return;
      }
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
  const { data: gateways = [] } = useQuery({ queryKey: ["gateways"], queryFn: api.gateways.list });
  const [gatewayId, setGatewayId] = useState("");
  const selectedGateway = gatewayId || gateways[0]?.id || "";
  const { data: controls } = useQuery({
    queryKey: ["routing-controls", selectedGateway],
    queryFn: () => fetchAPI<RoutingControls>(
      `/api/routing-controls/${encodeURIComponent(selectedGateway)}`,
    ),
    enabled: Boolean(selectedGateway),
  });
  const [showForm, setShowForm] = useState(false);
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [form, setForm] = useState<ModelConfig>({ ...EMPTY_FORM });
  const [modelError, setModelError] = useState("");
  const [pushStatus, setPushStatus] = useState("");

  useEffect(() => { setShowForm(false); setEditingModel(null); }, [location.key]);

  // Budget reset state
  const [budgetReset, setBudgetReset] = useState<BudgetResetConfig>({ schedule: "manual" });
  const [budgetResetError, setBudgetResetError] = useState("");

  // Sync budget reset from fetched config
  useEffect(() => {
    if (controls?.budget_reset) {
      setBudgetReset(controls.budget_reset);
    }
  }, [controls]);

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
        <div className="flex items-center gap-2">
          {pushStatus && <span className="text-xs text-stone-600">{pushStatus}</span>}
          <button
            onClick={async () => {
              setPushStatus("");
              try {
                const result = await fetchAPI<PushResult>("/api/models/push", { method: "POST" });
                setPushStatus(
                  result.failed
                    ? `${result.pushed}/${result.gateways - result.skipped} LLM gateways updated`
                    : result.pushed
                      ? `${result.models} models pushed to ${result.pushed} gateway${result.pushed === 1 ? "" : "s"}`
                      : "No LLM gateways available",
                );
              } catch (error) {
                setPushStatus(error instanceof Error ? error.message : "Registry push failed");
              }
            }}
            className="btn-secondary"
          >
            <Upload className="h-4 w-4" /> Push Registry
          </button>
          <button onClick={() => { setForm({ ...EMPTY_FORM }); setEditingModel(null); setShowForm(true); }} className="btn-indigo">
            <Plus className="h-4 w-4" /> Add Model
          </button>
        </div>
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
              <div key={i} className="flex flex-wrap gap-2 items-center">
                <select value={p.provider} onChange={(e) => { const providers = [...form.providers]; providers[i] = { ...p, provider: e.target.value }; setForm({ ...form, providers }); }} className="input w-36">
                  <option value="anthropic">Anthropic</option>
                  <option value="openai">OpenAI</option>
                  <option value="bedrock">Bedrock</option>
                  <option value="vertex">Vertex AI</option>
                  <option value="google_ai">Google AI</option>
                  <option value="bedrock-mantle">Bedrock Mantle</option>
                  <option value="azure">Azure OpenAI</option>
                  <option value="cohere">Cohere</option>
                  <option value="xai">xAI</option>
                  <option value="groq">Groq</option>
                  <option value="together">Together</option>
                  <option value="fireworks">Fireworks</option>
                  <option value="ai21">AI21</option>
                </select>
                <input placeholder="Model ID" value={p.model_id} onChange={(e) => { const providers = [...form.providers]; providers[i] = { ...p, model_id: e.target.value }; setForm({ ...form, providers }); }} className="input min-w-48 flex-1" />
                <input type="number" step="0.1" placeholder="Weight" value={p.weight} onChange={(e) => { const providers = [...form.providers]; providers[i] = { ...p, weight: parseFloat(e.target.value) || 1 }; setForm({ ...form, providers }); }} className="input w-20" />
                <input
                  type="number"
                  min="0"
                  step="1"
                  aria-label="Fallback order"
                  title="Fallback order"
                  value={p.fallback_order}
                  onChange={(e) => {
                    const providers = [...form.providers];
                    providers[i] = {
                      ...p,
                      fallback_order: Math.max(0, parseInt(e.target.value, 10) || 0),
                    };
                    setForm({ ...form, providers });
                  }}
                  className="input w-20"
                />
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

      {/* Per-Agent Model Access */}
      <div
        id="per-agent-access"
        className="flex flex-wrap items-center justify-between gap-3 border-y border-stone-200 py-4 scroll-mt-16"
      >
        <div className="flex items-center gap-3">
          <ShieldCheck className="h-5 w-5 text-violet-700" />
          <div>
            <h2 className="text-base font-semibold text-stone-900">Per-Agent Model Access</h2>
            <p className="text-xs text-stone-500">Managed with each agent's runtime quota.</p>
          </div>
        </div>
        <Link to="/agent-quotas" className="btn-primary">
          <ShieldCheck className="h-4 w-4" /> Manage Agent Quotas
        </Link>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Brain className="h-5 w-5 text-indigo-600" />
            <h2 className="text-lg font-semibold text-stone-900">Gateway Routing Controls</h2>
          </div>
        </div>
        <select
          value={selectedGateway}
          onChange={(event) => setGatewayId(event.target.value)}
          className="input w-56"
        >
          {gateways.map((gateway) => (
            <option key={gateway.id} value={gateway.id}>{gateway.name}</option>
          ))}
        </select>
      </div>

      {selectedGateway && (
        <TaskClassificationRules
          models={models}
          gatewayId={selectedGateway}
          initial={controls?.task_classification}
        />
      )}

      {/* Budget Reset Schedule */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Clock className="h-5 w-5 text-amber-600" />
          <h2 className="text-lg font-semibold text-stone-900">Budget Reset Schedule</h2>
        </div>

        <div className="card p-4">
          <div className="flex flex-wrap items-center gap-4">
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
                  Next reset: {budgetReset.next_reset
                    ? new Date(budgetReset.next_reset).toLocaleString()
                    : "Pending schedule save"}
                </span>
              </div>
            )}

            <button
              onClick={async () => {
                setBudgetResetError("");
                try {
                  await fetchAPI(
                    `/api/routing-controls/${encodeURIComponent(selectedGateway)}/reset-spend`,
                    { method: "POST" },
                  );
                  await queryClient.invalidateQueries({ queryKey: ["routing-controls", selectedGateway] });
                } catch (error) {
                  setBudgetResetError(error instanceof Error ? error.message : "Budget reset failed");
                }
              }}
              disabled={!selectedGateway}
              className="btn-secondary ml-auto"
              title="Reset spend now"
            >
              <RotateCcw className="h-4 w-4" /> Reset Now
            </button>
            <button
              onClick={async () => {
                setBudgetResetError("");
                try {
                  const result = await fetchAPI<{
                    pushed: boolean;
                    push_error?: string;
                    config: BudgetResetConfig;
                  }>(
                    `/api/routing-controls/${encodeURIComponent(selectedGateway)}/budget-reset`,
                    {
                      method: "PUT",
                      body: JSON.stringify({ schedule: budgetReset.schedule }),
                    },
                  );
                  setBudgetReset(result.config);
                  if (!result.pushed) {
                    setBudgetResetError(
                      `Saved, but gateway push failed: ${result.push_error || "unknown error"}`,
                    );
                  }
                  await queryClient.invalidateQueries({ queryKey: ["routing-controls", selectedGateway] });
                } catch (error) {
                  setBudgetResetError(error instanceof Error ? error.message : "Failed to save budget reset");
                }
              }}
              disabled={!selectedGateway}
              className="btn-primary"
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
