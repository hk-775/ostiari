import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  CheckCircle2,
  Eye,
  EyeOff,
  Gauge,
  Key,
  Loader2,
  LockKeyhole,
  Pencil,
  Plus,
  Shield,
  Trash2,
  Upload,
  Waypoints,
  X,
  XCircle,
} from "lucide-react";
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

interface ProviderRouteResponse {
  route_id: string;
  provider: string;
  endpoint: string;
  auth_type: string;
  region: string;
  allowed_models: string[];
  weight: number;
  priority: number;
  enabled: boolean;
  max_concurrency: number;
  capacity_group: string;
  capacity_limit: number;
  connect_timeout: number;
  read_timeout: number;
  max_connections: number;
  max_connections_per_host: number;
  keepalive_timeout: number;
  has_credentials: boolean;
  has_custom_headers: boolean;
  has_extra_params: boolean;
  created_at: string;
  updated_at: string;
}

interface ProviderRouteForm {
  provider: string;
  route_id: string;
  endpoint: string;
  auth_type: string;
  api_key: string;
  access_key: string;
  secret_key: string;
  session_token: string;
  access_token: string;
  region: string;
  allowed_models: string;
  weight: number;
  priority: number;
  enabled: boolean;
  max_concurrency: number;
  capacity_group: string;
  capacity_limit: number;
  connect_timeout: number;
  read_timeout: number;
  max_connections: number;
  max_connections_per_host: number;
  keepalive_timeout: number;
}

interface RuntimeRoute {
  route_id: string;
  provider: string;
  status: string;
  adaptive_weight?: number;
  inflight?: number;
  successes?: number;
  failures?: number;
  latency_ewma_ms?: number | null;
  cooldown_remaining_seconds?: number;
}

interface RouteRuntimeResponse {
  gateways: number;
  reachable: number;
  snapshots: Array<{
    gateway_id: string;
    status: string;
    routes: RuntimeRoute[];
    error: string;
  }>;
}

