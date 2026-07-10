import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Wrench, Search } from "lucide-react";
import { api, Tool } from "../lib/api";

export function Tools() {
  const { data: tools = [] } = useQuery({ queryKey: ["tools"], queryFn: () => api.tools.list() });
  const [search, setSearch] = useState("");

  const filtered = tools.filter((t) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      t.name.toLowerCase().includes(q) ||
      t.description.toLowerCase().includes(q) ||
      t.endpoint.toLowerCase().includes(q) ||
      t.gateway_id.toLowerCase().includes(q)
    );
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Tools</h1>
          <p className="mt-1 text-sm text-stone-500">All tools registered across gateways</p>
        </div>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-stone-400" />
          <input
            placeholder="Search tools..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input pl-9 w-64"
          />
        </div>
      </div>

      <div className="card overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-stone-200 text-left text-xs font-medium text-stone-500 uppercase tracking-wider">
              <th className="px-6 py-3.5">Tool</th>
              <th className="px-6 py-3.5">Endpoint</th>
              <th className="px-6 py-3.5">Method</th>
              <th className="px-6 py-3.5">Gateway</th>
              <th className="px-6 py-3.5">Timeout</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-100">
            {filtered.map((t) => (
              <tr key={t.id} className="transition hover:bg-stone-50">
                <td className="px-6 py-4">
                  <div className="flex items-center gap-2">
                    <Wrench className="h-4 w-4 text-amber-600" />
                    <div>
                      <p className="text-sm font-medium text-stone-900">{t.name}</p>
                      <p className="text-xs text-stone-500">{t.description}</p>
                    </div>
                  </div>
                </td>
                <td className="px-6 py-4 text-sm text-stone-500 font-mono">{t.endpoint}</td>
                <td className="px-6 py-4">
                  <span className="badge bg-stone-100 text-stone-700 ring-1 ring-inset ring-stone-200">{t.method}</span>
                </td>
                <td className="px-6 py-4 text-sm text-stone-500">{t.gateway_id}</td>
                <td className="px-6 py-4 text-sm text-stone-500">{t.timeout_seconds}s</td>
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length === 0 && tools.length > 0 && (
          <div className="flex flex-col items-center gap-3 py-12">
            <Search className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No tools matching "{search}"</p>
          </div>
        )}
        {tools.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-12">
            <Wrench className="h-8 w-8 text-stone-300" />
            <p className="text-sm text-stone-500">No tools registered. Add tools to gateways first.</p>
          </div>
        )}
      </div>
    </div>
  );
}
