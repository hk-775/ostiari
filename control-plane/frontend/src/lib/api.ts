const configuredApiBase = import.meta.env.VITE_API_URL;

// An explicitly empty value means same-origin routing through an ingress or ALB.
// Undefined retains the direct-backend default used by local frontend tooling.
export const API_BASE = (
  configuredApiBase === undefined ? "http://localhost:8400" : configuredApiBase
).replace(/\/$/, "");

export function websocketBase(): string {
  const base = API_BASE || window.location.origin;
  return base.replace(/^http/, "ws");
}
const TOKEN_KEY = "ostiari_token";

export class APIError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "APIError";
    this.status = status;
  }
}

async function responseError(res: Response): Promise<APIError> {
  const text = await res.text();
  let detail = res.statusText || `HTTP ${res.status}`;
  if (text) {
    try {
      const body = JSON.parse(text);
      detail = body.detail || body.error || body.message || detail;
    } catch {
      detail = text;
    }
  }
  return new APIError(res.status, detail);
}

export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const token = localStorage.getItem(TOKEN_KEY);
  const headers = new Headers(options?.headers);
  if (options?.body && typeof options.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    if (window.location.pathname !== "/login") {
      window.location.assign("/login");
    }
    throw new APIError(401, "Session expired");
  }

  return res;
}

export async function requireOk(res: Response): Promise<Response> {
  if (!res.ok) {
    throw await responseError(res);
  }
  return res;
}

export async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await requireOk(await apiFetch(path, options));
  if (res.status === 204) {
    return undefined as T;
  }
  const text = await res.text();
  return text ? JSON.parse(text) as T : undefined as T;
}

export interface Gateway {
  id: string;
  name: string;
  endpoint: string;
  description: string;
  status: string;
  last_heartbeat: string | null;
  tools_count: number;
  mode: "enforce" | "shadow";
  created_at: string;
  updated_at: string;
}

export interface Agent {
  name: string;
  framework: string;
  gateway_id: string;
  tools: string[];
  description: string;
  status: string;
  model: string;
}

export interface Quota {
  id: number;
  name: string;
  scope: string;
  scope_id: string;
  gateway_id: string;
  rate_limit_rpm: number | null;
  budget_limit_usd: number | null;
  max_tokens_per_request: number | null;
  allowed_models: string[];
  allowed_providers: string[];
  alert_threshold_pct: number;
  current_spend: number;
  current_rpm: number;
}

export interface Tool {
  id: number;
  name: string;
  endpoint: string;
  method: string;
  description: string;
  timeout_seconds: number;
  gateway_id: string;
}

