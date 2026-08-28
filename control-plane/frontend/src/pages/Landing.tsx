import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Brain,
  CheckCircle2,
  CircleAlert,
  Coins,
  EyeOff,
  FileCheck,
  Gauge,
  Lock,
  Network,
  Radio,
  Shield,
  TrendingUp,
  Wallet,
  Wrench,
} from "lucide-react";
import { api } from "../lib/api";
import { DEMO_MODES, DEPLOYMENT_VIEWS, GATE_CHAIN } from "../lib/architecture";
import { IS_PUBLIC_SITE, publicAsset } from "../lib/publicSite";

const FEATURES = [
  { icon: Shield, title: "Policy Engine", desc: "Per-tool allow/block/risk-score. Every call validated before execution.", color: "text-rose-600", bg: "bg-rose-50" },
  { icon: EyeOff, title: "Shadow Mode", desc: "Try before you enforce — evaluate and report what would block, with zero side effects.", color: "text-amber-600", bg: "bg-amber-50" },
  { icon: Network, title: "Protocol Governance", desc: "Govern agent-to-agent delegation: edges, trust scores, chain-depth limits.", color: "text-violet-600", bg: "bg-violet-50" },
  { icon: Wrench, title: "Universal Tool Proxy", desc: "HTTP tools, MCP servers, Agent-as-Tool, A2A — all through one gateway.", color: "text-emerald-600", bg: "bg-emerald-50" },
  { icon: Wallet, title: "Payments (x402)", desc: "Pay-per-tool-call with per-agent USDC wallets, limits, and auto-pause.", color: "text-emerald-600", bg: "bg-emerald-50" },
  { icon: Coins, title: "Token Broker", desc: "Bulk-buy/resell margin, per-provider token pools, invoice reconciliation.", color: "text-amber-600", bg: "bg-amber-50" },
  { icon: TrendingUp, title: "ROI / Savings", desc: "Damage-prevented estimate from blocked actions, priced by your own assumptions.", color: "text-emerald-600", bg: "bg-emerald-50" },
  { icon: Gauge, title: "Metering & Cost Control", desc: "Governed-call metering with tiers; per-agent budgets and spend alerts.", color: "text-orange-600", bg: "bg-orange-50" },
  { icon: FileCheck, title: "Compliance Reports", desc: "Auto-generated EU AI Act evidence from your traces, audit logs, and policies.", color: "text-sky-600", bg: "bg-sky-50" },
  { icon: Brain, title: "Model Access & Routing", desc: "Per-agent model restrictions, smart routing, A/B testing via AxonLLM.", color: "text-indigo-600", bg: "bg-indigo-50" },
  { icon: Lock, title: "Per-Agent Auth", desc: "Least privilege: each agent only gets the tools, models, and providers it's granted.", color: "text-violet-600", bg: "bg-violet-50" },
  { icon: Radio, title: "Real-Time Observability", desc: "Live traces, cost dashboards, audit logs. See every call as it happens.", color: "text-cyan-600", bg: "bg-cyan-50" },
];

