import { useEffect, useState } from "react";
import { Network, Save, Check, ShieldAlert } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";
const GATEWAY_BASE = `${API_BASE}/api/proxy/gateway/crm-agent`;

type EdgeState = "allow" | "deny" | "default";

interface CrossAgentConfig {
  enabled: boolean;
  default_allow: boolean;
  min_trust: number;
  max_chain_depth: number | null;
  trust_scores: Record<string, number>;
  edges: Record<string, { allow: string[]; deny: string[] }>;
}

const EMPTY: CrossAgentConfig = {
  enabled: false, default_allow: true, min_trust: 0, max_chain_depth: null,
  trust_scores: {}, edges: {},
};

// Demo agent fleet shown when the gateway hasn't been configured yet.
const DEMO_AGENTS = ["research", "coder", "db", "payments", "ops"];

async function fetchConfig(): Promise<CrossAgentConfig> {
  try {
    const res = await fetch(`${GATEWAY_BASE}/config/cross-agent`);
    if (!res.ok) throw new Error();
    const data = await res.json();
    return { ...EMPTY, ...data };
  } catch {
    return { ...EMPTY };
  }
}

function edgeState(cfg: CrossAgentConfig, caller: string, callee: string): EdgeState {
  const e = cfg.edges[caller];
  if (e?.deny?.includes(callee) || e?.deny?.includes("*")) return "deny";
  if (e?.allow?.includes(callee) || e?.allow?.includes("*")) return "allow";
  return "default";
}

export function ProtocolGovernance() {
  const [cfg, setCfg] = useState<CrossAgentConfig>(EMPTY);
  const [agents, setAgents] = useState<string[]>(DEMO_AGENTS);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetchConfig().then((c) => {
      setCfg(c);
      const seen = new Set<string>(DEMO_AGENTS);
      Object.keys(c.edges).forEach((k) => seen.add(k));
      Object.values(c.edges).forEach((e) => [...e.allow, ...e.deny].forEach((a) => a !== "*" && seen.add(a)));
      Object.keys(c.trust_scores).forEach((k) => seen.add(k));
      setAgents([...seen].sort());
    });
  }, []);

  // Cycle a cell: default -> allow -> deny -> default
  const cycle = (caller: string, callee: string) => {
    setCfg((prev) => {
      const edges = structuredClone(prev.edges);
      const e = edges[caller] ?? { allow: [], deny: [] };
      e.allow = e.allow.filter((x) => x !== callee);
      e.deny = e.deny.filter((x) => x !== callee);
      const cur = edgeState(prev, caller, callee);
      if (cur === "default") e.allow.push(callee);
      else if (cur === "allow") e.deny.push(callee);
      // deny -> default: leave removed
      edges[caller] = e;
      return { ...prev, edges };
    });
  };

  const setTrust = (agent: string, score: number) =>
    setCfg((prev) => ({ ...prev, trust_scores: { ...prev.trust_scores, [agent]: score } }));

  const save = async () => {
    try {
      await fetch(`${GATEWAY_BASE}/config/cross-agent`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
    } catch { /* gateway may be offline in demo */ }
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const cellClass = (s: EdgeState) =>
    s === "allow" ? "bg-emerald-100 text-emerald-700 hover:bg-emerald-200"
    : s === "deny" ? "bg-rose-100 text-rose-700 hover:bg-rose-200"
    : "bg-stone-50 text-stone-300 hover:bg-stone-100";
  const cellLabel = (s: EdgeState) => (s === "allow" ? "✓" : s === "deny" ? "✕" : "·");

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
            <Network className="h-6 w-6 text-violet-500" /> Protocol Governance
          </h1>
          <p className="mt-1 text-sm text-stone-500">
            Control which agents may delegate to which (A2A). Rows delegate → columns.
          </p>
        </div>
        <button onClick={save} className="btn-sky">
          {saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />} {saved ? "Saved" : "Save & Push"}
        </button>
      </div>

      {/* Global settings */}
      <div className="card grid grid-cols-4 gap-4 p-5">
        <label className="flex items-center gap-2 text-sm text-stone-700">
          <input type="checkbox" checked={cfg.enabled}
            onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })} />
          Enforcement enabled
        </label>
        <label className="flex items-center gap-2 text-sm text-stone-700">
          <input type="checkbox" checked={cfg.default_allow}
            onChange={(e) => setCfg({ ...cfg, default_allow: e.target.checked })} />
          Default allow (unlisted edges)
        </label>
        <label className="text-sm text-stone-700">
          Min trust
          <input type="number" min={0} max={100} value={cfg.min_trust}
            onChange={(e) => setCfg({ ...cfg, min_trust: Number(e.target.value) })}
            className="input mt-1" />
        </label>
        <label className="text-sm text-stone-700">
          Max chain depth
          <input type="number" min={0} value={cfg.max_chain_depth ?? ""}
            placeholder="unlimited"
            onChange={(e) => setCfg({ ...cfg, max_chain_depth: e.target.value ? Number(e.target.value) : null })}
            className="input mt-1" />
        </label>
      </div>

      {/* Delegation matrix */}
      <div className="card overflow-x-auto p-5">
        <h2 className="mb-3 text-sm font-semibold text-stone-800">Delegation matrix</h2>
        <table className="border-collapse">
          <thead>
            <tr>
              <th className="p-2 text-left text-xs font-semibold text-stone-500">caller ＼ callee</th>
              {agents.map((c) => (
                <th key={c} className="p-2 text-xs font-medium text-stone-600">
                  <div className="flex flex-col items-center gap-1">
                    <span>{c}</span>
                    <input type="number" min={0} max={100}
                      value={cfg.trust_scores[c] ?? 50}
                      onChange={(e) => setTrust(c, Number(e.target.value))}
                      title="trust score"
                      className="w-12 rounded border border-stone-200 px-1 py-0.5 text-center text-[10px]" />
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {agents.map((caller) => (
              <tr key={caller}>
                <td className="p-2 text-sm font-medium text-stone-700">{caller}</td>
                {agents.map((callee) => {
                  const s = edgeState(cfg, caller, callee);
                  return (
                    <td key={callee} className="p-1 text-center">
                      <button
                        onClick={() => cycle(caller, callee)}
                        disabled={caller === callee}
                        title={caller === callee ? "self" : `${caller} → ${callee}: ${s}`}
                        className={`h-8 w-8 rounded font-bold transition ${
                          caller === callee ? "cursor-default bg-stone-100 text-stone-200" : cellClass(s)
                        }`}
                      >{caller === callee ? "" : cellLabel(s)}</button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-3 flex gap-4 text-xs text-stone-500">
          <span><span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700">✓</span> allow</span>
          <span><span className="rounded bg-rose-100 px-1.5 py-0.5 text-rose-700">✕</span> deny</span>
          <span><span className="rounded bg-stone-50 px-1.5 py-0.5 text-stone-400">·</span> default</span>
          <span className="ml-auto flex items-center gap-1"><ShieldAlert className="h-3.5 w-3.5" /> click a cell to cycle</span>
        </div>
      </div>
    </div>
  );
}
