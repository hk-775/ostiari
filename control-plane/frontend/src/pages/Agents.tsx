import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Bot, Trash2, Plus, Route, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { fetchAPI } from "../lib/api";

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
  return fetchAPI<AgentConfig[]>("/api/agents");
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

// Preset frameworks offered in the dropdown. Anything else is entered via
// "Other…" as free text — the backend accepts any framework string.
const PRESET_FRAMEWORKS = [
  "openai", "anthropic", "strands", "bedrock",
  "agentcore", "crewai", "langgraph", "gateway-invoke",
];

interface RoutingPolicy {
  agent_id: string;
  gateway_id: string;
  strategy: "round_robin";
  models: string[];
  scope: "request" | "session";
}

function LLMRoundRobinSection({ agents }: { agents: AgentConfig[] }) {
  const queryClient = useQueryClient();
  const { data: policies = [] } = useQuery({
    queryKey: ["agent-routing"],
    queryFn: () => fetchAPI<RoutingPolicy[]>("/api/agent-routing"),
  });
  const { data: availableModels = [] } = useQuery({
    queryKey: ["model-config"],
    queryFn: () => fetchAPI<{ name: string }[]>("/api/models"),
  });
  const [agentId, setAgentId] = useState("");
  const [scope, setScope] = useState<"request" | "session">("session");
  const [models, setModels] = useState<string[]>([]);
  const [nextModel, setNextModel] = useState("");
  const [status, setStatus] = useState("");

  const agent = agentId || agents[0]?.name || "";
  const gw = agents.find(a => a.name === agent)?.gateway_id || "";
  const current = policies.find(
    (policy) => policy.agent_id === agent && policy.gateway_id === gw,
  );

  useEffect(() => {
    setScope(current?.scope || "session");
    setModels(current?.models || []);
    setStatus("");
  }, [agent, current]);

  const save = async () => {
    setStatus("");
    if (models.length < 1) { setStatus("Add at least one model."); return; }
    try {
      const d = await fetchAPI<{ pushed: boolean; push_error?: string }>("/api/agent-routing", {
        method: "POST",
        body: JSON.stringify({ agent_id: agent, gateway_id: gw, strategy: "round_robin", models, scope }),
      });
      await queryClient.invalidateQueries({ queryKey: ["agent-routing"] });
      setStatus(d.pushed ? "Saved and pushed" : `Saved; push failed: ${d.push_error}`);
    } catch (e) {
      setStatus(`Error: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const remove = async () => {
    setStatus("");
    try {
      const result = await fetchAPI<{ pushed: boolean; push_error?: string }>(
        `/api/agent-routing/${encodeURIComponent(gw)}/${encodeURIComponent(agent)}`,
        { method: "DELETE" },
      );
      await queryClient.invalidateQueries({ queryKey: ["agent-routing"] });
      setModels([]);
      setStatus(result.pushed ? "Policy removed" : `Removed; push failed: ${result.push_error}`);
    } catch (error) {
      setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Route className="h-5 w-5 text-emerald-600" />
        <h2 className="text-lg font-semibold text-stone-900">Per-Agent Model Routing</h2>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label className="text-[10px] font-semibold text-stone-400 uppercase">Agent</label>
          <select value={agent} onChange={(e) => setAgentId(e.target.value)}
                  className="mt-0.5 w-full rounded-lg border border-stone-200 bg-white px-2 py-1.5 text-xs">
            {agents.map(a => <option key={a.name} value={a.name}>{a.name}</option>)}
          </select>
        </div>
        <div>
          <label className="text-[10px] font-semibold text-stone-400 uppercase">Gateway</label>
          <input value={gw} readOnly
                 className="mt-0.5 w-full rounded-lg border border-stone-200 px-2 py-1.5 text-xs font-mono" />
        </div>
        <div>
          <label className="text-[10px] font-semibold text-stone-400 uppercase">Scope</label>
          <select value={scope} onChange={(e) => setScope(e.target.value as "request" | "session")}
                  className="mt-0.5 w-full rounded-lg border border-stone-200 bg-white px-2 py-1.5 text-xs">
            <option value="session">Per session (sticky per conversation)</option>
            <option value="request">Per request (rotate every call)</option>
          </select>
        </div>
      </div>
      <div>
        <label className="text-[10px] font-semibold text-stone-400 uppercase">Models (rotation order)</label>
        <div className="flex flex-wrap gap-1 mt-1">
          {models.map((m, i) => (
            <span key={m} className="inline-flex items-center gap-0.5 rounded-full bg-emerald-50 text-emerald-700 px-2 py-0.5 text-[10px] font-medium">
              {i + 1}. {m}
              <button onClick={() => setModels(models.filter((_, j) => j !== i))} className="ml-0.5 hover:text-rose-600">×</button>
            </span>
          ))}
        </div>
        <div className="mt-1 flex gap-2">
          <select
            value={nextModel}
            onChange={(event) => setNextModel(event.target.value)}
            className="input flex-1"
          >
            <option value="">Select model</option>
            {availableModels
              .filter((model) => !models.includes(model.name))
              .map((model) => (
                <option key={model.name} value={model.name}>{model.name}</option>
              ))}
          </select>
          <button
            onClick={() => {
              if (!nextModel) return;
              setModels([...models, nextModel]);
              setNextModel("");
            }}
            className="btn-secondary"
          >
            <Plus className="h-4 w-4" /> Add
          </button>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button onClick={save} className="btn-primary text-xs"><Save className="h-3.5 w-3.5" /> Save & Push</button>
        {current && (
          <button onClick={remove} className="btn-secondary text-xs text-rose-600">
            <Trash2 className="h-3.5 w-3.5" /> Remove
          </button>
        )}
        {status && <span className="text-xs text-stone-600">{status}</span>}
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
    mutationFn: (data: typeof form) =>
      fetchAPI<AgentConfig>("/api/agents", {
        method: "POST",
        body: JSON.stringify({ ...data, tools: data.tools.split(",").map(t => t.trim()).filter(Boolean) }),
      }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["agents"] }); setShowForm(false); },
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => fetchAPI(`/api/agents/${name}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agents"] }),
  });
  const mutationError = createMutation.error ?? deleteMutation.error;

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
      {mutationError && (
        <p className="text-sm font-medium text-rose-600">
          {mutationError instanceof Error ? mutationError.message : "Agent request failed"}
        </p>
      )}

      {showForm && (
        <form onSubmit={(e) => { e.preventDefault(); createMutation.mutate(form); }} className="card p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Agent name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" />
            <select
              value={PRESET_FRAMEWORKS.includes(form.framework) ? form.framework : "__other__"}
              onChange={(e) => setForm({ ...form, framework: e.target.value === "__other__" ? "" : e.target.value })}
              className="input"
            >
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="strands">Strands</option>
              <option value="bedrock">Bedrock</option>
              <option value="agentcore">AgentCore</option>
              <option value="crewai">CrewAI</option>
              <option value="langgraph">LangGraph</option>
              <option value="gateway-invoke">Gateway Invoke</option>
              <option value="__other__">Other…</option>
            </select>
            {!PRESET_FRAMEWORKS.includes(form.framework) && (
              <input
                placeholder="Framework name"
                value={form.framework}
                onChange={(e) => setForm({ ...form, framework: e.target.value })}
                className="input col-span-2"
              />
            )}
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

      {agents.length > 0 && (
        <LLMRoundRobinSection agents={agents} />
      )}
    </div>
  );
}
