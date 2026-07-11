import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Plug, Server, Globe, Terminal, Search, Wrench } from "lucide-react";
import { api, McpServer, Gateway } from "../lib/api";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";

const MODE_ICONS = { embedded: Plug, remote: Globe, stdio: Terminal };
const MODE_COLORS = {
  embedded: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200",
  remote: "bg-sky-50 text-sky-700 ring-1 ring-inset ring-sky-200",
  stdio: "bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-200",
};

export function McpServers() {
  const queryClient = useQueryClient();
  const { data: mcpServers = [] } = useQuery({ queryKey: ["mcp-servers"], queryFn: () => api.mcpServers.list() });
  const { data: gateways = [] } = useQuery({ queryKey: ["gateways"], queryFn: api.gateways.list });
  const [discoveredTools, setDiscoveredTools] = useState<Record<number, any[]>>({});
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    name: "", mode: "embedded" as "embedded" | "remote" | "stdio",
    package: "", url: "", command: "", gateway_id: "",
    config: "", allowed_tools: "", blocked_tools: "", prefix: "",
  });

  const createMutation = useMutation({
    mutationFn: (data: typeof form) => {
      const payload: any = {
        name: data.name,
        mode: data.mode,
        prefix: data.prefix || data.name,
      };
      if (data.mode === "embedded") payload.package = data.package;
      if (data.mode === "remote") payload.url = data.url;
      if (data.mode === "stdio") payload.command = data.command.split(" ").filter(Boolean);
      if (data.config) {
        try { payload.config = JSON.parse(data.config); } catch {}
      }
      if (data.allowed_tools) payload.allowed_tools = data.allowed_tools.split(",").map(s => s.trim());
      if (data.blocked_tools) payload.blocked_tools = data.blocked_tools.split(",").map(s => s.trim());
      return api.mcpServers.create(data.gateway_id, payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      setShowForm(false);
      setForm({ name: "", mode: "embedded", package: "", url: "", command: "", gateway_id: "", config: "", allowed_tools: "", blocked_tools: "", prefix: "" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: api.mcpServers.delete,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">MCP Servers</h1>
          <p className="mt-1 text-sm text-stone-500">Connect MCP tool servers to gateways — embedded, remote, or local process</p>
        </div>
        <button onClick={() => setShowForm(true)} className="btn-teal">
          <Plus className="h-4 w-4" /> Add MCP Server
        </button>
      </div>

      {showForm && (
        <form
          onSubmit={(e) => { e.preventDefault(); createMutation.mutate(form); }}
          className="card p-6 space-y-4"
        >
          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Server name (e.g., github)" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="input" />
            <select value={form.gateway_id} onChange={(e) => setForm({ ...form, gateway_id: e.target.value })} className="input">
              <option value="">Select gateway...</option>
              {gateways.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>

          <div className="flex gap-2">
            {(["embedded", "remote", "stdio"] as const).map(m => (
              <button key={m} type="button" onClick={() => setForm({ ...form, mode: m })}
                className={`rounded-xl border px-3 py-2 text-sm transition ${form.mode === m ? "border-violet-400 bg-violet-50 text-violet-700" : "border-stone-200 text-stone-500 hover:border-stone-300"}`}
              >
                {m === "embedded" && "Embedded (in-process)"}
                {m === "remote" && "Remote (HTTP/SSE)"}
                {m === "stdio" && "Stdio (subprocess)"}
              </button>
            ))}
          </div>

          {form.mode === "embedded" && (
            <input placeholder="Python package (e.g., mcp-server-github)" value={form.package} onChange={(e) => setForm({ ...form, package: e.target.value })} className="input w-full" />
          )}
          {form.mode === "remote" && (
            <input placeholder="MCP server URL (e.g., http://mcp-server:3000/mcp)" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} className="input w-full" />
          )}
          {form.mode === "stdio" && (
            <input placeholder="Command (e.g., npx @modelcontextprotocol/server-filesystem /data)" value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })} className="input w-full" />
          )}

          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Prefix (default: server name)" value={form.prefix} onChange={(e) => setForm({ ...form, prefix: e.target.value })} className="input" />
            <input placeholder="Blocked tools (comma-separated)" value={form.blocked_tools} onChange={(e) => setForm({ ...form, blocked_tools: e.target.value })} className="input" />
          </div>

          <textarea placeholder='Config JSON (e.g., {"token": "ghp_xxx"})' value={form.config} onChange={(e) => setForm({ ...form, config: e.target.value })} rows={2} className="input w-full font-mono" />

          <div className="flex gap-2">
            <button type="submit" className="btn-teal">Add Server</button>
            <button type="button" onClick={() => setShowForm(false)} className="btn-secondary">Cancel</button>
          </div>
        </form>
      )}

      <div className="grid gap-4">
        {mcpServers.map((m) => {
          const ModeIcon = MODE_ICONS[m.mode] || Server;
          return (
            <div key={m.id} className="card p-6">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-50">
                    <ModeIcon className="h-4 w-4 text-indigo-600" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-stone-900">{m.name}</p>
                    <div className="mt-1 flex items-center gap-2">
                      <span className={`badge ${MODE_COLORS[m.mode]}`}>{m.mode}</span>
                      <span className="text-xs text-stone-500">{m.gateway_id.split("-").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ")} Gateway</span>
                      <span className="text-xs text-stone-400">prefix: {m.prefix}</span>
                    </div>
                  </div>
                </div>
                <div className="flex gap-1">
                  <button
                    onClick={async () => {
                      const resp = await fetch(`${API_BASE}/api/mcp-servers/${m.id}/tools`);
                      const data = await resp.json();
                      setDiscoveredTools(prev => ({ ...prev, [m.id]: data.tools || [] }));
                    }}
                    title="Discover tools"
                    className="rounded-xl p-2 text-stone-400 hover:bg-violet-50 hover:text-violet-600 transition"
                  >
                    <Search className="h-4 w-4" />
                  </button>
                  <button onClick={() => deleteMutation.mutate(m.id)} title="Delete" className="rounded-xl p-2 text-stone-400 hover:bg-rose-50 hover:text-rose-600 transition">
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
              <div className="mt-3 rounded-xl bg-stone-50 p-3 text-xs text-stone-600 font-mono space-y-1 border border-stone-100">
                <div>
                  {m.mode === "embedded" && <span>package: {m.package || m.module}</span>}
                  {m.mode === "remote" && <span>url: {m.url}</span>}
                  {m.mode === "stdio" && <span>command: {m.command.join(" ")}</span>}
                </div>
                <div className="flex gap-4">
                  <span className="text-emerald-600">
                    allowed: {m.allowed_tools ? m.allowed_tools.join(", ") : "all tools"}
                  </span>
                  {m.blocked_tools.length > 0 && (
                    <span className="text-rose-600">blocked: {m.blocked_tools.join(", ")}</span>
                  )}
                </div>
              </div>
              {discoveredTools[m.id] && discoveredTools[m.id].length > 0 && (
                <div className="mt-3 rounded-xl border border-stone-200 bg-stone-50 p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <Wrench className="h-3.5 w-3.5 text-amber-600" />
                    <span className="text-xs font-medium text-stone-700">Discovered Tools ({discoveredTools[m.id].length})</span>
                  </div>
                  <div className="grid grid-cols-2 gap-1">
                    {discoveredTools[m.id].map((tool: any) => (
                      <div key={tool.name} className="flex items-center gap-2 text-xs">
                        <span className="text-amber-700 font-mono">{tool.name}</span>
                        {tool.description && <span className="text-stone-500 truncate">— {tool.description}</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
        {mcpServers.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-12">
            <Plug className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No MCP servers configured. Add one to auto-discover tools.</p>
          </div>
        )}
      </div>
    </div>
  );
}
