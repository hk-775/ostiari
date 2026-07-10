import { Link } from "react-router-dom";
import { Shield, ArrowRight, Brain, Wrench, Radio, DollarSign, Lock, Zap, Users2, Network } from "lucide-react";

const FEATURES = [
  { icon: Shield, title: "Policy Engine", desc: "Per-tool allow/block/risk-score. Every call validated before execution.", color: "text-rose-600", bg: "bg-rose-50" },
  { icon: Brain, title: "Model Access & Routing", desc: "Per-agent model restrictions, smart routing, ensemble, A/B testing.", color: "text-indigo-600", bg: "bg-indigo-50" },
  { icon: DollarSign, title: "Cost Control", desc: "Per-agent budgets, dual quota (pre-LLM + per-tool), spend alerts at 80/90/100%.", color: "text-amber-600", bg: "bg-amber-50" },
  { icon: Lock, title: "Per-Agent Auth", desc: "Least privilege: each agent only gets the tools, models, and providers it's granted.", color: "text-violet-600", bg: "bg-violet-50" },
  { icon: Wrench, title: "Universal Tool Proxy", desc: "HTTP tools, MCP servers, Agent-as-Tool, A2A — all through one gateway.", color: "text-emerald-600", bg: "bg-emerald-50" },
  { icon: Radio, title: "Real-Time Observability", desc: "Live traces, cost dashboards, audit logs. See every call as it happens.", color: "text-cyan-600", bg: "bg-cyan-50" },
  { icon: Zap, title: "AxonLLM Engine", desc: "Embedded LLM routing: smart model selection, fallback chains, PII redaction.", color: "text-pink-600", bg: "bg-pink-50" },
  { icon: Users2, title: "A2A Protocol", desc: "Agent-to-Agent communication. Discover, send tasks, multi-turn collaboration.", color: "text-sky-600", bg: "bg-sky-50" },
  { icon: Network, title: "Multi-Gateway Fleet", desc: "Central Control Plane manages N gateways. Push config, collect traces.", color: "text-orange-600", bg: "bg-orange-50" },
];

const ARCHITECTURE_FLOW = [
  { step: "1", label: "Agent sends intent", desc: "POST /tool/* or POST /invoke" },
  { step: "2", label: "Orchestrator receives", desc: "Single entry/exit point" },
  { step: "3", label: "Auth → Quota → Policy", desc: "Three gates must pass" },
  { step: "4", label: "Route & Execute", desc: "HTTP, MCP, A2A, or LLM" },
  { step: "5", label: "Report costs", desc: "Fire-and-forget to Control Plane" },
];