interface RoutePushResult {
  routes: number;
  gateways: number;
  pushed: number;
  failed: number;
  skipped: number;
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

const PROVIDER_ALIASES: Record<string, string> = {
  azure: "azure_openai",
  vertex: "vertex_ai",
};

function canonicalProvider(provider: string) {
  return PROVIDER_ALIASES[provider] || provider;
}

function defaultAuthType(provider: string) {
  const canonical = canonicalProvider(provider);
  if (canonical === "bedrock" || canonical === "bedrock-mantle") return "aws_credentials";
  if (canonical === "azure_openai") return "azure_key";
  if (canonical === "vertex_ai") return "gcp_service_account";
  return "api_key";
}

const EMPTY_ROUTE_FORM: ProviderRouteForm = {
  provider: "anthropic",
  route_id: "anthropic:primary",
  endpoint: "",
  auth_type: "api_key",
  api_key: "",
  access_key: "",
  secret_key: "",
  session_token: "",
  access_token: "",
  region: "",
  allowed_models: "",
  weight: 1,
  priority: 0,
  enabled: true,
  max_concurrency: 100,
  capacity_group: "",
  capacity_limit: 0,
  connect_timeout: 30,
  read_timeout: 120,
  max_connections: 100,
  max_connections_per_host: 100,
  keepalive_timeout: 30,
};

async function fetchProviders(): Promise<ProviderResponse[]> {
  return fetchAPI<ProviderResponse[]>("/api/providers");
}

async function fetchProviderRoutes(): Promise<ProviderRouteResponse[]> {
  return fetchAPI<ProviderRouteResponse[]>("/api/providers/routes");
}

async function fetchRouteRuntime(): Promise<RouteRuntimeResponse> {
  return fetchAPI<RouteRuntimeResponse>("/api/providers/routes/runtime");
}

export function Providers() {
  const queryClient = useQueryClient();
  const { data: providers = [], isLoading } = useQuery({ queryKey: ["providers"], queryFn: fetchProviders });
  const { data: routes = [], isLoading: routesLoading } = useQuery({
    queryKey: ["provider-routes"],
    queryFn: fetchProviderRoutes,
  });
  const { data: routeRuntime } = useQuery({
    queryKey: ["provider-route-runtime"],
    queryFn: fetchRouteRuntime,
    refetchInterval: 5000,
  });

  const [showForm, setShowForm] = useState(false);
  const [editingProvider, setEditingProvider] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderCreate>({ ...EMPTY_FORM });
  const [testingProvider, setTestingProvider] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, { success: boolean; error?: string } | null>>({});
  const [revealedKeys, setRevealedKeys] = useState<Record<string, string>>({});
  const [revealLoading, setRevealLoading] = useState<Record<string, boolean>>({});
  const [actionError, setActionError] = useState("");
  const [showRouteForm, setShowRouteForm] = useState(false);
  const [editingRoute, setEditingRoute] = useState<ProviderRouteResponse | null>(null);
  const [routeForm, setRouteForm] = useState<ProviderRouteForm>({ ...EMPTY_ROUTE_FORM });
  const [routeActionError, setRouteActionError] = useState("");
  const [pushResult, setPushResult] = useState<RoutePushResult | null>(null);

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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      queryClient.invalidateQueries({ queryKey: ["provider-routes"] });
    },
    onError: (error) => setActionError(error instanceof Error ? error.message : "Failed to delete provider"),
  });

  const routeMutation = useMutation({
    mutationFn: async (data: ProviderRouteForm) => {
      const credentials: Record<string, string> = {};
      if (data.auth_type === "api_key" || data.auth_type === "azure_key") {
        if (data.api_key) credentials.api_key = data.api_key;
      } else if (data.auth_type === "gcp_service_account") {
        if (data.access_token) credentials.access_token = data.access_token;
      } else if (data.auth_type === "aws_credentials") {
        if (data.access_key) credentials.access_key = data.access_key;
        if (data.secret_key) credentials.secret_key = data.secret_key;
        if (data.session_token) credentials.session_token = data.session_token;
      }

      const payload: Record<string, unknown> = {
        endpoint: data.endpoint,
        auth_type: data.auth_type,
        region: data.region,
        allowed_models: data.allowed_models
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
        weight: data.weight,
        priority: data.priority,
        enabled: data.enabled,
        max_concurrency: data.max_concurrency,
        capacity_group: data.capacity_group,
        capacity_limit: data.capacity_limit,
        connect_timeout: data.connect_timeout,
        read_timeout: data.read_timeout,
        max_connections: data.max_connections,
        max_connections_per_host: data.max_connections_per_host,
        keepalive_timeout: data.keepalive_timeout,
      };
      const credentialsChanged = Object.keys(credentials).length > 0
        || (!!editingRoute && editingRoute.auth_type !== data.auth_type);
      if (!editingRoute || credentialsChanged) {
        payload.credentials = credentials;
      }
      if (!editingRoute) {
        payload.route_id = data.route_id;
      }

      const provider = canonicalProvider(data.provider);
      const path = editingRoute
        ? `/api/providers/${encodeURIComponent(provider)}/routes/${encodeURIComponent(editingRoute.route_id)}`
        : `/api/providers/${encodeURIComponent(provider)}/routes`;
      return fetchAPI<ProviderRouteResponse>(path, {
        method: editingRoute ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
    },
    onMutate: () => setRouteActionError(""),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["provider-routes"] });
      queryClient.invalidateQueries({ queryKey: ["provider-route-runtime"] });
      setShowRouteForm(false);
      setEditingRoute(null);
      setRouteForm({ ...EMPTY_ROUTE_FORM });
    },
    onError: (error) => {
      setRouteActionError(error instanceof Error ? error.message : "Failed to save provider route");
    },
  });

  const deleteRouteMutation = useMutation({
    mutationFn: (route: ProviderRouteResponse) => fetchAPI(
      `/api/providers/${encodeURIComponent(route.provider)}/routes/${encodeURIComponent(route.route_id)}`,
      { method: "DELETE" },
    ),
    onMutate: () => setRouteActionError(""),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["provider-routes"] });
      queryClient.invalidateQueries({ queryKey: ["provider-route-runtime"] });
    },
    onError: (error) => {
      setRouteActionError(error instanceof Error ? error.message : "Failed to delete provider route");
    },
  });

  const pushRoutesMutation = useMutation({
    mutationFn: () => fetchAPI<RoutePushResult>("/api/providers/routes/push", {
      method: "POST",
    }),
    onMutate: () => {
      setRouteActionError("");
      setPushResult(null);
    },
    onSuccess: (result) => {
      setPushResult(result);
      queryClient.invalidateQueries({ queryKey: ["provider-route-runtime"] });
    },
    onError: (error) => {
      setRouteActionError(error instanceof Error ? error.message : "Failed to push provider routes");
    },
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

  const startRouteCreate = () => {
    const provider = canonicalProvider(providers[0]?.name || "anthropic");
    setRouteForm({
      ...EMPTY_ROUTE_FORM,
      provider,
      route_id: `${provider}:primary`,
      auth_type: defaultAuthType(provider),
    });
    setEditingRoute(null);
    setShowRouteForm(true);
    setRouteActionError("");
  };

  const startRouteEdit = (route: ProviderRouteResponse) => {
    setRouteForm({
      provider: route.provider,
      route_id: route.route_id,
      endpoint: route.endpoint,
      auth_type: route.auth_type,
      api_key: "",
      access_key: "",
      secret_key: "",
      session_token: "",
      access_token: "",
      region: route.region,
      allowed_models: route.allowed_models.join(", "),
      weight: route.weight,
      priority: route.priority,
      enabled: route.enabled,
      max_concurrency: route.max_concurrency,
      capacity_group: route.capacity_group,
      capacity_limit: route.capacity_limit,
      connect_timeout: route.connect_timeout,
      read_timeout: route.read_timeout,
      max_connections: route.max_connections,
      max_connections_per_host: route.max_connections_per_host,
      keepalive_timeout: route.keepalive_timeout,
    });
    setEditingRoute(route);
    setShowRouteForm(true);
    setRouteActionError("");
  };

  const runtimeByRoute = new Map<string, RuntimeRoute[]>();
  for (const snapshot of routeRuntime?.snapshots || []) {
    for (const route of snapshot.routes) {
      const existing = runtimeByRoute.get(route.route_id) || [];
      existing.push(route);
      runtimeByRoute.set(route.route_id, existing);
    }
  }
  const routeProviderOptions = Array.from(new Set(
    [
      ...providers.map((provider) => canonicalProvider(provider.name)),
      ...routes.map((route) => route.provider),
    ],
  ));

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

      {/* Concrete provider routes */}
      <section id="provider-routes" className="space-y-4 pt-2">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-sky-50 text-sky-700">
              <Waypoints className="h-4.5 w-4.5" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-stone-900">Provider Routes</h2>
              <p className="text-xs text-stone-500">
                Credential, endpoint, capacity, and transport assignments
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {routeRuntime && (
              <span className="text-xs text-stone-500">
                {routeRuntime.reachable}/{routeRuntime.gateways} gateways reachable
              </span>
            )}
            <button
              onClick={() => pushRoutesMutation.mutate()}
              disabled={pushRoutesMutation.isPending}
              className="btn-secondary"
            >
              {pushRoutesMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Upload className="h-4 w-4" />
              )}
              Push Routes
            </button>
            <button
              onClick={startRouteCreate}
              disabled={providers.length === 0}
              className="btn-primary"
            >
              <Plus className="h-4 w-4" /> Add Route
            </button>
          </div>
        </div>

        {routeActionError && (
          <p className="text-sm font-medium text-rose-600">{routeActionError}</p>
        )}
        {pushResult && (
          <p className={`text-xs font-medium ${pushResult.failed ? "text-amber-700" : "text-emerald-700"}`}>
            Pushed {pushResult.routes} routes to {pushResult.pushed} of {pushResult.gateways} gateways
            {pushResult.skipped ? `; ${pushResult.skipped} skipped` : ""}
            {pushResult.failed ? `; ${pushResult.failed} failed` : ""}
          </p>
        )}

        {showRouteForm && (
          <div className="card space-y-5 p-6">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-stone-800">
                {editingRoute ? `Edit: ${editingRoute.route_id}` : "Add Provider Route"}
              </h3>
              <button
                onClick={() => {
                  setShowRouteForm(false);
                  setEditingRoute(null);
                }}
                title="Close"
                className="rounded-lg p-1.5 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Provider</label>
                <select
                  value={routeForm.provider}
                  disabled={!!editingRoute}
                  onChange={(e) => {
                    const provider = e.target.value;
                    setRouteForm({
                      ...routeForm,
                      provider,
                      route_id: `${provider}:primary`,
                      auth_type: defaultAuthType(provider),
                    });
                  }}
                  className="input mt-1"
                >
                  {routeProviderOptions.map((provider) => (
                    <option key={provider} value={provider}>{provider}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Route ID</label>
                <input
                  value={routeForm.route_id}
                  disabled={!!editingRoute}
                  onChange={(e) => setRouteForm({ ...routeForm, route_id: e.target.value })}
                  className="input mt-1 font-mono"
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Authentication</label>
                <select
                  value={routeForm.auth_type}
                  onChange={(e) => setRouteForm({ ...routeForm, auth_type: e.target.value })}
                  className="input mt-1"
                >
                  <option value="api_key">API key</option>
                  <option value="azure_key">Azure key</option>
                  <option value="aws_credentials">AWS credentials</option>
                  <option value="gcp_service_account">GCP access token</option>
                </select>
              </div>
              <div className="flex items-end">
                <label className="flex items-center gap-2 pb-2 text-sm text-stone-600">
                  <input
                    type="checkbox"
                    checked={routeForm.enabled}
                    onChange={(e) => setRouteForm({ ...routeForm, enabled: e.target.checked })}
                    className="rounded"
                  />
                  Enabled
                </label>
              </div>

              <div className="md:col-span-2 lg:col-span-3">
                <label className="text-xs font-medium uppercase text-stone-500">Endpoint</label>
                <input
                  value={routeForm.endpoint}
                  onChange={(e) => setRouteForm({ ...routeForm, endpoint: e.target.value })}
                  placeholder="https://api.provider.example"
                  className="input mt-1 font-mono"
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Region</label>
                <input
                  value={routeForm.region}
                  onChange={(e) => setRouteForm({ ...routeForm, region: e.target.value })}
                  placeholder="us-east-1"
                  className="input mt-1"
                />
              </div>

              {(routeForm.auth_type === "api_key" || routeForm.auth_type === "azure_key") && (
                <div className="md:col-span-2">
                  <label className="text-xs font-medium uppercase text-stone-500">API Key</label>
                  <input
                    type="password"
                    value={routeForm.api_key}
                    onChange={(e) => setRouteForm({ ...routeForm, api_key: e.target.value })}
                    placeholder={editingRoute ? "Leave blank to retain stored credential" : "Provider API key"}
                    className="input mt-1 font-mono"
                  />
                </div>
              )}
              {routeForm.auth_type === "gcp_service_account" && (
                <div className="md:col-span-2">
                  <label className="text-xs font-medium uppercase text-stone-500">Access Token</label>
                  <input
                    type="password"
                    value={routeForm.access_token}
                    onChange={(e) => setRouteForm({ ...routeForm, access_token: e.target.value })}
                    placeholder={editingRoute ? "Leave blank to retain stored credential" : "GCP access token"}
                    className="input mt-1 font-mono"
                  />
                </div>
              )}
              {routeForm.auth_type === "aws_credentials" && (
                <>
                  <div>
                    <label className="text-xs font-medium uppercase text-stone-500">Access Key ID</label>
                    <input
                      type="password"
                      value={routeForm.access_key}
                      onChange={(e) => setRouteForm({ ...routeForm, access_key: e.target.value })}
                      className="input mt-1 font-mono"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-medium uppercase text-stone-500">Secret Access Key</label>
                    <input
                      type="password"
                      value={routeForm.secret_key}
                      onChange={(e) => setRouteForm({ ...routeForm, secret_key: e.target.value })}
                      className="input mt-1 font-mono"
                    />
                  </div>
                  <div className="md:col-span-2">
                    <label className="text-xs font-medium uppercase text-stone-500">Session Token</label>
                    <input
                      type="password"
                      value={routeForm.session_token}
                      onChange={(e) => setRouteForm({ ...routeForm, session_token: e.target.value })}
                      className="input mt-1 font-mono"
                    />
                  </div>
                </>
              )}

              <div className="md:col-span-2">
                <label className="text-xs font-medium uppercase text-stone-500">Allowed Models</label>
                <input
                  value={routeForm.allowed_models}
                  onChange={(e) => setRouteForm({ ...routeForm, allowed_models: e.target.value })}
                  placeholder="gpt-4o, gpt-4o-mini"
                  className="input mt-1 font-mono"
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Static Weight</label>
                <input
                  type="number"
                  min={0.01}
                  step={0.1}
                  value={routeForm.weight}
                  onChange={(e) => setRouteForm({ ...routeForm, weight: Number(e.target.value) })}
                  className="input mt-1"
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Priority</label>
                <input
                  type="number"
                  min={0}
                  value={routeForm.priority}
                  onChange={(e) => setRouteForm({ ...routeForm, priority: Number(e.target.value) })}
                  className="input mt-1"
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Max Concurrency</label>
                <input
                  type="number"
                  min={1}
                  value={routeForm.max_concurrency}
                  onChange={(e) => setRouteForm({ ...routeForm, max_concurrency: Number(e.target.value) })}
                  className="input mt-1"
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Capacity Group</label>
                <input
                  value={routeForm.capacity_group}
                  onChange={(e) => setRouteForm({ ...routeForm, capacity_group: e.target.value })}
                  placeholder="provider-account-a"
                  className="input mt-1"
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase text-stone-500">Group Limit</label>
                <input
                  type="number"
                  min={0}
                  value={routeForm.capacity_limit}
                  onChange={(e) => setRouteForm({ ...routeForm, capacity_limit: Number(e.target.value) })}
                  className="input mt-1"
                />
              </div>
            </div>

            <details className="border-t border-stone-200 pt-4">
              <summary className="cursor-pointer text-xs font-semibold uppercase text-stone-500">
                Transport Limits
              </summary>
              <div className="mt-4 grid gap-4 md:grid-cols-2 lg:grid-cols-5">
                <div>
                  <label className="text-xs font-medium uppercase text-stone-500">Connect Timeout</label>
                  <input
                    type="number"
                    min={0.1}
                    step={0.1}
                    value={routeForm.connect_timeout}
                    onChange={(e) => setRouteForm({ ...routeForm, connect_timeout: Number(e.target.value) })}
                    className="input mt-1"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium uppercase text-stone-500">Read Timeout</label>
                  <input
                    type="number"
                    min={0.1}
                    step={0.1}
                    value={routeForm.read_timeout}
                    onChange={(e) => setRouteForm({ ...routeForm, read_timeout: Number(e.target.value) })}
                    className="input mt-1"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium uppercase text-stone-500">Connections</label>
                  <input
                    type="number"
                    min={1}
                    value={routeForm.max_connections}
                    onChange={(e) => setRouteForm({ ...routeForm, max_connections: Number(e.target.value) })}
                    className="input mt-1"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium uppercase text-stone-500">Per Host</label>
                  <input
                    type="number"
                    min={1}
                    value={routeForm.max_connections_per_host}
                    onChange={(e) => setRouteForm({ ...routeForm, max_connections_per_host: Number(e.target.value) })}
                    className="input mt-1"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium uppercase text-stone-500">Keepalive</label>
                  <input
                    type="number"
                    min={0.1}
                    step={0.1}
                    value={routeForm.keepalive_timeout}
                    onChange={(e) => setRouteForm({ ...routeForm, keepalive_timeout: Number(e.target.value) })}
                    className="input mt-1"
                  />
                </div>
              </div>
            </details>

            <div className="flex items-center gap-2">
              <button
                onClick={() => routeMutation.mutate(routeForm)}
                disabled={routeMutation.isPending || !routeForm.route_id}
                className="btn-primary"
              >
                {routeMutation.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Waypoints className="h-4 w-4" />
                )}
                {editingRoute ? "Save Route" : "Add Route"}
              </button>
              <button
                onClick={() => {
                  setShowRouteForm(false);
                  setEditingRoute(null);
                }}
                className="btn-secondary"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        <div className="card overflow-x-auto">
          {routesLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-stone-400" />
            </div>
          ) : routes.length === 0 ? (
            <div className="flex items-center gap-3 px-5 py-8 text-sm text-stone-500">
              <Waypoints className="h-5 w-5 text-stone-300" />
              No explicit routes configured
            </div>
          ) : (
            <table className="w-full min-w-[1050px]">
              <thead>
                <tr className="border-b border-stone-200 bg-stone-50 text-left text-[10px] font-semibold uppercase text-stone-500">
                  <th className="px-5 py-3">Route</th>
                  <th className="px-4 py-3">Endpoint</th>
                  <th className="px-4 py-3">Selection</th>
                  <th className="px-4 py-3">Capacity</th>
                  <th className="px-4 py-3">Runtime</th>
                  <th className="px-4 py-3">Credential</th>
                  <th className="px-4 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-100">
                {routes.map((route) => {
                  const runtime = runtimeByRoute.get(route.route_id) || [];
                  const hasFailure = runtime.some((sample) => sample.status === "failed");
                  const hasDegraded = runtime.some((sample) => ["degraded", "recovering"].includes(sample.status));
                  const status = runtime.length === 0
                    ? "Not pushed"
                    : hasFailure
                      ? "Failed"
                      : hasDegraded
                        ? "Degraded"
                        : "Healthy";
                  const statusClass = status === "Healthy"
                    ? "bg-emerald-50 text-emerald-700"
                    : status === "Failed"
                      ? "bg-rose-50 text-rose-700"
                      : status === "Degraded"
                        ? "bg-amber-50 text-amber-700"
                        : "bg-stone-100 text-stone-500";
                  const adaptiveWeight = runtime.length
                    ? runtime.reduce((sum, sample) => sum + (sample.adaptive_weight || 0), 0) / runtime.length
                    : null;
                  const inflight = runtime.reduce((sum, sample) => sum + (sample.inflight || 0), 0);
                  const successes = runtime.reduce((sum, sample) => sum + (sample.successes || 0), 0);
                  const failures = runtime.reduce((sum, sample) => sum + (sample.failures || 0), 0);

                  return (
                    <tr key={route.route_id} className={!route.enabled ? "opacity-55" : ""}>
                      <td className="px-5 py-4">
                        <div className="flex items-center gap-2">
                          <Waypoints className="h-4 w-4 text-sky-600" />
                          <div>
                            <p className="font-mono text-xs font-semibold text-stone-800">{route.route_id}</p>
                            <p className="mt-0.5 text-[10px] text-stone-500">{route.provider}</p>
                          </div>
                        </div>
                      </td>
                      <td className="max-w-[260px] px-4 py-4">
                        <p className="truncate font-mono text-xs text-stone-700">
                          {route.endpoint || "Provider default"}
                        </p>
                        <p className="mt-1 text-[10px] text-stone-500">
                          {route.region || "Global"} · {route.allowed_models.length || "all"} models
                        </p>
                      </td>
                      <td className="px-4 py-4">
                        <div className="flex items-center gap-2 text-xs text-stone-700">
                          <Gauge className="h-3.5 w-3.5 text-stone-400" />
                          <span>{route.weight.toFixed(2)}</span>
                          <span className="text-stone-300">→</span>
                          <span className="font-semibold">
                            {adaptiveWeight == null ? "--" : adaptiveWeight.toFixed(3)}
                          </span>
                        </div>
                        <p className="mt-1 text-[10px] text-stone-500">Priority {route.priority}</p>
                      </td>
                      <td className="px-4 py-4 text-xs text-stone-700">
                        <p>{route.max_concurrency} concurrent</p>
                        <p className="mt-1 text-[10px] text-stone-500">
                          {route.capacity_group
                            ? `${route.capacity_group} · ${route.capacity_limit || "unlimited"}`
                            : "Independent"}
                        </p>
                      </td>
                      <td className="px-4 py-4">
                        <span className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium ${statusClass}`}>
                          {status}
                        </span>
                        <p className="mt-1 text-[10px] text-stone-500">
                          {inflight} active · {successes}/{failures} ok/fail
                        </p>
                      </td>
                      <td className="px-4 py-4">
                        <div className="flex items-center gap-1.5 text-xs text-stone-700">
                          <LockKeyhole className="h-3.5 w-3.5 text-stone-400" />
                          {route.has_credentials ? "Stored" : "Workload identity"}
                        </div>
                        <p className="mt-1 text-[10px] text-stone-500">{route.auth_type}</p>
                      </td>
                      <td className="px-4 py-4">
                        <div className="flex items-center justify-end gap-1">
                          <button
                            onClick={() => startRouteEdit(route)}
                            title="Edit route"
                            className="rounded-lg p-1.5 text-stone-400 transition hover:bg-sky-50 hover:text-sky-700"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                          <button
                            onClick={() => deleteRouteMutation.mutate(route)}
                            title="Delete route"
                            className="rounded-lg p-1.5 text-stone-400 transition hover:bg-rose-50 hover:text-rose-600"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

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
