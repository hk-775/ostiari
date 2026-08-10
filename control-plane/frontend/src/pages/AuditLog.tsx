import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { History, Filter } from "lucide-react";
import { fetchAPI } from "../lib/api";

interface AuditEntry {
  id: number;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string;
  details: Record<string, unknown>;
  timestamp: string;
}

async function fetchAudit(filters: Record<string, string>): Promise<AuditEntry[]> {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v) params.set(k, v);
  }
  return fetchAPI<AuditEntry[]>(`/api/audit?${params}`);
}

const ACTION_COLORS: Record<string, string> = {
  create: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200",
  update: "bg-sky-50 text-sky-700 ring-1 ring-inset ring-sky-200",
  delete: "bg-rose-50 text-rose-700 ring-1 ring-inset ring-rose-200",
  push: "bg-violet-50 text-violet-700 ring-1 ring-inset ring-violet-200",
  push_all: "bg-violet-50 text-violet-700 ring-1 ring-inset ring-violet-200",
};

const RESOURCE_COLORS: Record<string, string> = {
  gateway: "bg-cyan-50 text-cyan-700 ring-1 ring-inset ring-cyan-200",
  policy: "bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-200",
  tool: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200",
  mcp_server: "bg-violet-50 text-violet-700 ring-1 ring-inset ring-violet-200",
};

export function AuditLog() {
  const [filters, setFilters] = useState({ resource_type: "", action: "", actor: "" });
  const { data: entries = [], isLoading } = useQuery({
    queryKey: ["audit", filters],
    queryFn: () => fetchAudit(filters),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Audit Log</h1>
        <p className="mt-1 text-sm text-stone-500">Who changed what config, when</p>
      </div>

      <div className="flex items-center gap-3">
        <Filter className="h-4 w-4 text-stone-400" />
        <select
          value={filters.resource_type}
          onChange={(e) => setFilters({ ...filters, resource_type: e.target.value })}
          className="input"
        >
          <option value="">All resources</option>
          <option value="gateway">Gateways</option>
          <option value="policy">Policies</option>
          <option value="tool">Tools</option>
          <option value="mcp_server">MCP Servers</option>
        </select>
        <select
          value={filters.action}
          onChange={(e) => setFilters({ ...filters, action: e.target.value })}
          className="input"
        >
          <option value="">All actions</option>
          <option value="create">Create</option>
          <option value="update">Update</option>
          <option value="delete">Delete</option>
          <option value="push">Push</option>
          <option value="push_all">Push All</option>
        </select>
        <input
          placeholder="Filter by actor..."
          value={filters.actor}
          onChange={(e) => setFilters({ ...filters, actor: e.target.value })}
          className="input"
        />
      </div>

      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-200 text-left text-xs font-medium text-stone-500 uppercase tracking-wider">
              <th className="px-6 py-3.5">Time</th>
              <th className="px-6 py-3.5">Actor</th>
              <th className="px-6 py-3.5">Action</th>
              <th className="px-6 py-3.5">Resource</th>
              <th className="px-6 py-3.5">Details</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-100">
            {entries.map((entry) => (
              <tr key={entry.id} className="transition hover:bg-stone-50">
                <td className="px-6 py-4 text-xs text-stone-500 whitespace-nowrap">
                  {new Date(entry.timestamp).toLocaleString()}
                </td>
                <td className="px-6 py-4">
                  <span className="text-sm text-stone-900">{entry.actor}</span>
                </td>
                <td className="px-6 py-4">
                  <span className={`badge ${ACTION_COLORS[entry.action] || "bg-stone-50 text-stone-500 ring-1 ring-inset ring-stone-200"}`}>
                    {entry.action}
                  </span>
                </td>
                <td className="px-6 py-4">
                  <div className="flex items-center gap-2">
                    <span className={`badge ${RESOURCE_COLORS[entry.resource_type] || "bg-stone-50 text-stone-500 ring-1 ring-inset ring-stone-200"}`}>
                      {entry.resource_type}
                    </span>
                    <span className="text-sm text-stone-700">{entry.resource_id}</span>
                  </div>
                </td>
                <td className="px-6 py-4">
                  {Object.keys(entry.details).length > 0 && (
                    <pre className="text-xs text-stone-500 max-w-xs truncate" title={JSON.stringify(entry.details)}>
                      {JSON.stringify(entry.details)}
                    </pre>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {entries.length === 0 && !isLoading && (
          <div className="flex flex-col items-center gap-3 py-12">
            <History className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No audit entries yet. Changes will appear here when you create, update, or push config.</p>
          </div>
        )}
      </div>
    </div>
  );
}
