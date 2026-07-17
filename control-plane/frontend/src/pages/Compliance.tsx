import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileCheck, CheckCircle2, AlertTriangle, XCircle, RefreshCw } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";

interface Requirement {
  id: string;
  title: string;
  status: "met" | "partial" | "unmet";
  detail: string;
  evidence_refs: string[];
}

interface Report {
  framework: string;
  posture: "green" | "yellow" | "red";
  score_pct: number;
  period_days: number;
  summary: { met: number; partial: number; unmet: number };
  evidence: {
    audit_count: number; trace_count: number; blocked_count: number;
    intervene_count: number; policy_count: number;
  };
  requirements: Requirement[];
}

async function fetchReport(framework: string, period: number): Promise<Report> {
  const res = await fetch(`${API_BASE}/api/compliance/report?framework=${framework}&period_days=${period}`);
  return res.json();
}

const STATUS = {
  met: { icon: CheckCircle2, cls: "text-emerald-600", badge: "bg-emerald-50 text-emerald-700", label: "Met" },
  partial: { icon: AlertTriangle, cls: "text-amber-600", badge: "bg-amber-50 text-amber-700", label: "Partial" },
  unmet: { icon: XCircle, cls: "text-rose-600", badge: "bg-rose-50 text-rose-700", label: "Unmet" },
} as const;

const POSTURE = {
  green: "text-emerald-600", yellow: "text-amber-600", red: "text-rose-600",
} as const;

export function Compliance() {
  const [framework, setFramework] = useState("eu-ai-act");
  const [period, setPeriod] = useState(90);
  const { data, isFetching, refetch } = useQuery({
    queryKey: ["compliance", framework, period],
    queryFn: () => fetchReport(framework, period),
  });

  const ev = data?.evidence;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-stone-900">
            <FileCheck className="h-6 w-6 text-sky-500" /> Compliance
          </h1>
          <p className="mt-1 text-sm text-stone-500">
            Auto-generated evidence from your governance data. Auditor-ready.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select value={framework} onChange={(e) => setFramework(e.target.value)} className="input">
            <option value="eu-ai-act">EU AI Act</option>
          </select>
          <select value={period} onChange={(e) => setPeriod(Number(e.target.value))} className="input">
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
            <option value={365}>1 year</option>
          </select>
          <button onClick={() => refetch()} className="btn-secondary" disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* Posture + evidence summary */}
      <div className="grid grid-cols-5 gap-4">
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Posture</p>
          <p className={`mt-2 text-3xl font-bold uppercase ${data ? POSTURE[data.posture] : "text-stone-400"}`}>
            {data?.posture ?? "—"}
          </p>
          <p className="mt-1 text-xs text-stone-500">{data?.score_pct ?? 0}% requirements met</p>
        </div>
        <div className="card p-5"><p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Policies</p><p className="mt-2 text-3xl font-bold text-stone-900">{ev?.policy_count ?? 0}</p></div>
        <div className="card p-5"><p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Audit records</p><p className="mt-2 text-3xl font-bold text-stone-900">{ev?.audit_count ?? 0}</p></div>
        <div className="card p-5"><p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Blocked</p><p className="mt-2 text-3xl font-bold text-rose-600">{ev?.blocked_count ?? 0}</p></div>
        <div className="card p-5"><p className="text-xs font-semibold uppercase tracking-wider text-stone-500">Human oversight</p><p className="mt-2 text-3xl font-bold text-amber-600">{ev?.intervene_count ?? 0}</p></div>
      </div>

      {/* Requirements */}
      <div className="space-y-3">
        {(data?.requirements ?? []).map((r) => {
          const s = STATUS[r.status];
          const Icon = s.icon;
          return (
            <div key={r.id} className="card flex items-start gap-4 p-5">
              <Icon className={`mt-0.5 h-6 w-6 shrink-0 ${s.cls}`} />
              <div className="flex-1">
                <div className="flex items-center gap-3">
                  <h3 className="text-sm font-semibold text-stone-800">{r.title}</h3>
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${s.badge}`}>{s.label}</span>
                </div>
                <p className="mt-1 text-sm text-stone-600">{r.detail}</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {r.evidence_refs.map((ref) => (
                    <span key={ref} className="rounded bg-stone-100 px-1.5 py-0.5 font-mono text-[11px] text-stone-500">{ref}</span>
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