export function Landing() {
  return (
    <div className="min-h-screen bg-gradient-to-br from-stone-50 via-white to-violet-50/30">
      {/* Hero */}
      <div className="max-w-5xl mx-auto px-8 pt-16 pb-12">
        <div className="flex items-center gap-3 mb-6">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500 to-indigo-600 shadow-lg">
            <Shield className="h-6 w-6 text-white" />
          </div>
          <div>
            <h1 className="text-3xl font-extrabold text-stone-900 tracking-tight">Ostiari</h1>
            <p className="text-sm text-stone-500">Agent Gateway + Control Plane</p>
          </div>
        </div>

        <p className="text-xl text-stone-700 max-w-2xl leading-relaxed">
          The runtime safety layer for AI agents. Every tool call validated, every model access controlled, every dollar tracked — without changing agent code.
        </p>

        <div className="flex gap-3 mt-8">
          <Link to="/" className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-6 py-3 text-sm font-semibold text-white shadow-lg hover:shadow-xl transition">
            Open Control Plane <ArrowRight className="h-4 w-4" />
          </Link>
          <Link to="/architecture" className="inline-flex items-center gap-2 rounded-xl border border-stone-200 bg-white px-6 py-3 text-sm font-semibold text-stone-700 hover:bg-stone-50 transition">
            Watch Architecture Demo
          </Link>
        </div>
      </div>

      {/* How it works */}
      <div className="max-w-5xl mx-auto px-8 py-12">
        <h2 className="text-lg font-bold text-stone-900 mb-6">How it works</h2>
        <div className="flex items-center gap-2 overflow-x-auto pb-4">
          {ARCHITECTURE_FLOW.map((item, i) => (
            <div key={item.step} className="flex items-center gap-2">
              <div className="flex-shrink-0 rounded-xl border border-stone-200 bg-white p-4 w-44 shadow-sm">
                <div className="flex items-center gap-2 mb-1">
                  <span className="flex h-6 w-6 items-center justify-center rounded-full bg-violet-100 text-violet-700 text-xs font-bold">{item.step}</span>
                  <p className="text-xs font-semibold text-stone-800">{item.label}</p>
                </div>
                <p className="text-[10px] text-stone-500">{item.desc}</p>
              </div>
              {i < ARCHITECTURE_FLOW.length - 1 && <ArrowRight className="h-4 w-4 text-stone-300 flex-shrink-0" />}
            </div>
          ))}
        </div>
      </div>

      {/* Features grid */}
      <div className="max-w-5xl mx-auto px-8 py-12">
        <h2 className="text-lg font-bold text-stone-900 mb-6">Capabilities</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map(f => (
            <div key={f.title} className="rounded-xl border border-stone-100 bg-white p-5 shadow-sm hover:shadow-md transition">
              <div className={`inline-flex h-9 w-9 items-center justify-center rounded-xl ${f.bg} mb-3`}>
                <f.icon className={`h-4.5 w-4.5 ${f.color}`} />
              </div>
              <h3 className="text-sm font-semibold text-stone-800 mb-1">{f.title}</h3>
              <p className="text-xs text-stone-500 leading-relaxed">{f.desc}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Two paths */}
      <div className="max-w-5xl mx-auto px-8 py-12">
        <h2 className="text-lg font-bold text-stone-900 mb-6">Two paths through the gateway</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-xl border border-emerald-200 bg-emerald-50/50 p-6">
            <h3 className="text-sm font-bold text-emerald-800 mb-2">PATH 1: Direct Tool Call</h3>
            <code className="text-xs text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded">POST /tool/&#123;action&#125;</code>
            <p className="text-xs text-stone-600 mt-3 leading-relaxed">
              Agent already knows which tool to call. Gateway validates (auth → quota → policy) and proxies. No LLM involved inside the gateway.
            </p>
          </div>
          <div className="rounded-xl border border-pink-200 bg-pink-50/50 p-6">
            <h3 className="text-sm font-bold text-pink-800 mb-2">PATH 2: LLM-Driven (Intent)</h3>
            <code className="text-xs text-pink-700 bg-pink-100 px-2 py-0.5 rounded">POST /invoke</code>
            <p className="text-xs text-stone-600 mt-3 leading-relaxed">
              Agent sends intent. Gateway generates tool plan via AxonLLM, validates each tool, executes, synthesizes response. Agent doesn't call LLMs directly.
            </p>
          </div>
        </div>
      </div>

      {/* Deployment */}
      <div className="max-w-5xl mx-auto px-8 py-12">
        <h2 className="text-lg font-bold text-stone-900 mb-4">Deployment</h2>
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="rounded-xl border border-stone-200 bg-white p-4">
            <p className="text-xs font-semibold text-stone-800">Standalone Service</p>
            <p className="text-[10px] text-stone-500 mt-1">One gateway serves multiple agents. Shared infrastructure.</p>
          </div>
          <div className="rounded-xl border border-stone-200 bg-white p-4">
            <p className="text-xs font-semibold text-stone-800">Kubernetes Sidecar</p>
            <p className="text-[10px] text-stone-500 mt-1">One gateway per pod. Strong isolation, network policy enforcement.</p>
          </div>
          <div className="rounded-xl border border-stone-200 bg-white p-4">
            <p className="text-xs font-semibold text-stone-800">NAT Gateway</p>
            <p className="text-[10px] text-stone-500 mt-1">Network-level proxy. Zero agent code changes. Enterprise-wide governance.</p>
          </div>
        </div>
      </div>

      {/* CTA */}
      <div className="max-w-5xl mx-auto px-8 py-16 text-center">
        <p className="text-sm text-stone-500 mb-4">Ready to explore?</p>
        <Link to="/" className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-8 py-3.5 text-sm font-semibold text-white shadow-lg hover:shadow-xl transition">
          Enter Control Plane <ArrowRight className="h-4 w-4" />
        </Link>
      </div>
    </div>
  );
}
