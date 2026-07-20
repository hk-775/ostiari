import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Wrench, Search, FileCode, X } from "lucide-react";
import { api, Tool } from "../lib/api";

export function Tools() {
  const { data: tools = [] } = useQuery({ queryKey: ["tools"], queryFn: () => api.tools.list() });
  const [search, setSearch] = useState("");
  const [importOpen, setImportOpen] = useState(false);

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
        <div className="flex items-center gap-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-stone-400" />
            <input
              placeholder="Search tools..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-9 w-64"
            />
          </div>
          <button className="btn btn-primary flex items-center gap-2" onClick={() => setImportOpen(true)}>
            <FileCode className="h-4 w-4" />
            Import from OpenAPI
          </button>
        </div>
      </div>

      {importOpen && <ImportOpenAPIModal onClose={() => setImportOpen(false)} />}

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
                <td className="px-6 py-4 text-sm text-stone-500">{t.gateway_id.replace("-agent","").split("-").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ").replace("Devops","DevOps").replace("Crm","CRM") + " Gateway"}</td>
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

type PreviewTool = {
  name: string;
  method: string;
  endpoint: string;
  description: string;
  path_params: string[];
  query_params: string[];
};

function ImportOpenAPIModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data: gateways = [] } = useQuery({ queryKey: ["gateways"], queryFn: () => api.gateways.list() });
  const [gatewayId, setGatewayId] = useState("");
  const [source, setSource] = useState("");
  const [serverUrl, setServerUrl] = useState("");
  const [namePrefix, setNamePrefix] = useState("");
  const [replace, setReplace] = useState(false);
  const [preview, setPreview] = useState<PreviewTool[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const gw = gatewayId || (gateways[0]?.id ?? "");

  async function run(previewOnly: boolean) {
    setError("");
    setBusy(true);
    try {
      const res = await api.tools.importOpenapi(gw, {
        source: source.trim(),
        server_url: serverUrl.trim() || null,
        name_prefix: namePrefix.trim(),
        replace,
        preview: previewOnly,
      });
      if (previewOnly) {
        setPreview(res.tools);
      } else {
        queryClient.invalidateQueries({ queryKey: ["tools"] });
        onClose();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="card w-full max-w-2xl max-h-[85vh] overflow-y-auto p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <FileCode className="h-5 w-5 text-amber-600" />
            <h2 className="text-lg font-semibold text-stone-900">Import tools from OpenAPI</h2>
          </div>
          <button onClick={onClose} className="text-stone-400 hover:text-stone-600">
            <X className="h-5 w-5" />
          </button>
        </div>

        <p className="text-sm text-stone-500 mb-4">
          Generate governed tools from an OpenAPI 3.x spec. Each REST operation becomes an Ostiari
          tool that passes through the full gate chain. Provide a spec URL, or paste JSON/YAML.
        </p>

        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-stone-600 mb-1">Gateway</label>
            <select className="input w-full" value={gw} onChange={(e) => setGatewayId(e.target.value)}>
              {gateways.map((g) => (
                <option key={g.id} value={g.id}>{g.name || g.id}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-stone-600 mb-1">
              Spec URL or JSON/YAML
            </label>
            <textarea
              className="input w-full font-mono text-xs"
              rows={5}
              placeholder="https://api.example.com/openapi.json  — or paste the spec here"
              value={source}
              onChange={(e) => setSource(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-stone-600 mb-1">Base URL override (optional)</label>
              <input className="input w-full" placeholder="https://api.example.com" value={serverUrl} onChange={(e) => setServerUrl(e.target.value)} />
            </div>
            <div>
              <label className="block text-xs font-medium text-stone-600 mb-1">Name prefix (optional)</label>
              <input className="input w-full" placeholder="crm." value={namePrefix} onChange={(e) => setNamePrefix(e.target.value)} />
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm text-stone-600">
            <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} />
            Replace all existing tools on this gateway
          </label>
        </div>

        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

        {preview && (
          <div className="mt-4 rounded-lg border border-stone-200">
            <div className="border-b border-stone-200 px-4 py-2 text-xs font-medium text-stone-500 uppercase tracking-wider">
              {preview.length} tool(s) generated
            </div>
            <div className="max-h-56 overflow-y-auto divide-y divide-stone-100">
              {preview.map((t) => (
                <div key={t.name} className="px-4 py-2 text-sm">
                  <span className="badge bg-stone-100 text-stone-700 ring-1 ring-inset ring-stone-200 mr-2">{t.method}</span>
                  <span className="font-medium text-stone-900">{t.name}</span>
                  <span className="text-stone-400 font-mono text-xs ml-2">{t.endpoint}</span>
                  {(t.path_params.length > 0 || t.query_params.length > 0) && (
                    <span className="text-xs text-stone-400 ml-2">
                      {t.path_params.length > 0 && `path: ${t.path_params.join(", ")}`}
                      {t.query_params.length > 0 && ` query: ${t.query_params.join(", ")}`}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="mt-5 flex justify-end gap-3">
          <button className="btn btn-secondary" onClick={() => run(true)} disabled={busy || !source.trim() || !gw}>
            Preview
          </button>
          <button className="btn btn-primary" onClick={() => run(false)} disabled={busy || !source.trim() || !gw}>
            {busy ? "Importing…" : "Import"}
          </button>
        </div>
      </div>
    </div>
  );
}
