import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Shield, Key, Plus, Trash2, Pencil, X, Loader2, CheckCircle2, XCircle, Eye, EyeOff, Activity } from "lucide-react";
import { fetchAPI } from "../lib/api";

interface ProviderResponse {
  name: string;
  enabled: boolean;
  status: string;
  last_checked: string | null;
  latency_ms: number | null;
  models_available: string[];
  api_base_url: string;
  region: string;
  project_id: string;
  tenant_id: string;
  has_api_key: boolean;
  api_key_preview: string;
}

interface ProviderCreate {
  name: string;
  api_key: string;
  api_base_url: string;
  region: string;
  project_id: string;
  tenant_id: string;
  enabled: boolean;
}

const PROVIDER_NAMES = [
  "bedrock", "bedrock-mantle", "anthropic", "openai", "google_ai",
  "xai", "groq", "together", "fireworks", "ai21", "azure", "vertex", "cohere",
];

const PROVIDER_COLORS: Record<string, { bg: string; text: string; border: string; icon: string }> = {
  anthropic: { bg: "bg-amber-50", text: "text-amber-700", border: "border-amber-200", icon: "text-amber-600" },
  openai: { bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200", icon: "text-emerald-600" },
  bedrock: { bg: "bg-orange-50", text: "text-orange-700", border: "border-orange-200", icon: "text-orange-600" },
  azure: { bg: "bg-blue-50", text: "text-blue-700", border: "border-blue-200", icon: "text-blue-600" },
  vertex: { bg: "bg-indigo-50", text: "text-indigo-700", border: "border-indigo-200", icon: "text-indigo-600" },
  cohere: { bg: "bg-purple-50", text: "text-purple-700", border: "border-purple-200", icon: "text-purple-600" },
};

const STATUS_DISPLAY: Record<string, { label: string; dot: string; badge: string }> = {
  connected: { label: "Connected", dot: "bg-emerald-500", badge: "bg-emerald-50 text-emerald-700" },
  error: { label: "Error", dot: "bg-rose-500", badge: "bg-rose-50 text-rose-700" },
  unchecked: { label: "Unchecked", dot: "bg-stone-300", badge: "bg-stone-100 text-stone-500" },
};

const EMPTY_FORM: ProviderCreate = {
  name: "anthropic",
  api_key: "",
  api_base_url: "",
  region: "",
  project_id: "",
  tenant_id: "",
  enabled: true,
};

async function fetchProviders(): Promise<ProviderResponse[]> {
  return fetchAPI<ProviderResponse[]>("/api/providers");
}

export function Providers() {
  const queryClient = useQueryClient();
  const { data: providers = [], isLoading } = useQuery({ queryKey: ["providers"], queryFn: fetchProviders });

  const [showForm, setShowForm] = useState(false);
  const [editingProvider, setEditingProvider] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderCreate>({ ...EMPTY_FORM });
  const [testingProvider, setTestingProvider] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { success: boolean; error?: string } | null>>({});
  const [revealedKeys, setRevealedKeys] = useState<Record<string, string>>({});
  const [revealLoading, setRevealLoading] = useState<Record<string, boolean>>({});
  const [actionError, setActionError] = useState("");

  const createMutation = useMutation({
    mutationFn: async (data: ProviderCreate) => {
      const method = editingProvider ? "PUT" : "POST";
      const path = editingProvider
        ? `/api/providers/${editingProvider}`
        : "/api/providers";
      const body = editingProvider
        ? JSON.stringify({
            api_key: data.api_key || undefined,
            api_base_url: data.api_base_url,
            region: data.region,
            project_id: data.project_id,
            tenant_id: data.tenant_id,
            enabled: data.enabled,
          })
        : JSON.stringify(data);
      return fetchAPI<ProviderResponse>(path, { method, body });
    },
    onSuccess: () => {
      setActionError("");
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      setShowForm(false);
      setEditingProvider(null);
      setForm({ ...EMPTY_FORM });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (name: string) => fetchAPI(`/api/providers/${name}`, { method: "DELETE" }),
    onMutate: () => setActionError(""),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["providers"] }),
    onError: (error) => setActionError(error instanceof Error ? error.message : "Failed to delete provider"),
  });

  const testProvider = async (name: string) => {
    setTestingProvider(name);
    setTestResult((prev) => ({ ...prev, [name]: null }));
    try {
      const data = await fetchAPI<{ success: boolean; error?: string }>(
        `/api/providers/${name}/test`,
        { method: "POST" },
      );
      setTestResult((prev) => ({ ...prev, [name]: { success: data.success, error: data.error } }));
      queryClient.invalidateQueries({ queryKey: ["providers"] });
    } catch (err: any) {
      setTestResult((prev) => ({ ...prev, [name]: { success: false, error: err.message } }));
    } finally {
      setTestingProvider(null);
    }
  };

  const revealKey = async (name: string) => {
    if (revealedKeys[name]) {
      // Toggle off
      setRevealedKeys((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
      return;
    }
    setRevealLoading((prev) => ({ ...prev, [name]: true }));
    setActionError("");
    try {
      const data = await fetchAPI<{ api_key: string }>(`/api/providers/${name}/key`);
      setRevealedKeys((prev) => ({ ...prev, [name]: data.api_key }));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Failed to retrieve provider key");
    } finally {
      setRevealLoading((prev) => ({ ...prev, [name]: false }));
    }
  };

  const startEdit = (p: ProviderResponse) => {
    setForm({
      name: p.name,
      api_key: "",
      api_base_url: p.api_base_url,
      region: p.region,
      project_id: p.project_id,
      tenant_id: p.tenant_id,
      enabled: p.enabled,
    });
    setEditingProvider(p.name);
    setShowForm(true);
  };

  // Which extra fields to show based on provider type
  const showRegion = ["bedrock", "bedrock-mantle", "vertex", "azure"].includes(form.name);
  const showProjectId = form.name === "vertex";
  const showTenantId = form.name === "azure";
  const showApiBaseUrl = ["openai", "azure", "anthropic", "cohere", "vertex"].includes(form.name);
  const showApiKey = form.name !== "bedrock"; // Bedrock uses IAM, not API keys

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-stone-900">LLM Providers</h1>
          <p className="mt-1 text-sm text-stone-500">
            Manage API keys and connectivity for LLM providers
          </p>
        </div>
        <button
          onClick={() => {
            setForm({ ...EMPTY_FORM });
            setEditingProvider(null);
            setShowForm(true);
          }}
          className="btn-primary"
        >
          <Plus className="h-4 w-4" /> Add Provider
        </button>
      </div>
      {actionError && <p className="text-sm font-medium text-rose-600">{actionError}</p>}

      {/* Add/Edit Form */}
      {showForm && (
        <div className="card p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-stone-800">
              {editingProvider ? `Edit: ${editingProvider}` : "Add New Provider"}
            </h3>
            <button
              onClick={() => {
                setShowForm(false);
                setEditingProvider(null);
              }}
              className="text-stone-400 hover:text-stone-600"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="grid grid-cols-2 gap-4">
            {/* Provider Name */}
            <div>
              <label className="text-xs font-medium text-stone-500 uppercase">Provider</label>
              <select
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="input mt-1"
                disabled={!!editingProvider}
              >
                {PROVIDER_NAMES.map((p) => (
                  <option key={p} value={p}>
                    {p.charAt(0).toUpperCase() + p.slice(1)}
                  </option>
                ))}
              </select>
            </div>

            {/* Enabled Toggle */}
            <div className="flex items-end">
              <label className="flex items-center gap-2 text-sm text-stone-600">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                  className="rounded"
                />
                Enabled
              </label>
            </div>

            {/* API Key */}
            {showApiKey && (
              <div className="col-span-2">
                <label className="text-xs font-medium text-stone-500 uppercase">API Key</label>
                <input
                  type="password"
                  placeholder={editingProvider ? "Leave blank to keep existing key" : "sk-..."}
                  value={form.api_key}
                  onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                  className="input mt-1 font-mono"
                />
              </div>
            )}

            {/* API Base URL */}
            {showApiBaseUrl && (
              <div className="col-span-2">
                <label className="text-xs font-medium text-stone-500 uppercase">
                  API Base URL <span className="text-stone-400">(optional)</span>
                </label>
                <input
                  placeholder={
                    form.name === "anthropic"
                      ? "https://api.anthropic.com"
                      : form.name === "openai"
                        ? "https://api.openai.com"
                        : form.name === "azure"
                          ? "https://your-resource.openai.azure.com"
                          : ""
                  }
                  value={form.api_base_url}
                  onChange={(e) => setForm({ ...form, api_base_url: e.target.value })}
                  className="input mt-1"
                />
              </div>
            )}

            {/* Region */}
            {showRegion && (
              <div>
                <label className="text-xs font-medium text-stone-500 uppercase">Region</label>
                <input
                  placeholder={form.name === "bedrock" ? "us-east-1" : "us-central1"}
                  value={form.region}
                  onChange={(e) => setForm({ ...form, region: e.target.value })}
                  className="input mt-1"
                />
              </div>
            )}

            {/* Project ID (Vertex) */}
            {showProjectId && (
              <div>
                <label className="text-xs font-medium text-stone-500 uppercase">Project ID</label>
                <input
                  placeholder="my-gcp-project"
                  value={form.project_id}
                  onChange={(e) => setForm({ ...form, project_id: e.target.value })}
                  className="input mt-1"
                />
              </div>
            )}

            {/* Tenant ID (Azure) */}
            {showTenantId && (
              <div>
                <label className="text-xs font-medium text-stone-500 uppercase">Tenant ID</label>
                <input
                  placeholder="Azure tenant ID"
                  value={form.tenant_id}
                  onChange={(e) => setForm({ ...form, tenant_id: e.target.value })}
                  className="input mt-1"
                />
              </div>
            )}
          </div>

          <div className="flex gap-2">
            <button
              onClick={() => createMutation.mutate(form)}
              disabled={createMutation.isPending}
              className="btn-primary"
            >
              {createMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Key className="h-4 w-4" />
              )}
              {editingProvider ? "Save Changes" : "Add Provider"}
            </button>
            <button
              onClick={() => {
                setShowForm(false);
                setEditingProvider(null);
              }}
              className="btn-secondary"
            >
              Cancel
            </button>
            {createMutation.isError && (
              <span className="text-xs text-rose-600 self-center">
                {(createMutation.error as Error)?.message}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Provider Cards Grid */}
      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-stone-400" />
        </div>
      ) : providers.length === 0 ? (
        <div className="card flex flex-col items-center gap-3 py-12">
          <Shield className="h-8 w-8 text-stone-300" />
          <p className="text-sm text-stone-500">No providers configured</p>
          <p className="text-xs text-stone-400">
            Add your first LLM provider to start routing requests
          </p>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {providers.map((provider) => {
            const colors = PROVIDER_COLORS[provider.name] || PROVIDER_COLORS.anthropic;
            const statusInfo = STATUS_DISPLAY[provider.status] || STATUS_DISPLAY.unchecked;
            const isTesting = testingProvider === provider.name;
            const result = testResult[provider.name];
            const revealed = revealedKeys[provider.name];

            return (
              <div
                key={provider.name}
                className={`card p-5 space-y-4 border ${colors.border} ${!provider.enabled ? "opacity-60" : ""}`}
              >
                {/* Header */}
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2.5">
                    <div
                      className={`flex h-9 w-9 items-center justify-center rounded-lg ${colors.bg}`}
                    >
                      <Key className={`h-4.5 w-4.5 ${colors.icon}`} />
                    </div>
                    <div>
                      <p className="text-sm font-semibold text-stone-800">
                        {provider.name.charAt(0).toUpperCase() + provider.name.slice(1)}
                      </p>
                      <div className="flex items-center gap-1.5 mt-0.5">
                        <span className={`h-2 w-2 rounded-full ${statusInfo.dot}`} />
                        <span className="text-[10px] text-stone-500">{statusInfo.label}</span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => startEdit(provider)}
                      title="Edit"
                      className="rounded-lg p-1.5 text-stone-400 hover:bg-indigo-50 hover:text-indigo-600 transition"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={() => deleteMutation.mutate(provider.name)}
                      title="Delete"
                      className="rounded-lg p-1.5 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>

                {/* API Key */}
                {provider.has_api_key && (
                  <div className="space-y-1">
                    <p className="text-[10px] font-semibold text-stone-400 uppercase">API Key</p>
                    <div className="flex items-center gap-2">
                      <code className="flex-1 rounded-lg bg-stone-50 px-2.5 py-1.5 text-xs font-mono text-stone-600 truncate">
                        {revealed || provider.api_key_preview || "****"}
                      </code>
                      <button
                        onClick={() => revealKey(provider.name)}
                        className="rounded-lg p-1.5 text-stone-400 hover:bg-stone-100 hover:text-stone-600 transition"
                        title={revealed ? "Hide key" : "Reveal key"}
                      >
                        {revealLoading[provider.name] ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : revealed ? (
                          <EyeOff className="h-3.5 w-3.5" />
                        ) : (
                          <Eye className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </div>
                  </div>
                )}

                {/* Latency & Models */}
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <p className="text-[10px] font-semibold text-stone-400 uppercase">Latency</p>
                    <p className="text-sm font-medium text-stone-700">
                      {provider.latency_ms != null ? `${provider.latency_ms}ms` : "--"}
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] font-semibold text-stone-400 uppercase">Models</p>
                    <p className="text-sm font-medium text-stone-700">
                      {provider.models_available.length || "--"}
                    </p>
                  </div>
                </div>

                {/* Extra config info */}
                {(provider.region || provider.project_id || provider.api_base_url) && (
                  <div className="space-y-1">
                    {provider.region && (
                      <p className="text-[10px] text-stone-500">
                        <span className="font-semibold">Region:</span> {provider.region}
                      </p>
                    )}
                    {provider.project_id && (
                      <p className="text-[10px] text-stone-500">
                        <span className="font-semibold">Project:</span> {provider.project_id}
                      </p>
                    )}
                    {provider.api_base_url && (
                      <p className="text-[10px] text-stone-500 truncate">
                        <span className="font-semibold">Endpoint:</span> {provider.api_base_url}
                      </p>
                    )}
                  </div>
                )}

                {/* Test Result */}
                {result && (
                  <div
                    className={`flex items-center gap-2 rounded-lg px-3 py-2 text-xs ${
                      result.success
                        ? "bg-emerald-50 text-emerald-700"
                        : "bg-rose-50 text-rose-700"
                    }`}
                  >
                    {result.success ? (
                      <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                    ) : (
                      <XCircle className="h-3.5 w-3.5 shrink-0" />
                    )}
                    <span className="truncate">
                      {result.success ? "Connection successful" : result.error || "Connection failed"}
                    </span>
                  </div>
                )}

                {/* Actions */}
                <div className="flex gap-2 pt-1">
                  <button
                    onClick={() => testProvider(provider.name)}
                    disabled={isTesting}
                    className="btn-secondary flex-1 text-xs"
                  >
                    {isTesting ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Activity className="h-3.5 w-3.5" />
                    )}
                    Test
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Models Summary (if providers have been tested) */}
      {providers.some((p) => p.models_available.length > 0) && (
        <div className="card p-5 space-y-3">
          <h3 className="text-sm font-semibold text-stone-800">Available Models by Provider</h3>
          <div className="space-y-2">
            {providers
              .filter((p) => p.models_available.length > 0)
              .map((p) => {
                const colors = PROVIDER_COLORS[p.name] || PROVIDER_COLORS.anthropic;
                return (
                  <div key={p.name} className="flex items-start gap-3">
                    <span
                      className={`mt-0.5 inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${colors.bg} ${colors.text}`}
                    >
                      {p.name}
                    </span>
                    <div className="flex flex-wrap gap-1">
                      {p.models_available.map((m) => (
                        <span
                          key={m}
                          className="rounded-full bg-stone-100 px-2 py-0.5 text-[10px] text-stone-600 font-mono"
                        >
                          {m}
                        </span>
                      ))}
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      )}
    </div>
  );
}