export function Landing() {
  const { data: gateways } = useQuery({
    queryKey: ["landing-gateways"],
    queryFn: api.gateways.list,
    refetchInterval: 5000,
    retry: false,
    enabled: !IS_PUBLIC_SITE,
  });

  return (
    <div
      className="min-h-screen bg-gradient-to-br from-stone-50 via-white to-violet-50/30"
      data-testid="canonical-landing"
    >
      {/* Hero */}
      <div className="max-w-5xl mx-auto px-8 pt-10 pb-5">
        <img src={publicAsset("logo.svg")} alt="Ostiari" className="w-full" />

        <div className="flex items-center justify-between gap-8 mt-5">
          <p className="text-2xl font-semibold text-stone-900 leading-snug">AI agents are autonomous.<br /><span className="text-violet-700">Your risk shouldn't be.</span></p>
          <div className="flex flex-col gap-2.5 shrink-0">
            <Link to="/dashboard" className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg hover:shadow-xl transition">
              Open Control Plane <ArrowRight className="h-4 w-4" />
            </Link>
            <Link
              to="/architecture"
              data-testid="architecture-demo-link"
              className="inline-flex items-center gap-2 rounded-xl border border-stone-200 bg-white px-5 py-2.5 text-sm font-semibold text-stone-700 hover:bg-stone-50 transition"
            >
              Watch Architecture Demo
            </Link>
          </div>
        </div>

        <p className="mt-5 text-lg text-stone-600 max-w-2xl leading-relaxed">
          The runtime governance layer for AI agents. Every tool call validated, every agent-to-agent delegation governed, every dollar metered — without changing agent code.
        </p>
      </div>

      {/* Current runtime topology */}
      <div className="max-w-5xl mx-auto px-8 py-4">
        <div className="rounded-lg border border-stone-200 bg-white p-5 shadow-sm">
          <div className="grid items-stretch gap-3 lg:grid-cols-[1fr_auto_1.25fr_auto_1fr]">
            <div className="rounded-lg border border-sky-200 bg-sky-50 p-4">
              <p className="text-xs font-bold text-sky-800">Agents and SDKs</p>
              <p className="mt-1 text-[11px] leading-5 text-sky-700">Direct tools, LLM intent, and A2A delegation</p>
            </div>
            <ArrowRight className="hidden h-5 w-5 self-center text-stone-300 lg:block" />
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
              <p className="text-xs font-bold text-amber-800">Ostiari gateway</p>
              <p className="mt-1 text-[11px] leading-5 text-amber-700">Identity, authorization, quota, risk, approval, payment, execution, and trace</p>
            </div>
            <ArrowRight className="hidden h-5 w-5 self-center text-stone-300 lg:block" />
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-4">
              <p className="text-xs font-bold text-emerald-800">Governed targets</p>
              <p className="mt-1 text-[11px] leading-5 text-emerald-700">HTTP tools, stdio MCP, peer agents, and LLM providers</p>
            </div>
          </div>
          <div className="mx-auto mt-3 max-w-xl rounded-lg border border-violet-200 bg-violet-50 p-4 text-center">
            <p className="text-xs font-bold text-violet-800">Control plane</p>
            <p className="mt-1 text-[11px] leading-5 text-violet-700">Configuration, approvals, fleet health, traces, cost, payments, and audit evidence</p>
          </div>
        </div>
      </div>

      {/* How it works */}
      <div className="max-w-5xl mx-auto px-8 py-4 border-t border-stone-100/50">
        <h2 className="text-lg font-bold text-stone-900 mb-1">The gate chain</h2>
        <p className="text-xs text-stone-500 mb-6">Enforced calls follow this pipeline. Shadow mode runs the ordered pre-execution checks and returns a synthetic result before approval, payment, or execution.</p>
        <div className="flex items-center gap-2 overflow-x-auto pb-4">
          {GATE_CHAIN.map((item, i) => (
            <div key={item.label} className="flex items-center gap-2">
              <div className="w-44 flex-shrink-0 rounded-lg border border-stone-200 bg-white p-4 shadow-sm">
                <div className="flex items-center gap-2 mb-1">
                  <span className="flex h-6 w-6 items-center justify-center rounded-full bg-violet-100 text-violet-700 text-xs font-bold">{i + 1}</span>
                  <p className="text-xs font-semibold text-stone-800">{item.label}</p>
                </div>
                <p className="text-[10px] leading-4 text-stone-500">{item.detail}</p>
              </div>
              {i < GATE_CHAIN.length - 1 && <ArrowRight className="h-4 w-4 text-stone-300 flex-shrink-0" />}
            </div>
          ))}
        </div>
      </div>

      {/* Features grid */}
      <div className="max-w-5xl mx-auto px-8 py-4 border-t border-stone-100/50">
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
      <div className="max-w-5xl mx-auto px-8 py-4 border-t border-stone-100/50">
        <h2 className="text-lg font-bold text-stone-900 mb-6">Two paths through the gateway</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-xl border border-emerald-200 bg-emerald-50/50 p-6">
            <h3 className="text-sm font-bold text-emerald-800 mb-2">PATH 1: Direct Tool Call</h3>
            <code className="text-xs text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded">POST /tool/&#123;action&#125;</code>
            <p className="text-xs text-stone-600 mt-3 leading-relaxed">
              Agent already knows which tool to call. The gateway resolves it, then runs authorization, quota, risk, optional human approval, payment, execution, and trace reporting.
            </p>
          </div>
          <div className="rounded-xl border border-pink-200 bg-pink-50/50 p-6">
            <h3 className="text-sm font-bold text-pink-800 mb-2">PATH 2: LLM-Driven (Intent)</h3>
            <code className="text-xs text-pink-700 bg-pink-100 px-2 py-0.5 rounded">POST /invoke</code>
            <p className="text-xs text-stone-600 mt-3 leading-relaxed">
              Agent sends intent. AxonLLM selects a provider and model route; generated HTTP and MCP tool calls are authorized, quota-checked, and risk-scored before execution.
            </p>
          </div>
        </div>
      </div>

      {/* Deployment */}
      <div className="max-w-5xl mx-auto px-8 py-4 border-t border-stone-100/50">
        <h2 className="text-lg font-bold text-stone-900 mb-1">Supported deployment topologies</h2>
        <p className="mb-4 text-xs text-stone-500">The launcher selects a complete topology and validates its prerequisites before deployment.</p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {DEPLOYMENT_VIEWS.filter((view) => view.id !== "source-demo").map((view) => (
            <div key={view.id} className="rounded-lg border border-stone-200 bg-white p-4">
              <p className="text-xs font-semibold text-stone-800">{view.name}</p>
              <p className="mt-1 text-[10px] leading-4 text-stone-500">{view.summary}</p>
              <div className="mt-3 flex flex-wrap gap-1">
                {view.profiles.map((profile) => (
                  <code key={profile} className="rounded bg-stone-100 px-1.5 py-1 text-[9px] text-stone-600">{profile}</code>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Demo modes */}
      <div className="max-w-5xl mx-auto px-8 py-4 border-t border-stone-100/50">
        <h2 className="text-lg font-bold text-stone-900 mb-1">Project demos</h2>
        <p className="mb-4 text-xs text-stone-500">Use the launcher for the packaged evaluation path or the source demo for the broadest protocol walkthrough.</p>
        <div className="grid gap-4 sm:grid-cols-2">
          {DEMO_MODES.map((mode) => (
            <div key={mode.name} className="rounded-lg border border-stone-200 bg-white p-5 shadow-sm">
              <p className="text-sm font-bold text-stone-800">{mode.name}</p>
              <code className="mt-2 block overflow-x-auto rounded-md bg-stone-900 px-3 py-2 text-[10px] text-stone-100">{mode.command}</code>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {mode.facts.map((fact) => (
                  <span key={fact} className="flex items-center gap-2 text-[10px] text-stone-600">
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
                    {fact}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Control Plane Philosophy */}
      <div className="max-w-5xl mx-auto px-8 py-4 border-t border-stone-100/50">
        <h2 className="text-lg font-bold text-stone-900 mb-2">How the Control Plane governs</h2>
        <p className="text-xs text-stone-500 mb-5">The Control Plane is the single source of truth. Gateways are stateless consumers that pull config on startup and receive updates via heartbeat. Config changes propagate in under 1 second to healthy gateways — no restart required.</p>

        <div className="grid gap-4 sm:grid-cols-2 mb-5">
          {/* What CP manages */}
          <div className="rounded-xl border border-emerald-200 bg-emerald-50/30 p-5">
            <p className="text-xs font-bold text-emerald-800 uppercase tracking-wide mb-3">What the Control Plane manages</p>
            <div className="space-y-2">
              {[
                { label: "Policies", desc: "Define and push allow/block/score rules" },
                { label: "Quotas & budgets", desc: "Per-gateway and per-agent rate/spend limits, alerts" },
                { label: "Protocol (A2A)", desc: "Agent-to-agent delegation edges, trust, chain depth" },
                { label: "Payments (x402)", desc: "Per-agent USDC wallets, limits, pay-per-call" },
                { label: "Token broker", desc: "Bulk token pools, margin, invoice reconciliation" },
                { label: "Model access", desc: "Who uses which models and providers" },
                { label: "MCP & A2A registry", desc: "Connected servers/agents, restored on restart" },
                { label: "Traces, metering & ROI", desc: "Collect, aggregate, price — in real time" },
                { label: "Compliance & audit", desc: "EU AI Act evidence, admin audit trail" },
                { label: "Health", desc: "Heartbeat-based monitoring of gateway fleet" },
              ].map(item => (
                <div key={item.label} className="flex items-start gap-2">
                  <span className="text-emerald-500 mt-0.5 text-xs">●</span>
                  <div><span className="text-xs font-semibold text-stone-800">{item.label}</span><span className="text-[10px] text-stone-500"> — {item.desc}</span></div>
                </div>
              ))}
            </div>
          </div>

          {/* What CP does NOT manage */}
          <div className="rounded-xl border border-stone-200 bg-stone-50/50 p-5">
            <p className="text-xs font-bold text-stone-600 uppercase tracking-wide mb-3">What it does NOT manage</p>
            <div className="space-y-2">
              {[
                { label: "Starting/stopping gateways", desc: "That's your orchestrator (K8s, ECS, systemd)" },
                { label: "Deploying agents", desc: "CP controls what agents can do, not where they run" },
                { label: "Infrastructure", desc: "Terraform, CDK, CloudFormation own provisioning" },
                { label: "Network routing", desc: "Service mesh, DNS, load balancers" },
                { label: "Secret rotation", desc: "Secrets Manager / Vault handle rotation" },
              ].map(item => (
                <div key={item.label} className="flex items-start gap-2">
                  <span className="text-stone-400 mt-0.5 text-xs">○</span>
                  <div><span className="text-xs font-semibold text-stone-700">{item.label}</span><span className="text-[10px] text-stone-500"> — {item.desc}</span></div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Config propagation */}
        <div className="rounded-xl border border-indigo-100 bg-indigo-50/30 p-5">
          <p className="text-xs font-bold text-indigo-800 uppercase tracking-wide mb-3">Config propagation</p>
          <div className="flex items-center gap-2 flex-wrap text-[10px]">
            {[
              "Operator changes config in UI",
              "Control Plane saves to DB",
              "Push to gateway (or queue if offline)",
              "Gateway applies immediately (hot-reload)",
              "Next call uses new rules",
              "Trace shows result in < 1s",
            ].map((step, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className="rounded-full bg-white border border-indigo-200 px-2.5 py-1 text-indigo-700 font-medium shadow-sm">{step}</span>
                {i < 5 && <ArrowRight className="h-3 w-3 text-indigo-300 shrink-0" />}
              </div>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap gap-4 text-[10px] text-stone-500">
            <span className="inline-flex items-center gap-1.5"><CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />Healthy gateway: <strong className="text-stone-700">&lt; 1s</strong> propagation</span>
            <span className="inline-flex items-center gap-1.5"><CircleAlert className="h-3.5 w-3.5 text-rose-600" />Offline gateway: <strong className="text-stone-700">queued</strong>, syncs on reconnect (≤ 30s)</span>
          </div>
        </div>

        {/* Lifecycle example */}
        <div className="rounded-xl border border-stone-200 bg-stone-50/50 p-5 mt-4">
          <p className="text-xs font-bold text-stone-700 uppercase tracking-wide mb-3">Lifecycle in action</p>
          <pre className="text-[10px] text-stone-600 leading-relaxed overflow-x-auto whitespace-pre">{`CP starts
  ← Gateway 1 registers (POST /api/gateways/gateway-a/register)
  ← Gateway 2 registers (POST /api/gateways/gateway-b/register)
  ← Gateway 3 registers (POST /api/gateways/gateway-c/register)

Running:
  ← Gateway 1 heartbeats every 30s
  ← Gateway 2 heartbeats every 30s
  ← Gateway 3 heartbeats every 30s

Operator pushes policy change to Gateway 2:
  → CP checks: Gateway 2 healthy? Yes → forward immediately ✓

Gateway 3 goes down:
  → CP marks unhealthy after 90s (red dot)
  → Operator pushes quota change to Gateway 3
  → CP queues it

Gateway 3 restarts:
  → Registers → gets full config bundle (including the queued quota change)
  → Starts heartbeating → green dot again`}</pre>
        </div>

        {/* Fleet status — live from the control plane */}
        <div className="rounded-xl border border-emerald-200 bg-emerald-50/30 p-5 mt-4">
          <p className="text-xs font-bold text-emerald-800 uppercase tracking-wide mb-3">
            Fleet status {gateways ? `(live · ${gateways.length} gateway${gateways.length === 1 ? "" : "s"})` : "(connecting…)"}
          </p>
          {gateways && gateways.length > 0 ? (
            <div className="grid grid-cols-3 gap-x-4 gap-y-1 text-[10px]">
              <span className="font-semibold text-stone-600">Gateway</span>
              <span className="font-semibold text-stone-600">Tools</span>
              <span className="font-semibold text-stone-600">Status</span>
              {gateways.map((g) => {
                const healthy = g.status === "healthy" || g.status === "registered";
                return (
                  <div key={g.id} className="contents">
                    <span className="text-stone-700">{g.id}</span>
                    <span className="text-stone-500">{g.tools_count}</span>
                    <span className={`inline-flex items-center gap-1 ${healthy ? "text-emerald-600" : "text-rose-500"}`}>
                      {healthy ? <CheckCircle2 className="h-3 w-3" /> : <CircleAlert className="h-3 w-3" />} {g.status}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-[10px] text-stone-500">
              No gateways registered yet. Start the packaged launcher demo or the source demo shown above.
            </p>
          )}
        </div>
      </div>

      {/* CTA */}
      <div className="max-w-5xl mx-auto px-8 py-6 border-t border-stone-100/50 text-center">
        <p className="text-sm text-stone-500 mb-4">Ready to explore?</p>
        <Link to="/dashboard" className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-8 py-3.5 text-sm font-semibold text-white shadow-lg hover:shadow-xl transition">
          Enter Control Plane <ArrowRight className="h-4 w-4" />
        </Link>
      </div>
    </div>
  );
}
