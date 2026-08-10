import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Plus, Trash2, ToggleLeft, ToggleRight } from "lucide-react";
import { fetchAPI } from "../lib/api";

interface Experiment {
  name: string;
  model_a: string;
  model_b: string;
  traffic_pct_b: number;
  gateway_id: string;
  enabled: boolean;
}

interface ExperimentResults {
  experiment_name: string;
  period_days: number;
  model_a: { model: string; requests: number; total_tokens: number; total_cost: number; avg_tokens: number; avg_cost: number };
  model_b: { model: string; requests: number; total_tokens: number; total_cost: number; avg_tokens: number; avg_cost: number };
}

async function fetchExperiments(): Promise<Experiment[]> {
  return fetchAPI<Experiment[]>("/api/experiments");
}

async function fetchResults(name: string): Promise<ExperimentResults> {
  return fetchAPI<ExperimentResults>(`/api/experiments/${name}/results`);
}

export function Experiments() {
  const queryClient = useQueryClient();
  const { data: experiments = [] } = useQuery({ queryKey: ["experiments"], queryFn: fetchExperiments });
  const [showForm, setShowForm] = useState(false);
  const [selectedExp, setSelectedExp] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", model_a: "", model_b: "", traffic_pct_b: "10", gateway_id: "" });

  const { data: results } = useQuery({
    queryKey: ["experiment-results", selectedExp],
    queryFn: () => fetchResults(selectedExp!),
    enabled: !!selectedExp,
  });

  const createMutation = useMutation({
    mutationFn: (data: typeof form) =>
      fetchAPI<Experiment>("/api/experiments", {
        method: "POST",
        body: JSON.stringify({ ...data, traffic_pct_b: parseInt(data.traffic_pct_b) }),
      }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["experiments"] }); setShowForm(false); },
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => fetchAPI(`/api/experiments/${name}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["experiments"] }),
  });

  const toggleMutation = useMutation({
    mutationFn: (name: string) => fetchAPI(`/api/experiments/${name}/toggle`, { method: "PATCH" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["experiments"] }),
  });
  const mutationError = createMutation.error ?? deleteMutation.error ?? toggleMutation.error;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">A/B Experiments</h1>
          <p className="mt-1 text-sm text-stone-500">Split traffic between models and compare performance</p>
        </div>
        <button onClick={() => setShowForm(true)} className="btn-pink">
          <Plus className="h-4 w-4" /> New Experiment
        </button>
      </div>
      {mutationError && (
        <p className="text-sm font-medium text-rose-600">
          {mutationError instanceof Error ? mutationError.message : "Experiment request failed"}
        </p>
      )}

      {showForm && (
        <form onSubmit={(e) => { e.preventDefault(); createMutation.mutate(form); }} className="card p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Experiment name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" />
            <input placeholder="Gateway ID" value={form.gateway_id} onChange={(e) => setForm({ ...form, gateway_id: e.target.value })} className="input" />
            <input placeholder="Model A (control)" value={form.model_a} onChange={(e) => setForm({ ...form, model_a: e.target.value })} className="input" />
            <input placeholder="Model B (challenger)" value={form.model_b} onChange={(e) => setForm({ ...form, model_b: e.target.value })} className="input" />
          </div>
          <div className="flex items-center gap-3">
            <label className="text-sm text-stone-500">Traffic to Model B:</label>
            <input type="range" min="1" max="99" value={form.traffic_pct_b} onChange={(e) => setForm({ ...form, traffic_pct_b: e.target.value })} className="flex-1 accent-violet-600" />
            <span className="w-12 text-center text-sm font-medium text-stone-900">{form.traffic_pct_b}%</span>
          </div>
          <div className="flex gap-2">
            <button type="submit" className="btn-pink">Create</button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">Cancel</button>
          </div>
        </form>
      )}

      <div className="grid gap-4">
        {experiments.map((exp) => (
          <div key={exp.name} className={`card p-6 ${!exp.enabled ? "opacity-60" : ""}`}>
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-pink-50">
                  <FlaskConical className="h-4 w-4 text-pink-600" />
                </div>
                <div>
                  <p className="text-sm font-medium text-stone-900">{exp.name}</p>
                  <p className="text-xs text-stone-500">Gateway: {exp.gateway_id.replace("-agent","").split("-").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ").replace("Devops","DevOps").replace("Crm","CRM") + " Gateway"}</p>
                </div>
              </div>
              <div className="flex items-center gap-1">
                <button onClick={() => toggleMutation.mutate(exp.name)} title={exp.enabled ? "Disable" : "Enable"} className="rounded-xl p-2 text-stone-400 hover:bg-stone-50 transition">
                  {exp.enabled ? <ToggleRight className="h-5 w-5 text-emerald-500" /> : <ToggleLeft className="h-5 w-5" />}
                </button>
                <button onClick={() => setSelectedExp(selectedExp === exp.name ? null : exp.name)} className="btn-secondary text-xs px-2 py-1">Results</button>
                <button onClick={() => deleteMutation.mutate(exp.name)} className="rounded-xl p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>

            <div className="mt-4 flex items-center gap-2">
              <div className="flex-1 rounded-xl bg-stone-50 border border-stone-100 p-3">
                <p className="text-xs text-stone-500">Model A (control)</p>
                <p className="text-sm font-medium text-stone-900 mt-0.5">{exp.model_a}</p>
                <p className="text-xs text-stone-400 mt-0.5">{100 - exp.traffic_pct_b}% traffic</p>
              </div>
              <span className="text-stone-400 text-sm">vs</span>
              <div className="flex-1 rounded-xl bg-stone-50 border border-stone-100 p-3">
                <p className="text-xs text-stone-500">Model B (challenger)</p>
                <p className="text-sm font-medium text-stone-900 mt-0.5">{exp.model_b}</p>
                <p className="text-xs text-stone-400 mt-0.5">{exp.traffic_pct_b}% traffic</p>
              </div>
            </div>

            {selectedExp === exp.name && results && (
              <div className="mt-4 rounded-xl border border-stone-200 bg-stone-50 p-4">
                <h4 className="text-xs font-medium text-stone-500 mb-3">Results (last {results.period_days} days)</h4>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-stone-500">
                      <th className="text-left pb-2">Metric</th>
                      <th className="text-right pb-2">{results.model_a.model}</th>
                      <th className="text-right pb-2">{results.model_b.model}</th>
                    </tr>
                  </thead>
                  <tbody className="text-stone-700">
                    <tr><td>Requests</td><td className="text-right">{results.model_a.requests}</td><td className="text-right">{results.model_b.requests}</td></tr>
                    <tr><td>Avg Tokens</td><td className="text-right">{results.model_a.avg_tokens}</td><td className="text-right">{results.model_b.avg_tokens}</td></tr>
                    <tr><td>Avg Cost</td><td className="text-right">${results.model_a.avg_cost.toFixed(5)}</td><td className="text-right">${results.model_b.avg_cost.toFixed(5)}</td></tr>
                    <tr><td>Total Cost</td><td className="text-right">${results.model_a.total_cost.toFixed(4)}</td><td className="text-right">${results.model_b.total_cost.toFixed(4)}</td></tr>
                  </tbody>
                </table>
                {results.model_a.requests === 0 && results.model_b.requests === 0 && (
                  <p className="mt-2 text-xs text-stone-500">No data yet. Results appear after traffic flows through the experiment.</p>
                )}
              </div>
            )}
          </div>
        ))}
        {experiments.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-12">
            <FlaskConical className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No experiments yet. Create one to start comparing models.</p>
          </div>
        )}
      </div>
    </div>
  );
}
