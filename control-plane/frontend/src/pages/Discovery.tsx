import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Radar, ShieldAlert, ShieldCheck, Ghost, Plus } from "lucide-react";
import { api, type DiscoveredAgent } from "../lib/api";

const STATUS: Record<string, { label: string; cls: string; icon: any }> = {
  discovered:      { label: "Shadow — ungoverned", cls: "bg-rose-50 text-rose-700",    icon: ShieldAlert },
  governed:        { label: "Governed",            cls: "bg-emerald-50 text-emerald-700", icon: ShieldCheck },
  governed_unseen: { label: "Registered, unseen",  cls: "bg-stone-100 text-stone-500",  icon: Ghost },
};

export function Discovery() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["discovery"], queryFn: api.discovery.agents, refetchInterval: 5000 });

  const onboard = useMutation({
    mutationFn: (a: DiscoveredAgent) =>
      api.discovery.onboard(a.agent_id, a.gateways[0] || "crm-agent"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["discovery"] }),
  });

  const s = data?.summary;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
          <Radar className="h-6 w-6 text-violet-500" /> Agent Discovery
        </h1>
        <p className="mt-1 text-sm text-stone-500">
          Agents correlated across signals we can observe — gateway traffic and cloud signals —
          reconciled against the agents we govern. <span className="font-medium">Shadow AI</span> is
          what's running but never registered. (Sources: {s?.sources?.join(" · ") || "…"})
        </p>
      </div>

      {/* Summary */}
      <div className="grid grid-cols-3 gap-4">
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Shadow (ungoverned)</p>
          <p className="mt-2 text-3xl font-bold text-rose-600">{s?.shadow ?? 0}</p>
          <p className="mt-1 text-xs text-stone-400">seen, not registered</p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Governed</p>
          <p className="mt-2 text-3xl font-bold text-emerald-600">{s?.governed ?? 0}</p>
          <p className="mt-1 text-xs text-stone-400">seen and registered</p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Registered, unseen</p>
          <p className="mt-2 text-3xl font-bold text-stone-700">{s?.stale ?? 0}</p>
          <p className="mt-1 text-xs text-stone-400">stale? decommissioned?</p>
        </div>
      </div>

      {/* Agents table */}
      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-100 text-left text-xs font-semibold uppercase tracking-wider text-stone-500">
              <th className="px-6 py-3.5">Agent</th>
              <th className="px-6 py-3.5">Status</th>
              <th className="px-6 py-3.5">Seen by</th>
              <th className="px-6 py-3.5">Evidence</th>
              <th className="px-6 py-3.5 text-right">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-50">
            {(data?.agents ?? []).map((a) => {
              const st = STATUS[a.status] ?? STATUS.discovered;
              return (
                <tr key={a.agent_id} className="align-top transition hover:bg-stone-50/50">
                  <td className="px-6 py-4">
                    <div className="font-mono text-sm text-stone-800">{a.agent_id}</div>
                    {a.gateways.length > 0 && (
                      <div className="mt-0.5 text-xs text-stone-400">{a.gateways.join(", ")}</div>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${st.cls}`}>
                      <st.icon className="h-3 w-3" /> {st.label}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-xs text-stone-600">
                    {a.sources.join(", ")}
                    {a.call_count > 0 && <span className="text-stone-400"> · {a.call_count} calls</span>}
                    {a.confidence < 1 && <span className="text-stone-400"> · conf {a.confidence}</span>}
                  </td>
                  <td className="px-6 py-4 text-xs text-stone-500">
                    {a.evidence.slice(0, 2).map((e, i) => <div key={i}>{e}</div>)}
                  </td>
                  <td className="px-6 py-4 text-right">
                    {a.status === "discovered" ? (
                      <button
                        onClick={() => onboard.mutate(a)}
                        disabled={onboard.isPending}
                        className="inline-flex items-center gap-1 rounded-md bg-violet-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-violet-700 disabled:opacity-40"
                      >
                        <Plus className="h-3.5 w-3.5" /> Onboard
                      </button>
                    ) : (
                      <span className="text-xs text-stone-300">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {(data?.agents?.length ?? 0) === 0 && (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <Radar className="h-7 w-7 text-stone-300" />
            <p className="text-sm text-stone-500">No agents observed yet.</p>
          </div>
        )}
      </div>

      <p className="text-xs text-stone-400">
        Note: onboarding registers a discovered agent and assigns a gateway. Routing its traffic
        through that gateway (to actually enforce policy) is a separate step — discovery gives you
        visibility and the path, not automatic control.
      </p>
    </div>
  );
}
