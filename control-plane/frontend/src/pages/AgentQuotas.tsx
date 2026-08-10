import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Edit2,
  LoaderCircle,
  Plus,
  Save,
  ShieldCheck,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api, type Agent, type Quota } from "../lib/api";

interface QuotaForm {
  agent_id: string;
  gateway_id: string;
  rate_limit_rpm: string;
  budget_limit_usd: string;
  max_tokens_per_request: string;
  allowed_models: string;
  allowed_providers: string;
  alert_threshold_pct: number;
}

const EMPTY_FORM: QuotaForm = {
  agent_id: "",
  gateway_id: "",
  rate_limit_rpm: "30",
  budget_limit_usd: "10",
  max_tokens_per_request: "4096",
  allowed_models: "*",
  allowed_providers: "*",
  alert_threshold_pct: 90,
};

function optionalInt(value: string): number | null {
  return value.trim() ? Number.parseInt(value, 10) : null;
}

function optionalFloat(value: string): number | null {
  return value.trim() ? Number.parseFloat(value) : null;
}

function splitList(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function quotaToForm(quota: Quota): QuotaForm {
  return {
    agent_id: quota.scope_id,
    gateway_id: quota.gateway_id,
    rate_limit_rpm: quota.rate_limit_rpm?.toString() ?? "",
    budget_limit_usd: quota.budget_limit_usd?.toString() ?? "",
    max_tokens_per_request: quota.max_tokens_per_request?.toString() ?? "",
    allowed_models: quota.allowed_models.join(", "),
    allowed_providers: quota.allowed_providers.join(", "),
    alert_threshold_pct: quota.alert_threshold_pct,
  };
}

function agentGateway(agentId: string, agents: Agent[]): string {
  return agents.find((agent) => agent.name === agentId)?.gateway_id ?? "";
}

export function AgentQuotas() {
  const queryClient = useQueryClient();
  const [editingId, setEditingId] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<QuotaForm>(EMPTY_FORM);
  const [statusMessage, setStatusMessage] = useState("");

  const { data: quotas = [], isLoading } = useQuery({
    queryKey: ["agent-quotas"],
    queryFn: () => api.quotas.list("agent"),
    refetchInterval: 15_000,
  });
  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: api.agents.list,
  });
  const { data: gateways = [] } = useQuery({
    queryKey: ["gateways"],
    queryFn: api.gateways.list,
  });

  const saveMutation = useMutation({
    mutationFn: async () => {
      const common = {
        name: `${form.agent_id} limits`,
        gateway_id: form.gateway_id,
        rate_limit_rpm: optionalInt(form.rate_limit_rpm),
        budget_limit_usd: optionalFloat(form.budget_limit_usd),
        max_tokens_per_request: optionalInt(form.max_tokens_per_request),
        allowed_models: splitList(form.allowed_models),
        allowed_providers: splitList(form.allowed_providers),
        alert_threshold_pct: form.alert_threshold_pct,
      };
      let saved: Quota | null = null;
      try {
        saved = editingId === null
          ? await api.quotas.create({
              ...common,
              scope: "agent",
              scope_id: form.agent_id,
            })
          : await api.quotas.update(editingId, common);
        const pushed = await api.quotas.push(saved.id);
        if (!["pushed", "queued"].includes(pushed.status)) {
          throw new Error(pushed.detail || pushed.reason || "Gateway rejected agent quotas");
        }
        return pushed.status;
      } catch (error) {
        // Persistence and delivery are separate operations. If persistence
        // succeeded, retry must update that record instead of creating a
        // duplicate after a gateway-side rejection.
        if (saved) {
          setEditingId(saved.id);
          queryClient.invalidateQueries({ queryKey: ["agent-quotas"] });
          queryClient.invalidateQueries({ queryKey: ["quotas"] });
        }
        throw error;
      }
    },
    onSuccess: (status) => {
      queryClient.invalidateQueries({ queryKey: ["agent-quotas"] });
      queryClient.invalidateQueries({ queryKey: ["quotas"] });
      setShowForm(false);
      setEditingId(null);
      setStatusMessage(status === "queued" ? "Saved; gateway sync queued" : "Saved and pushed");
      window.setTimeout(() => setStatusMessage(""), 2500);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (quota: Quota) => {
      const result = await api.quotas.delete(quota.id);
      if (result.gateway_id) {
        const pushed = await api.quotas.pushAgents(result.gateway_id);
        if (!["pushed", "queued"].includes(pushed.status)) {
          throw new Error(pushed.detail || pushed.reason || "Gateway rejected updated agent quotas");
        }
      }
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-quotas"] });
      queryClient.invalidateQueries({ queryKey: ["quotas"] });
    },
  });

  const pushAllMutation = useMutation({
    mutationFn: async () => {
      const gatewayIds = gateways.map((gateway) => gateway.id);
      const results = await Promise.all(gatewayIds.map((gatewayId) => api.quotas.pushAgents(gatewayId)));
      const failed = results.find((result) => !["pushed", "queued"].includes(result.status));
      if (failed) {
        throw new Error(failed.detail || failed.reason || `Failed to sync ${failed.gateway}`);
      }
      return results.some((result) => result.status === "queued");
    },
    onSuccess: (queued) => {
      setStatusMessage(queued ? "Gateway sync queued" : "All gateways synchronized");
      window.setTimeout(() => setStatusMessage(""), 2500);
    },
  });

  const error = saveMutation.error ?? deleteMutation.error ?? pushAllMutation.error;

  const openNew = () => {
    const firstAvailable = agents.find(
      (agent) => !quotas.some(
        (quota) => quota.scope_id === agent.name && quota.gateway_id === agent.gateway_id,
      ),
    );
    setForm({
      ...EMPTY_FORM,
      agent_id: firstAvailable?.name ?? "",
      gateway_id: firstAvailable?.gateway_id ?? gateways[0]?.id ?? "",
    });
    setEditingId(null);
    setShowForm(true);
  };

  const openEdit = (quota: Quota) => {
    setForm(quotaToForm(quota));
    setEditingId(quota.id);
    setShowForm(true);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Agent Quotas</h1>
          <p className="mt-1 text-sm text-stone-500">Per-agent rate limits, budgets, and token caps</p>
        </div>
        <div className="flex items-center gap-2">
          {statusMessage && (
            <span className="flex items-center gap-1 text-xs font-medium text-emerald-700">
              <Check className="h-3.5 w-3.5" /> {statusMessage}
            </span>
          )}
          <button
            onClick={() => pushAllMutation.mutate()}
            disabled={!gateways.length || pushAllMutation.isPending}
            className="btn-secondary"
          >
            {pushAllMutation.isPending
              ? <LoaderCircle className="h-4 w-4 animate-spin" />
              : <Upload className="h-4 w-4" />}
            Push All
          </button>
          <button onClick={openNew} className="btn-primary">
            <Plus className="h-4 w-4" /> Add Agent
          </button>
        </div>
      </div>

      {error && (
        <p className="text-sm font-medium text-rose-600">
          {error instanceof Error ? error.message : "Agent quota request failed"}
        </p>
      )}

      {showForm && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            saveMutation.mutate();
          }}
          className="card space-y-4 p-6"
        >
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-stone-800">
              {editingId === null ? "Add Agent Quota" : `Edit ${form.agent_id}`}
            </h2>
            <button
              type="button"
              onClick={() => setShowForm(false)}
              title="Close"
              className="rounded-lg p-1.5 text-stone-400 hover:bg-stone-100 hover:text-stone-700"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-stone-600">
              Agent
              <select
                required
                disabled={editingId !== null}
                value={form.agent_id}
                onChange={(event) => {
                  const agentId = event.target.value;
                  setForm({
                    ...form,
                    agent_id: agentId,
                    gateway_id: agentGateway(agentId, agents) || form.gateway_id,
                  });
                }}
                className="input mt-1"
              >
                <option value="">Select agent</option>
                {agents.map((agent) => (
                  <option
                    key={agent.name}
                    value={agent.name}
                    disabled={quotas.some(
                      (quota) => quota.id !== editingId
                        && quota.scope_id === agent.name
                        && quota.gateway_id === agent.gateway_id,
                    )}
                  >
                    {agent.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="text-xs font-medium text-stone-600">
              Gateway
              <select
                required
                disabled={editingId !== null}
                value={form.gateway_id}
                onChange={(event) => setForm({ ...form, gateway_id: event.target.value })}
                className="input mt-1"
              >
                <option value="">Select gateway</option>
                {gateways.map((gateway) => (
                  <option key={gateway.id} value={gateway.id}>{gateway.name || gateway.id}</option>
                ))}
              </select>
            </label>

            <label className="text-xs font-medium text-stone-600">
              Rate Limit (RPM)
              <input
                type="number"
                min="1"
                value={form.rate_limit_rpm}
                onChange={(event) => setForm({ ...form, rate_limit_rpm: event.target.value })}
                className="input mt-1"
              />
            </label>

            <label className="text-xs font-medium text-stone-600">
              Budget (USD)
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.budget_limit_usd}
                onChange={(event) => setForm({ ...form, budget_limit_usd: event.target.value })}
                className="input mt-1"
              />
            </label>

            <label className="text-xs font-medium text-stone-600">
              Max Tokens per Request
              <input
                type="number"
                min="1"
                value={form.max_tokens_per_request}
                onChange={(event) => setForm({ ...form, max_tokens_per_request: event.target.value })}
                className="input mt-1"
              />
            </label>

            <label className="text-xs font-medium text-stone-600">
              Alert Threshold
              <select
                value={form.alert_threshold_pct}
                onChange={(event) => setForm({
                  ...form,
                  alert_threshold_pct: Number.parseInt(event.target.value, 10),
                })}
                className="input mt-1"
              >
                <option value={80}>80%</option>
                <option value={90}>90%</option>
                <option value={100}>100%</option>
              </select>
            </label>

            <label className="text-xs font-medium text-stone-600">
              Allowed Providers
              <input
                value={form.allowed_providers}
                onChange={(event) => setForm({ ...form, allowed_providers: event.target.value })}
                className="input mt-1"
                placeholder="* or anthropic, openai"
              />
            </label>

            <label className="text-xs font-medium text-stone-600">
              Allowed Models
              <input
                value={form.allowed_models}
                onChange={(event) => setForm({ ...form, allowed_models: event.target.value })}
                className="input mt-1"
                placeholder="* or claude-haiku, gpt-4o-mini"
              />
            </label>
          </div>

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={saveMutation.isPending || !form.agent_id || !form.gateway_id}
              className="btn-primary"
            >
              {saveMutation.isPending
                ? <LoaderCircle className="h-4 w-4 animate-spin" />
                : <Save className="h-4 w-4" />}
              Save & Push
            </button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">
              Cancel
            </button>
          </div>
        </form>
      )}

      {isLoading ? (
        <div className="flex h-40 items-center justify-center text-stone-400">
          <LoaderCircle className="h-5 w-5 animate-spin" />
        </div>
      ) : quotas.length === 0 ? (
        <div className="flex h-40 items-center justify-center border border-dashed border-stone-300 text-sm text-stone-500">
          No agent quotas configured
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {quotas.map((quota) => {
            const budgetPct = quota.budget_limit_usd
              ? Math.min(100, (quota.current_spend / quota.budget_limit_usd) * 100)
              : 0;
            const barColor = budgetPct >= quota.alert_threshold_pct
              ? "bg-rose-500"
              : budgetPct >= 70
                ? "bg-amber-500"
                : "bg-emerald-500";

            return (
              <div key={quota.id} className="card space-y-4 p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-2.5">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-teal-50 text-teal-700">
                      <ShieldCheck className="h-4 w-4" />
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-stone-900">{quota.scope_id}</p>
                      <p className="truncate text-xs text-stone-500">{quota.gateway_id}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => openEdit(quota)}
                      title="Edit"
                      className="rounded-lg p-1.5 text-stone-400 hover:bg-indigo-50 hover:text-indigo-700"
                    >
                      <Edit2 className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={() => deleteMutation.mutate(quota)}
                      title="Delete"
                      disabled={deleteMutation.isPending}
                      className="rounded-lg p-1.5 text-stone-400 hover:bg-rose-50 hover:text-rose-700"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>

                {quota.budget_limit_usd !== null && (
                  <div>
                    <div className="mb-1 flex items-center justify-between text-[10px] text-stone-500">
                      <span className="font-semibold uppercase">Budget</span>
                      <span className="tabular-nums">
                        ${quota.current_spend.toFixed(4)} / ${quota.budget_limit_usd.toFixed(2)}
                      </span>
                    </div>
                    <div className="h-2 w-full overflow-hidden rounded-full bg-stone-100">
                      <div className={`h-full ${barColor}`} style={{ width: `${budgetPct}%` }} />
                    </div>
                    <p className="mt-1 text-[10px] text-stone-400">
                      {budgetPct.toFixed(0)}% used · alert at {quota.alert_threshold_pct}%
                    </p>
                  </div>
                )}

                <div className="grid grid-cols-2 gap-2">
                  <div className="bg-stone-50 px-2.5 py-2 text-xs">
                    <span className="text-stone-400">Request rate</span>
                    <p className="font-semibold text-stone-800">
                      {quota.current_rpm} / {quota.rate_limit_rpm ?? "∞"} RPM
                    </p>
                  </div>
                  <div className="bg-stone-50 px-2.5 py-2 text-xs">
                    <span className="text-stone-400">Max tokens</span>
                    <p className="font-semibold text-stone-800">
                      {quota.max_tokens_per_request?.toLocaleString() ?? "∞"}
                    </p>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="flex flex-wrap gap-1">
                    {(quota.allowed_models.length ? quota.allowed_models : ["*"]).slice(0, 4).map((model) => (
                      <span key={model} className="bg-violet-50 px-2 py-0.5 text-[9px] font-medium text-violet-700">
                        {model}
                      </span>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {(quota.allowed_providers.length ? quota.allowed_providers : ["*"]).map((provider) => (
                      <span key={provider} className="bg-sky-50 px-2 py-0.5 text-[9px] font-medium text-sky-700">
                        {provider}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
