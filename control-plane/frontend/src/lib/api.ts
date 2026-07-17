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
};
