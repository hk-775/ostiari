import { Link, Outlet, useLocation } from "react-router-dom";
import { Shield, Server, Wrench, FileText, Activity, Plug, History, DollarSign, Radio, FlaskConical, Brain, ShieldCheck, Beaker, Bot, Network } from "lucide-react";

const NAV_SECTIONS = [
  {
    label: "Observe",
    labelColor: "text-emerald-500",
    borderColor: "border-l-emerald-400",
    items: [
      { path: "/dashboard", label: "Dashboard", icon: Activity, color: "text-emerald-600", activeBg: "bg-emerald-50" },
      { path: "/traces", label: "Live Traces", icon: Radio, color: "text-emerald-600", activeBg: "bg-emerald-50" },
      { path: "/costs", label: "Costs", icon: DollarSign, color: "text-orange-600", activeBg: "bg-orange-50" },
      { path: "/audit", label: "Audit Log", icon: History, color: "text-stone-600", activeBg: "bg-stone-100" },
    ],
  },
  {
    label: "Control",
    labelColor: "text-rose-500",
    borderColor: "border-l-rose-400",
    items: [
      { path: "/models", label: "Models (per agent)", icon: Brain, color: "text-indigo-600", activeBg: "bg-indigo-50" },
      { path: "/policies", label: "Policies (per tool)", icon: FileText, color: "text-rose-600", activeBg: "bg-rose-50" },
      { path: "/quotas", label: "Quotas (per gateway)", icon: ShieldCheck, color: "text-amber-600", activeBg: "bg-amber-50" },
    ],
  },
  {
    label: "Configure",
    labelColor: "text-sky-500",
    borderColor: "border-l-sky-400",
    items: [
      { path: "/gateways", label: "Agent Gateways", icon: Server, color: "text-sky-600", activeBg: "bg-sky-50" },
      { path: "/agents", label: "Agents", icon: Bot, color: "text-lime-600", activeBg: "bg-lime-50" },
      { path: "/tools", label: "Tools", icon: Wrench, color: "text-amber-600", activeBg: "bg-amber-50" },
      { path: "/mcp-servers", label: "MCP Servers", icon: Plug, color: "text-teal-600", activeBg: "bg-teal-50" },
    ],
  },
  {
    label: "Test",
    labelColor: "text-fuchsia-500",
    borderColor: "border-l-fuchsia-400",
    items: [
      { path: "/sandbox", label: "Sandbox", icon: Beaker, color: "text-fuchsia-600", activeBg: "bg-fuchsia-50" },
      { path: "/experiments", label: "A/B Tests", icon: FlaskConical, color: "text-pink-600", activeBg: "bg-pink-50" },
      { path: "/architecture", label: "Architecture", icon: Network, color: "text-violet-600", activeBg: "bg-violet-50" },
    ],
  },
];

const ROUTE_BORDER_COLORS: Record<string, string> = {
  "/": "rgba(139, 92, 246, 0.3)",           // violet
  "/gateways": "rgba(14, 165, 233, 0.3)",   // sky
  "/tools": "rgba(245, 158, 11, 0.3)",      // amber
  "/mcp-servers": "rgba(20, 184, 166, 0.3)",// teal
  "/policies": "rgba(244, 63, 94, 0.3)",    // rose
  "/traces": "rgba(16, 185, 129, 0.3)",     // emerald
  "/costs": "rgba(234, 88, 12, 0.3)",       // orange
  "/experiments": "rgba(236, 72, 153, 0.3)",// pink
  "/models": "rgba(99, 102, 241, 0.3)",     // indigo
  "/efficiency": "rgba(6, 182, 212, 0.3)",  // cyan
  "/quotas": "rgba(217, 119, 6, 0.3)",      // amber-600
  "/audit": "rgba(120, 113, 108, 0.3)",     // stone
  "/sandbox": "rgba(192, 38, 211, 0.3)",   // fuchsia
  "/agents": "rgba(132, 204, 22, 0.3)",    // lime
  "/architecture": "rgba(139, 92, 246, 0.3)", // violet
};

export function Layout() {
  const location = useLocation();
  const borderColor = ROUTE_BORDER_COLORS[location.pathname] || "rgba(214, 211, 209, 0.8)";

  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 flex w-60 flex-col border-r border-stone-200 bg-white">
        {/* Logo */}
        <div className="flex h-16 items-center px-5 border-b border-stone-100">
          <img src="/logo.svg" alt="Ostiari" className="h-8" />
        </div>

        {/* Nav */}
        <nav className="flex-1 overflow-y-auto px-3 py-4">
          {NAV_SECTIONS.map((section) => (
            <div key={section.label} className={`mb-4 border-l-2 ${section.borderColor} pl-2 ml-1`}>
              <p className={`mb-1.5 px-2 text-[10px] font-bold uppercase tracking-widest ${section.labelColor}`}>
                {section.label}
              </p>
              <div className="space-y-0.5">
                {section.items.map((item) => {
                  const active = location.pathname === item.path;
                  return (
                    <Link
                      key={item.path}
                      to={item.path}
                      className={`flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-sm transition ${
                        active
                          ? `${item.activeBg} ${item.color} font-medium`
                          : "text-stone-500 hover:bg-stone-100 hover:text-stone-800"
                      }`}
                    >
                      <item.icon className={`h-4 w-4 ${active ? item.color : ""}`} />
                      {item.label}
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        {/* Footer */}
        <div className="border-t border-stone-100 px-6 py-4">
          <p className="text-[11px] text-stone-400">v0.1.0</p>
        </div>
      </aside>

      {/* Main content */}
      <main className="ml-60 flex-1 min-h-screen" style={{ "--card-border": borderColor } as React.CSSProperties}>
        <div className="mx-auto max-w-6xl px-8 py-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