export interface Policy {
  id: number;
  name: string;
  description: string;
  content: Record<string, unknown>;
  is_active: boolean;
  gateway_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface PushResult {
  gateway_id: string;
  status: string;
  message: string;
}

export interface PushResponse {
  results: PushResult[];
  total: number;
  succeeded: number;
  failed: number;
}

export interface McpServer {
  id: number;
  name: string;
  mode: "embedded" | "remote" | "stdio";
  package: string;
  module: string;
  url: string;
  command: string[];
  config: Record<string, unknown>;
  allowed_tools: string[] | null;
  blocked_tools: string[];
  prefix: string;
  gateway_id: string;
  created_at: string;
}

export interface SandboxRun {
  id: string;
  gateway_id: string;
  language: "javascript";
  source_digest: string;
  source_bytes: number;
  status: "running" | "completed" | "error" | "cancelled" | "timed_out";
  timeout_ms: number;
  max_tool_calls: number;
  max_output_bytes: number;
  max_tool_payload_bytes: number;
  tool_calls: number;
  output_bytes: number;
  error: string;
  started_at: string;
  completed_at: string | null;
}

export const api = {
  gateways: {
    list: () => fetchAPI<Gateway[]>("/api/gateways"),
    get: (id: string) => fetchAPI<Gateway>(`/api/gateways/${id}`),
    create: (data: { id: string; name: string; endpoint: string; description?: string }) =>
      fetchAPI<Gateway>("/api/gateways", { method: "POST", body: JSON.stringify(data) }),
    delete: (id: string) => fetchAPI(`/api/gateways/${id}`, { method: "DELETE" }),
    setMode: (id: string, mode: "enforce" | "shadow") =>
      fetchAPI<Gateway>(`/api/gateways/${id}/mode`, { method: "PUT", body: JSON.stringify({ mode }) }),
    push: (id: string) => fetchAPI(`/api/gateways/${id}/push`, { method: "POST" }),
    pushConfig: (id: string, config: Record<string, unknown>) =>
      fetchAPI<{ status: string; gateway_id: string; reason?: string }>(`/api/gateways/${id}/push-config`, {
        method: "POST",
        body: JSON.stringify(config),
      }),
    pushAll: () => fetchAPI("/api/gateways/push-all", { method: "POST" }),
    health: (id: string) => fetchAPI(`/api/gateways/${id}/health`),
  },
  agents: {
    list: () => fetchAPI<Agent[]>("/api/agents"),
  },
  quotas: {
    list: (scope?: string) =>
      fetchAPI<Quota[]>(`/api/quotas${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`),
    create: (data: Omit<Quota, "id" | "current_spend" | "current_rpm">) =>
      fetchAPI<Quota>("/api/quotas", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    update: (
      id: number,
      data: Partial<Omit<Quota, "id" | "scope" | "scope_id" | "current_spend" | "current_rpm">>,
    ) =>
      fetchAPI<Quota>(`/api/quotas/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    delete: (id: number) =>
      fetchAPI<{ deleted: number; scope: string; gateway_id: string }>(
        `/api/quotas/${id}`,
        { method: "DELETE" },
      ),
    push: (id: number) =>
      fetchAPI<{ status: string; gateway?: string; detail?: string; reason?: string }>(
        `/api/quotas/${id}/push`,
        { method: "POST" },
      ),
    pushAgents: (gatewayId: string) =>
      fetchAPI<{
        status: string;
        gateway: string;
        agents: number;
        detail?: string;
        reason?: string;
      }>(`/api/quotas/agents/push?gateway_id=${encodeURIComponent(gatewayId)}`, {
        method: "POST",
      }),
  },
  tools: {
    list: (gatewayId?: string) =>
      fetchAPI<Tool[]>(`/api/tools${gatewayId ? `?gateway_id=${gatewayId}` : ""}`),
    create: (gatewayId: string, data: Partial<Tool>) =>
      fetchAPI<Tool>(`/api/tools/${gatewayId}`, { method: "POST", body: JSON.stringify(data) }),
    delete: (id: number) => fetchAPI(`/api/tools/${id}`, { method: "DELETE" }),
    importOpenapi: (
      gatewayId: string,
      data: {
        source?: string;
        server_url?: string | null;
        name_prefix?: string;
        replace?: boolean;
        preview?: boolean;
      },
    ) =>
      fetchAPI<{
        status: string;
        count: number;
        tools: {
          name: string;
          method: string;
          endpoint: string;
          description: string;
          path_params: string[];
          query_params: string[];
        }[];
      }>(`/api/tools/${gatewayId}/import-openapi`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
  },
  sandbox: {
    start: (data: {
      gateway_id: string;
      language: "javascript";
      source_digest: string;
      source_bytes: number;
    }) =>
      fetchAPI<SandboxRun>("/api/sandbox/runs", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    complete: (
      id: string,
      data: {
        status: "completed" | "error" | "cancelled" | "timed_out";
        duration_ms: number;
        output_bytes: number;
        error: string;
      },
    ) =>
      fetchAPI<SandboxRun>(`/api/sandbox/runs/${encodeURIComponent(id)}/complete`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
    cancel: (id: string) =>
      fetchAPI<SandboxRun>(`/api/sandbox/runs/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
  },
  policies: {
    list: () => fetchAPI<Policy[]>("/api/policies"),
    create: (data: { name: string; content: Record<string, unknown>; gateway_id?: string }) =>
      fetchAPI<Policy>("/api/policies", { method: "POST", body: JSON.stringify(data) }),
    update: (id: number, data: Partial<Policy>) =>
      fetchAPI<Policy>(`/api/policies/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: number) => fetchAPI(`/api/policies/${id}`, { method: "DELETE" }),
    push: (id: number) =>
      fetchAPI<PushResponse>(`/api/policies/${id}/push`, { method: "POST" }),
  },
  mcpServers: {
    list: (gatewayId?: string) =>
      fetchAPI<McpServer[]>(`/api/mcp-servers${gatewayId ? `?gateway_id=${gatewayId}` : ""}`),
    create: (gatewayId: string, data: Partial<McpServer>) =>
      fetchAPI<McpServer>(`/api/mcp-servers/${gatewayId}`, { method: "POST", body: JSON.stringify(data) }),
    delete: (id: number) => fetchAPI(`/api/mcp-servers/${id}`, { method: "DELETE" }),
  },
  payments: {
    wallets: () => fetchAPI<Wallet[]>("/api/payments/wallets"),
    upsert: (data: { agent_id: string; balance_usdc?: number; daily_limit_usdc?: number | null; per_call_limit_usdc?: number | null }) =>
      fetchAPI<Wallet>("/api/payments/wallets", { method: "POST", body: JSON.stringify(data) }),
    fund: (agentId: string, amount: number) =>
      fetchAPI<Wallet>(`/api/payments/wallets/${agentId}/fund`, {
        method: "POST", body: JSON.stringify({ amount_usdc: amount }),
      }),
    patchWallet: (agentId: string, data: Partial<Pick<Wallet, "daily_limit_usdc" | "per_call_limit_usdc" | "status">>) =>
      fetchAPI<Wallet>(`/api/payments/wallets/${agentId}`, { method: "PATCH", body: JSON.stringify(data) }),
    ledger: (agentId?: string) =>
      fetchAPI<PaymentRecord[]>(`/api/payments/ledger${agentId ? `?agent_id=${agentId}` : ""}`),
    summary: () => fetchAPI<PaymentSummary>("/api/payments/summary"),
    pricing: (gatewayId: string) =>
      fetchAPI<Pricing>(`/api/payments/pricing?gateway_id=${encodeURIComponent(gatewayId)}`),
    push: (gatewayId: string) =>
      fetchAPI(`/api/payments/push?gateway_id=${encodeURIComponent(gatewayId)}`, { method: "POST" }),
  },
  roi: {
    report: (weightByScore = true) =>
      fetchAPI<RoiReport>(`/api/roi/report?weight_by_score=${weightByScore}`),
    costModel: () => fetchAPI<CostModel>("/api/roi/cost-model"),
    setCostModel: (entries: { pattern: string; cost: number }[], fallback: number) =>
      fetchAPI<CostModel>("/api/roi/cost-model", { method: "POST", body: JSON.stringify({ entries, fallback }) }),
    resetCostModel: () => fetchAPI<CostModel>("/api/roi/cost-model/reset", { method: "POST" }),
  },
  approvals: {
    list: () => fetchAPI<ApprovalItem[]>("/api/approvals"),
    all: () => fetchAPI<ApprovalItem[]>("/api/approvals/all"),
    decide: (id: string, decision: "approve" | "deny", decided_by = "operator") =>
      fetchAPI<ApprovalItem>(`/api/approvals/${id}/decision`, {
        method: "POST", body: JSON.stringify({ decision, decided_by }),
      }),
  },
  discovery: {
    agents: () => fetchAPI<DiscoveryReport>("/api/discovery/agents"),
    onboard: (data: DiscoveryOnboardRequest) =>
      fetchAPI<DiscoveryOnboardResponse>("/api/discovery/onboard", {
        method: "POST",
        body: JSON.stringify(data),
      }),
  },
  tokenBroker: {
    report: (periodDays = 30) =>
      fetchAPI<BrokerReport>(`/api/token-broker/report?period_days=${periodDays}`),
    config: () => fetchAPI<BrokerConfig>("/api/token-broker/config"),
    setConfig: (bulk_discount: number, markup: number) =>
      fetchAPI<BrokerConfig>("/api/token-broker/config", { method: "POST", body: JSON.stringify({ bulk_discount, markup }) }),
    resetConfig: () => fetchAPI<BrokerConfig>("/api/token-broker/config/reset", { method: "POST" }),
    pools: () => fetchAPI<TokenPool[]>("/api/token-broker/pilot/pools"),
    fundPool: (data: { provider: string; tokens: number; cost_usd: number; low_threshold_tokens?: number }) =>
      fetchAPI<TokenPool>("/api/token-broker/pilot/pools/fund", { method: "POST", body: JSON.stringify(data) }),
    reconcile: (provider: string, invoiced_cost_usd: number, period_days = 30) =>
      fetchAPI<Reconciliation>("/api/token-broker/pilot/reconcile", { method: "POST", body: JSON.stringify({ provider, invoiced_cost_usd, period_days }) }),
    reconciliations: () => fetchAPI<Reconciliation[]>("/api/token-broker/pilot/reconciliations"),
    collector: () => fetchAPI<CollectorInfo>("/api/token-broker/pilot/collector"),
  },
};

export interface TokenPool {
  provider: string;
  purchased_tokens: number;
  purchased_cost_usd: number;
  consumed_tokens: number;
  consumed_cost_usd: number;
  remaining_tokens: number;
  remaining_pct: number;
  low_threshold_tokens: number;
  status: "active" | "depleted";
}

export interface Reconciliation {
  id: number;
  provider: string;
  period_start: string;
  period_end: string;
  computed_cost_usd: number;
  invoiced_cost_usd: number;
  drift_usd: number;
  drift_pct: number;
  consumed_tokens: number;
}

export interface CollectorInfo {
  mode: string;
  configured?: boolean;
  meter_event_name?: string;
  customer_mappings?: number;
  default_customer?: boolean;
}

export interface ApprovalItem {
  id: string;
  agent_id: string;
  gateway_id: string;
  action: string;
  params: Record<string, unknown>;
  score: number;
  reason: string;
  status: "pending" | "approved" | "denied" | "expired";
  decided_by: string;
  decided_at: string;
  created_at: string;
}

export interface DiscoveredAgent {
  agent_id: string;
  status: "discovered" | "registered_off_gateway" | "governed" | "governed_unseen";
  registered: boolean;
  sources: string[];
  gateways: string[];
  governed_gateways: string[];
  assigned_gateway: string;
  call_count: number;
  confidence: number;
  evidence: string[];
}

export interface DiscoveryReport {
  summary: {
    total: number;
    shadow: number;
    off_gateway: number;
    governed: number;
    stale: number;
    sources: string[];
    source_status: { source: string; status: "ok" | "error"; detail: string }[];
  };
  agents: DiscoveredAgent[];
}

export interface DiscoveryOnboardRequest {
  agent_id: string;
  gateway_id: string;
  framework?: string;
  allowed_tools?: string[];
  allowed_models?: string[];
  allowed_providers?: string[];
}

export interface DiscoveryOnboardResponse {
  onboarded: string;
  registered: boolean;
  gateway_id: string;
  status: "registered_off_gateway" | "governed";
  traffic_routed: boolean;
  gateway_policy: {
    status: "pushed" | "queued" | "error";
    gateway: string;
    reason?: string;
    detail?: string;
  };
}

export interface BrokerModelRow {
  model: string;
  calls: number;
  tokens: number;
  retail_usd: number;
  our_cost_usd: number;
  charged_usd: number;
  customer_savings_usd: number;
  margin_usd: number;
}

export interface BrokerReport {
  period_days: number;
  bulk_discount: number;
  markup: number;
  total_retail_usd: number;
  total_our_cost_usd: number;
  total_charged_usd: number;
  total_tokens: number;
  customer_savings_usd: number;
  savings_pct: number;
  margin_usd: number;
  models: BrokerModelRow[];
}

export interface BrokerConfig {
  bulk_discount: number;
  markup: number;
  customized: boolean;
}

export interface RoiActionSaving {
  action: string;
  count: number;
  unit_cost: number;
  prevented_usd: number;
  max_score: number;
}

export interface RoiReport {
  blocked_count: number;
  distinct_actions: number;
  total_prevented_usd: number;
  fallback_cost: number;
  weight_by_score: boolean;
  actions: RoiActionSaving[];
}

export interface CostModel {
  entries: { pattern: string; cost: number }[];
  fallback: number;
  customized: boolean;
}

export interface Wallet {
  agent_id: string;
  address: string;
  balance_usdc: number;
  daily_limit_usdc: number | null;
  per_call_limit_usdc: number | null;
  spent_today_usdc: number;
  status: "active" | "paused";
}

export interface PaymentRecord {
  id: number;
  event_id: string;
  agent_id: string;
  gateway_id: string;
  action: string;
  amount_usdc: number;
  settled: boolean;
  wallet_debited: boolean;
  tx_hash: string;
  mode: string;
  source: string;
  reason: string;
  timestamp: string;
}

export interface PaymentSummary {
  total_settled_usdc: number;
  settled_count: number;
  blocked_count: number;
  fee_rate: number;
  fees_captured_usdc: number;
  by_agent: { agent_id: string; spent_usdc: number; calls: number }[];
}

export interface Pricing {
  gateway_id: string;
  mode: string;
  default: number;
  overrides: Record<string, number>;
}
