const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";
const TOKEN_KEY = "ostiari_token";

async function fetchAPI<T>(path: string, options?: RequestInit): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    window.location.href = "/login";
    throw new Error("Session expired");
  }

  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(error.detail || `HTTP ${res.status}`);
  }
  return res.json();
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
  policies: {
    list: () => fetchAPI<Policy[]>("/api/policies"),
    create: (data: { name: string; content: Record<string, unknown>; gateway_id?: string }) =>
      fetchAPI<Policy>("/api/policies", { method: "POST", body: JSON.stringify(data) }),
    update: (id: number, data: Partial<Policy>) =>
      fetchAPI<Policy>(`/api/policies/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: number) => fetchAPI(`/api/policies/${id}`, { method: "DELETE" }),
    push: (id: number) => fetchAPI(`/api/policies/${id}/push`, { method: "POST" }),
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
    pricing: (gatewayId = "crm-agent") =>
      fetchAPI<Pricing>(`/api/payments/pricing?gateway_id=${gatewayId}`),
    push: (gatewayId = "crm-agent") =>
      fetchAPI(`/api/payments/push?gateway_id=${gatewayId}`, { method: "POST" }),
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
    onboard: (agent_id: string, gateway_id: string, framework = "other") =>
      fetchAPI(`/api/discovery/onboard`, { method: "POST", body: JSON.stringify({ agent_id, gateway_id, framework }) }),
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
    collector: () => fetchAPI<{ mode: string }>("/api/token-broker/pilot/collector"),
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
  status: "discovered" | "governed" | "governed_unseen";
  registered: boolean;
  sources: string[];
  gateways: string[];
  call_count: number;
  confidence: number;
  evidence: string[];
}

export interface DiscoveryReport {
  summary: { total: number; shadow: number; governed: number; stale: number; sources: string[] };
  agents: DiscoveredAgent[];
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
  agent_id: string;
  gateway_id: string;
  action: string;
  amount_usdc: number;
  settled: boolean;
  tx_hash: string;
  mode: string;
  source: string;
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
