import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Ghost,
  Plus,
  Radar,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  api,
  type DiscoveredAgent,
  type DiscoveryOnboardResponse,
} from "../lib/api";

const STATUS: Record<string, { label: string; cls: string; icon: typeof ShieldAlert }> = {
  discovered: {
    label: "Shadow - ungoverned",
    cls: "bg-rose-50 text-rose-700",
    icon: ShieldAlert,
  },
  registered_off_gateway: {
    label: "Registered, off gateway",
    cls: "bg-amber-50 text-amber-700",
    icon: AlertCircle,
  },
  governed: {
    label: "Governed",
    cls: "bg-emerald-50 text-emerald-700",
    icon: ShieldCheck,
  },
  governed_unseen: {
    label: "Registered, unseen",
    cls: "bg-stone-100 text-stone-500",
    icon: Ghost,
  },
};

export function Discovery() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["discovery"],
    queryFn: api.discovery.agents,
    refetchInterval: 5000,
  });
  const { data: gateways = [] } = useQuery({
    queryKey: ["gateways"],
    queryFn: api.gateways.list,
  });
  const [selected, setSelected] = useState<DiscoveredAgent | null>(null);
  const [gatewayId, setGatewayId] = useState("");
  const [framework, setFramework] = useState("other");
  const [lastResult, setLastResult] = useState<DiscoveryOnboardResponse | null>(null);

  const onboard = useMutation({
    mutationFn: ({
      agent,
      targetGateway,
      targetFramework,
    }: {
      agent: DiscoveredAgent;
      targetGateway: string;
      targetFramework: string;
    }) =>
      api.discovery.onboard({
        agent_id: agent.agent_id,
        gateway_id: targetGateway,
        framework: targetFramework,
        allowed_tools: [],
        allowed_models: ["*"],
        allowed_providers: ["*"],
      }),
    onSuccess: async (result) => {
      setLastResult(result);
      setSelected(null);
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["discovery"] }),
        qc.invalidateQueries({ queryKey: ["agents"] }),
      ]);
    },
  });

  function openOnboard(agent: DiscoveredAgent) {
    const observed = gateways.find((gateway) => agent.gateways.includes(gateway.id));
    setGatewayId(observed?.id || gateways[0]?.id || "");
    setFramework("other");
    setSelected(agent);
    onboard.reset();
  }

  const summary = data?.summary;
  const sourceErrors = summary?.source_status?.filter((source) => source.status === "error") ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
          <Radar className="h-6 w-6 text-violet-500" /> Agent Discovery
        </h1>
        <p className="mt-1 text-sm text-stone-500">
          Reconciles gateway traffic and infrastructure signals against the agent registry.
          {" "}Sources: {summary?.sources?.join(" / ") || "..."}
        </p>
      </div>

      {sourceErrors.length > 0 && (
        <div className="border-l-4 border-amber-400 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {sourceErrors.map((source) => (
            <div key={source.source}>
              <span className="font-medium">{source.source}</span>: {source.detail}
            </div>
          ))}
        </div>
      )}

      {lastResult && (
        <div
          className={`flex items-start gap-2 border-l-4 px-4 py-3 text-sm ${
            lastResult.traffic_routed && lastResult.gateway_policy.status === "pushed"
              ? "border-emerald-500 bg-emerald-50 text-emerald-800"
              : "border-amber-400 bg-amber-50 text-amber-800"
          }`}
        >
          {lastResult.traffic_routed ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <span>
            <span className="font-medium">{lastResult.onboarded}</span> registered on{" "}
            <span className="font-mono">{lastResult.gateway_id}</span>. Gateway policy:{" "}
            {lastResult.gateway_policy.status}. Traffic:{" "}
            {lastResult.traffic_routed ? "observed through assigned gateway" : "still off gateway"}.
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
        <Summary label="Shadow" value={summary?.shadow ?? 0} tone="text-rose-600" />
        <Summary
          label="Registered, off gateway"
          value={summary?.off_gateway ?? 0}
          tone="text-amber-600"
        />
        <Summary label="Governed" value={summary?.governed ?? 0} tone="text-emerald-600" />
        <Summary label="Registered, unseen" value={summary?.stale ?? 0} tone="text-stone-700" />
      </div>

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[900px]">
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
            {(data?.agents ?? []).map((agent) => {
              const status = STATUS[agent.status] ?? STATUS.discovered;
              return (
                <tr key={agent.agent_id} className="align-top transition hover:bg-stone-50/50">
                  <td className="px-6 py-4">
                    <div className="font-mono text-sm text-stone-800">{agent.agent_id}</div>
                    {(agent.assigned_gateway || agent.gateways.length > 0) && (
                      <div className="mt-0.5 text-xs text-stone-400">
                        {agent.assigned_gateway
                          ? `assigned: ${agent.assigned_gateway}`
                          : `observed: ${agent.gateways.join(", ")}`}
                      </div>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${status.cls}`}
                    >
                      <status.icon className="h-3 w-3" /> {status.label}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-xs text-stone-600">
                    {agent.sources.join(", ")}
                    {agent.call_count > 0 && (
                      <span className="text-stone-400"> / {agent.call_count} calls</span>
                    )}
                    {agent.confidence < 1 && (
                      <span className="text-stone-400"> / conf {agent.confidence}</span>
                    )}
                  </td>
                  <td className="px-6 py-4 text-xs text-stone-500">
                    {agent.evidence.slice(0, 2).map((evidence, index) => (
                      <div key={`${agent.agent_id}-${index}`}>{evidence}</div>
                    ))}
                  </td>
                  <td className="px-6 py-4 text-right">
                    {agent.status === "discovered" ? (
                      <button
                        onClick={() => openOnboard(agent)}
                        disabled={gateways.length === 0}
                        className="inline-flex items-center gap-1 rounded-md bg-violet-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-violet-700 disabled:opacity-40"
                      >
                        <Plus className="h-3.5 w-3.5" /> Onboard
                      </button>
                    ) : (
                      <span className="text-xs text-stone-300">-</span>
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

      {selected && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="card w-full max-w-md p-6">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-lg font-semibold text-stone-900">Onboard agent</h2>
                <p className="mt-0.5 font-mono text-xs text-stone-500">{selected.agent_id}</p>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="text-stone-400 hover:text-stone-600"
                title="Close"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            <div className="mt-5 space-y-4">
              <div>
                <label className="mb-1 block text-xs font-medium text-stone-600">Gateway</label>
                <select
                  className="input w-full"
                  value={gatewayId}
                  onChange={(event) => setGatewayId(event.target.value)}
                >
                  {gateways.map((gateway) => (
                    <option key={gateway.id} value={gateway.id}>
                      {gateway.name || gateway.id}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-stone-600">Framework</label>
                <select
                  className="input w-full"
                  value={framework}
                  onChange={(event) => setFramework(event.target.value)}
                >
                  {[
                    "other",
                    "openai",
                    "anthropic",
                    "bedrock",
                    "agentcore",
                    "strands",
                    "crewai",
                    "langgraph",
                  ].map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
              </div>
              <div className="border-l-4 border-violet-400 bg-violet-50 px-3 py-2 text-xs text-violet-800">
                The gateway will allow existing traffic defaults while this identity starts with no
                tool grants.
              </div>
              {onboard.error && (
                <p className="text-sm text-red-600">
                  {onboard.error instanceof Error ? onboard.error.message : String(onboard.error)}
                </p>
              )}
            </div>

            <div className="mt-6 flex justify-end gap-3">
              <button className="btn btn-secondary" onClick={() => setSelected(null)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                disabled={!gatewayId || onboard.isPending}
                onClick={() =>
                  onboard.mutate({
                    agent: selected,
                    targetGateway: gatewayId,
                    targetFramework: framework,
                  })
                }
              >
                {onboard.isPending ? "Applying..." : "Register and apply"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Summary({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: string;
}) {
  return (
    <div className="card p-5">
      <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">{label}</p>
      <p className={`mt-2 text-3xl font-bold ${tone}`}>{value}</p>
    </div>
  );
}
