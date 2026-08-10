import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Upload, FileText, Pencil, Check, Clock } from "lucide-react";
import { api, Policy } from "../lib/api";

export function Policies() {
  const queryClient = useQueryClient();
  const { data: policies = [] } = useQuery({ queryKey: ["policies"], queryFn: api.policies.list });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: "", content: "" });
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editContent, setEditContent] = useState("");
  const [editError, setEditError] = useState("");
  const [pushStatus, setPushStatus] = useState<Record<number, string>>({});

  const createMutation = useMutation({
    mutationFn: (data: { name: string; content: string }) =>
      api.policies.create({ name: data.name, content: JSON.parse(data.content) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["policies"] });
      setShowForm(false);
      setForm({ name: "", content: "" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: api.policies.delete,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["policies"] }),
  });
  const mutationError = createMutation.error ?? deleteMutation.error;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Tool Policies</h1>
          <p className="mt-1 text-sm text-stone-500">Per-tool allow/block rules, risk scoring, and thresholds</p>
        </div>
        <button onClick={() => setShowForm(true)} className="btn-rose">
          <Plus className="h-4 w-4" /> New Policy
        </button>
      </div>
      {mutationError && (
        <p className="text-sm font-medium text-rose-600">
          {mutationError instanceof Error ? mutationError.message : "Policy request failed"}
        </p>
      )}

      {showForm && (
        <form
          onSubmit={(e) => { e.preventDefault(); createMutation.mutate(form); }}
          className="card p-6 space-y-4"
        >
          <input placeholder="Policy name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input w-full" />
          <textarea
            placeholder='{"block": ["*.delete"], "allow": ["db_query"], "rules": []}'
            value={form.content}
            onChange={(e) => setForm({ ...form, content: e.target.value })}
            rows={6}
            className="input w-full font-mono"
          />
          <div className="flex gap-2">
            <button type="submit" className="btn-rose">Create</button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">Cancel</button>
          </div>
        </form>
      )}

      <div className="grid gap-4">
        {policies.map((p) => (
          <div key={p.id} className="card p-6">
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-violet-50">
                  <FileText className="h-4 w-4 text-violet-600" />
                </div>
                <div>
                  <p className="text-sm font-medium text-stone-900">{p.name}</p>
                  <p className="text-xs text-stone-500">
                    {p.gateway_id ? `${p.gateway_id.replace("-agent","").split("-").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ").replace("Devops","DevOps").replace("Crm","CRM") + " Gateway"}` : "Global policy"}
                    {" · "}
                    <span className={p.is_active ? "text-emerald-600" : "text-stone-400"}>{p.is_active ? "Active" : "Inactive"}</span>
                  </p>
                </div>
              </div>
              <div className="flex gap-1">
                <button onClick={() => { setEditingId(editingId === p.id ? null : p.id); setEditContent(JSON.stringify(p.content, null, 2)); setEditError(""); }} title="Edit" className="rounded-xl p-2 text-stone-400 hover:bg-indigo-50 hover:text-indigo-600 transition">
                  <Pencil className="h-4 w-4" />
                </button>
                <button onClick={async () => {
                  const gatewayId = p.gateway_id || "";
                  if (!gatewayId) {
                    setPushStatus(prev => ({ ...prev, [p.id]: "error" }));
                    setTimeout(() => setPushStatus(prev => ({ ...prev, [p.id]: "" })), 2000);
                    return;
                  }
                  setPushStatus(prev => ({ ...prev, [p.id]: "pushing" }));
                  try {
                    const res = await api.gateways.pushConfig(gatewayId, { policy: p.content });
                    if (res.status === "applied") {
                      setPushStatus(prev => ({ ...prev, [p.id]: "done" }));
                    } else if (res.status === "queued") {
                      setPushStatus(prev => ({ ...prev, [p.id]: "queued" }));
                    } else {
                      setPushStatus(prev => ({ ...prev, [p.id]: "error" }));
                    }
                    setTimeout(() => setPushStatus(prev => ({ ...prev, [p.id]: "" })), 3000);
                  } catch {
                    setPushStatus(prev => ({ ...prev, [p.id]: "error" }));
                    setTimeout(() => setPushStatus(prev => ({ ...prev, [p.id]: "" })), 2000);
                  }
                }} title="Push to gateway" className="rounded-xl p-2 text-stone-400 hover:bg-violet-50 hover:text-violet-600 transition">
                  {pushStatus[p.id] === "done" ? <Check className="h-4 w-4 text-emerald-600" /> :
                   pushStatus[p.id] === "queued" ? <Clock className="h-4 w-4 text-amber-500" /> :
                   <Upload className="h-4 w-4" />}
                </button>
                <button onClick={() => deleteMutation.mutate(p.id)} title="Delete" className="rounded-xl p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
            {editingId === p.id ? (
              <div className="mt-3 space-y-2">
                <textarea value={editContent} onChange={(e) => setEditContent(e.target.value)} rows={8} className="input w-full font-mono text-xs" />
                {editError && <p className="text-xs font-medium text-rose-600">{editError}</p>}
                <div className="flex gap-2">
                  <button onClick={async () => {
                    setEditError("");
                    try {
                      await api.policies.update(p.id, {
                        name: p.name,
                        content: JSON.parse(editContent),
                      });
                      queryClient.invalidateQueries({ queryKey: ["policies"] });
                      setEditingId(null);
                    } catch (error) {
                      setEditError(error instanceof Error ? error.message : "Failed to update policy");
                    }
                  }} className="btn-primary text-xs">Save</button>
                  <button onClick={() => setEditingId(null)} className="btn-secondary text-xs">Cancel</button>
                </div>
              </div>
            ) : (
              <pre className="mt-3 overflow-x-auto rounded-xl bg-stone-50 p-3 text-xs text-stone-600 border border-stone-100">
                {JSON.stringify(p.content, null, 2)}
              </pre>
            )}
          </div>
        ))}
        {policies.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-12">
            <FileText className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No policies defined yet</p>
          </div>
        )}
      </div>
    </div>
  );
}
