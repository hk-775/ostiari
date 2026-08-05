import { useQuery } from "@tanstack/react-query";
import { Server, Wrench, FileText, ArrowUpRight, Plug } from "lucide-react";
import { Link } from "react-router-dom";
import { api, Gateway } from "../lib/api";

const STAT_COLORS = [
  { bg: "bg-sky-50", icon: "text-sky-600", border: "border-sky-200", glow: "0 4px 20px rgba(14, 165, 233, 0.12)", hoverGlow: "0 8px 32px rgba(14, 165, 233, 0.25), 0 0 0 1px rgba(14, 165, 233, 0.3)" },
  { bg: "bg-amber-50", icon: "text-amber-600", border: "border-amber-200", glow: "0 4px 20px rgba(245, 158, 11, 0.12)", hoverGlow: "0 8px 32px rgba(245, 158, 11, 0.25), 0 0 0 1px rgba(245, 158, 11, 0.3)" },
  { bg: "bg-rose-50", icon: "text-rose-600", border: "border-rose-200", glow: "0 4px 20px rgba(244, 63, 94, 0.12)", hoverGlow: "0 8px 32px rgba(244, 63, 94, 0.25), 0 0 0 1px rgba(244, 63, 94, 0.3)" },
  { bg: "bg-teal-50", icon: "text-teal-600", border: "border-teal-200", glow: "0 4px 20px rgba(20, 184, 166, 0.12)", hoverGlow: "0 8px 32px rgba(20, 184, 166, 0.25), 0 0 0 1px rgba(20, 184, 166, 0.3)" },
];

function StatCard({ label, value, sub, icon: Icon, href, colorIdx }: { label: string; value: string | number; sub?: string; icon: any; href: string; colorIdx: number }) {
  const c = STAT_COLORS[colorIdx % STAT_COLORS.length];
  return (
    <Link
      to={href}
      className={`group rounded-2xl border bg-white p-6 transition-all duration-300 ${c.border}`}
      style={{ boxShadow: c.glow }}
      onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.boxShadow = c.hoverGlow; }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.boxShadow = c.glow; }}
    >
      <div className="flex items-center justify-between">
        <div className={`flex h-10 w-10 items-center justify-center rounded-xl ${c.bg} border ${c.border}`}>
          <Icon className={`h-5 w-5 ${c.icon}`} />
        </div>
        <ArrowUpRight className="h-4 w-4 text-stone-300 transition group-hover:text-stone-500" />
      </div>
      <div className="mt-4">
        <p className="text-3xl font-bold tracking-tight text-stone-900">{value}</p>
        <p className="mt-1 text-sm text-stone-500">{label}</p>
        {sub && <p className="text-xs text-stone-400">{sub}</p>}
      </div>
    </Link>
  );
}

function StatusDot({ status }: { status: string }) {
  const color = status === "healthy" ? "bg-emerald-400" : status === "unreachable" ? "bg-rose-400" : "bg-stone-400";
  return <span className={`h-2.5 w-2.5 rounded-full ${color}`} />;
}

export function Dashboard() {
  const { data: gateways = [] } = useQuery({ queryKey: ["gateways"], queryFn: api.gateways.list });
  const { data: tools = [] } = useQuery({ queryKey: ["tools"], queryFn: () => api.tools.list() });
  const { data: policies = [] } = useQuery({ queryKey: ["policies"], queryFn: api.policies.list });
  const { data: mcpServers = [] } = useQuery({ queryKey: ["mcp-servers"], queryFn: () => api.mcpServers.list() });

  const healthy = gateways.filter((s) => s.status === "healthy").length;

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-stone-900">Overview</h1>
          <p className="mt-1 text-sm text-stone-500">Your agent safety infrastructure at a glance</p>
        </div>
        <Link to="/architecture" className="inline-flex items-center gap-2 rounded-xl border border-violet-200 bg-violet-50 px-4 py-2.5 text-sm font-medium text-violet-700 transition hover:bg-violet-100 hover:border-violet-300">
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}><circle cx="12" cy="12" r="3"/><path d="M12 2v4m0 12v4m10-10h-4M6 12H2m15.07-5.07l-2.83 2.83M9.76 14.24l-2.83 2.83m11.14 0l-2.83-2.83M9.76 9.76L6.93 6.93"/></svg>
          Architecture Demo
        </Link>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Agent Gateways" value={gateways.length} sub={`${healthy} healthy`} icon={Server} href="/gateways" colorIdx={0} />
        <StatCard label="Tools" value={tools.length} sub="registered" icon={Wrench} href="/tools" colorIdx={1} />
        <StatCard label="Policies" value={policies.length} sub="active" icon={FileText} href="/policies" colorIdx={2} />
        <StatCard label="MCP Servers" value={mcpServers.length} sub="connected" icon={Plug} href="/mcp-servers" colorIdx={3} />
      </div>

      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h2 className="text-sm font-semibold text-stone-700">Agent Gateway Fleet</h2>
          <Link to="/gateways" className="text-xs font-medium text-violet-600 hover:text-violet-700">View all →</Link>
        </div>
        <div className="divide-y divide-stone-100">
          {gateways.length === 0 && (
            <p className="px-6 py-10 text-center text-sm text-stone-400">
              No agent gateways registered. <Link to="/gateways" className="text-violet-600 hover:underline">Add one</Link>
            </p>
          )}
          {gateways.map((s) => (
            <div key={s.id} className="flex items-center justify-between px-6 py-4 transition hover:bg-stone-50">
              <div className="flex items-center gap-3">
                <StatusDot status={s.status} />
                <div>
                  <p className="text-sm font-medium text-stone-800">{s.name}</p>
                  <p className="text-xs text-stone-400 font-mono">{s.endpoint}</p>
                </div>
              </div>
              <div className="flex items-center gap-4">
                <span className="text-xs text-stone-400">{s.tools_count} tools</span>
                <span className={`badge ${
                  s.status === "healthy" ? "badge-allow" :
                  s.status === "unreachable" ? "badge-block" : "badge-neutral"
                }`}>{s.status}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
