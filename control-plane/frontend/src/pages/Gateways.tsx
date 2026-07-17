import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Upload, Server, Check, EyeOff, ShieldCheck } from "lucide-react";
import { api, Gateway } from "../lib/api";

function GatewayHealthDot({ gateway }: { gateway: Gateway }) {
  const getHealth = () => {
    if (gateway.status === "healthy") {
      return { color: "bg-emerald-500", label: "healthy" };
    }
    if (gateway.status === "unhealthy" || gateway.status === "unreachable") {
      return { color: "bg-red-500", label: "unhealthy" };
    }
    // "registered" or never heartbeated
    return { color: "bg-stone-400", label: "registered" };
  };
  const { color, label } = getHealth();
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`inline-block h-2 w-2 rounded-full ${color}`} />
      <span className="text-sm text-stone-600">{label}</span>
    </span>
  );
}

export function Gateways() {
  const queryClient = useQueryClient();
  const { data: gateways = [], isLoading } = useQuery({ queryKey: ["gateways"], queryFn: api.gateways.list });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ id: "", name: "", endpoint: "", description: "" });

  const createMutation = useMutation({
    mutationFn: api.gateways.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["gateways"] });
      setShowForm(false);
      setForm({ id: "", name: "", endpoint: "", description: "" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: api.gateways.delete,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["gateways"] }),
  });

  const [pushStatus, setPushStatus] = useState<Record<string, string>>({});

  const pushMutation = useMutation({
    mutationFn: async (id: string) => {
      setPushStatus((prev) => ({ ...prev, [id]: "pushing" }));
      try {
        const result = await api.gateways.push(id);
        setPushStatus((prev) => ({ ...prev, [id]: "done" }));
        setTimeout(() => setPushStatus((prev) => ({ ...prev, [id]: "" })), 2000);
        return result;
      } catch {
        setPushStatus((prev) => ({ ...prev, [id]: "error" }));
        setTimeout(() => setPushStatus((prev) => ({ ...prev, [id]: "" })), 2000);
      }
    },
  });
  const pushAllMutation = useMutation({ mutationFn: api.gateways.pushAll });

  const modeMutation = useMutation({
    mutationFn: ({ id, mode }: { id: string; mode: "enforce" | "shadow" }) =>
      api.gateways.setMode(id, mode),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["gateways"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-stone-900">Agent Gateways</h1>
          <p className="mt-1 text-sm text-stone-500">Register and manage your agent gateway fleet</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => pushAllMutation.mutate()} className="btn-secondary">
            <Upload className="h-4 w-4" /> Push All
          </button>
          <button onClick={() => setShowForm(true)} className="btn-sky">
            <Plus className="h-4 w-4" /> Register
          </button>
        </div>
      </div>

      {showForm && (
        <form onSubmit={(e) => { e.preventDefault(); createMutation.mutate(form); }} className="card p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Gateway ID" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} className="input" />
            <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" />
            <input placeholder="Endpoint URL (http://...)" value={form.endpoint} onChange={(e) => setForm({ ...form, endpoint: e.target.value })} className="input col-span-2" />
          </div>
          <div className="flex gap-2">
            <button type="submit" className="btn-sky">Register</button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">Cancel</button>
          </div>
        </form>
      )}

      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold text-stone-500 uppercase tracking-wider">
              <th className="px-6 py-3.5">Name</th>
              <th className="px-6 py-3.5">Endpoint</th>
              <th className="px-6 py-3.5">Status</th>
              <th className="px-6 py-3.5">Mode</th>
              <th className="px-6 py-3.5">Tools</th>
              <th className="px-6 py-3.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {gateways.map((s) => (
              <tr key={s.id} className="transition hover:bg-stone-50/50">
                <td className="px-6 py-4">
                  <p className="text-sm font-medium text-stone-800">{s.name}</p>
                </td>
                <td className="px-6 py-4 text-sm text-stone-500 font-mono">{s.endpoint}</td>
                <td className="px-6 py-4">
                  <GatewayHealthDot gateway={s} />
                </td>
                <td className="px-6 py-4">
                  <button
                    onClick={() => modeMutation.mutate({ id: s.id, mode: s.mode === "shadow" ? "enforce" : "shadow" })}
                    disabled={modeMutation.isPending}
                    title={s.mode === "shadow"
                      ? "Shadow: policies evaluate but never block. Click to enforce."
                      : "Enforce: policies are applied. Click to switch to shadow."}
                    className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition ${
                      s.mode === "shadow"
                        ? "bg-amber-50 text-amber-700 hover:bg-amber-100"
                        : "bg-emerald-50 text-emerald-700 hover:bg-emerald-100"
                    }`}
                  >
                    {s.mode === "shadow" ? <EyeOff className="h-3.5 w-3.5" /> : <ShieldCheck className="h-3.5 w-3.5" />}
                    {s.mode === "shadow" ? "Shadow" : "Enforce"}
                  </button>
                </td>
                <td className="px-6 py-4 text-sm text-stone-500">{s.tools_count}</td>
                <td className="px-6 py-4">
                  <div className="flex justify-end gap-1">
                    <button onClick={() => pushMutation.mutate(s.id)} title="Push config" className="rounded-lg p-2 text-stone-400 hover:bg-violet-50 hover:text-violet-600 transition">
                      {pushStatus[s.id] === "done" ? <Check className="h-4 w-4 text-emerald-600" /> : <Upload className="h-4 w-4" />}
                    </button>
                    <button onClick={() => deleteMutation.mutate(s.id)} title="Delete" className="rounded-lg p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {gateways.length === 0 && !isLoading && (
          <div className="flex flex-col items-center gap-3 py-12">
            <Server className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-400">No agent gateways registered yet</p>
          </div>
        )}
      </div>
    </div>
  );
}
