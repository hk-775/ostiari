import { useEffect, useRef, useState } from "react";
import { Radio, Pause, Play, Trash2, ChevronDown, ChevronRight } from "lucide-react";

const WS_URL = (import.meta.env.VITE_API_URL || "http://localhost:8400").replace("http", "ws");

interface TraceEvent {
  gateway_id: string;
  action: string;
  tier: string;
  score: number;
  duration_ms: number;
  agent_id: string;
  framework: string;
  is_mcp: boolean;
  blocked_reason: string | null;
  endpoint: string;
  session_id: string;
  plan: string;
  step: string;
  params: Record<string, unknown> | null;
  model: string;
  timestamp: number;
}

const TIER_STYLES: Record<string, string> = {
  allow: "border-emerald-300 bg-emerald-50/50",
  block: "border-rose-300 bg-rose-50/50",
  intervene: "border-amber-300 bg-amber-50/50",
  error: "border-orange-300 bg-orange-50/50",
};

const TIER_BADGES: Record<string, string> = {
  allow: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200",
  block: "bg-rose-50 text-rose-700 ring-1 ring-inset ring-rose-200",
  intervene: "bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-200",
  error: "bg-orange-50 text-orange-700 ring-1 ring-inset ring-orange-200",
};

export function LiveTraces() {
  const [traces, setTraces] = useState<TraceEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [paused, setPaused] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const ws = new WebSocket(`${WS_URL}/ws/traces`);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);

    ws.onmessage = (event) => {
      if (paused) return;
      const trace: TraceEvent = JSON.parse(event.data);
      setTraces((prev) => [...prev.slice(-199), trace]);
    };

    return () => ws.close();
  }, [paused]);

  useEffect(() => {
    if (scrollRef.current && !paused) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [traces, paused]);

  const stats = {
    total: traces.length,
    allowed: traces.filter(t => t.tier === "allow").length,
    blocked: traces.filter(t => t.tier === "block").length,
    avgDuration: traces.length > 0
      ? (traces.reduce((s, t) => s + t.duration_ms, 0) / traces.length).toFixed(1)
      : "0",
  };

  const renderSpanDetail = (trace: TraceEvent) => (
    <div className="px-12 pb-3">
      <div className="rounded-xl border border-stone-200 bg-stone-50 p-4 space-y-3">
        <div className="space-y-1">
          <p className="text-xs font-medium text-stone-500 mb-2">Trace Span</p>
          <div className="relative">
            <div className="flex items-center gap-2">
              <span className="w-28 text-xs text-stone-500 text-right">validate</span>
              <div className="flex-1 h-5 bg-stone-100 rounded relative overflow-hidden">
                <div className={`absolute left-0 top-0 h-full rounded ${trace.tier === "block" ? "bg-rose-400" : "bg-emerald-400"}`}
                  style={{ width: `${Math.min(Math.max((3 / Math.max(trace.duration_ms, 3)) * 100, 5), 30)}%` }} />
                <span className="absolute inset-0 flex items-center px-2 text-xs text-stone-700">
                  guard.validate() → {trace.tier} (score: {trace.score})
                </span>
              </div>
              <span className="w-14 text-xs text-stone-500 text-right">~3ms</span>
            </div>
            {trace.tier !== "block" && (
              <div className="flex items-center gap-2 mt-1">
                <span className="w-28 text-xs text-stone-500 text-right">{trace.is_mcp ? "mcp.call" : "http.proxy"}</span>
                <div className="flex-1 h-5 bg-stone-100 rounded relative overflow-hidden">
                  <div className="absolute left-0 top-0 h-full rounded bg-amber-400"
                    style={{ width: `${Math.min(Math.max(((trace.duration_ms - 3) / Math.max(trace.duration_ms, 3)) * 100, 10), 95)}%` }} />
                  <span className="absolute inset-0 flex items-center px-2 text-xs text-stone-700">
                    {trace.is_mcp ? `tools/call("${trace.action.split('.')[1] || trace.action}")` : `POST → ${trace.endpoint || "endpoint"}`}
                  </span>
                </div>
                <span className="w-14 text-xs text-stone-500 text-right">{(trace.duration_ms - 3).toFixed(1)}ms</span>
              </div>
            )}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-x-8 gap-y-1 text-xs border-t border-stone-200 pt-3">
          <div className="flex justify-between"><span className="text-stone-500">Action</span><span className="text-stone-900 font-mono">{trace.action}</span></div>
          <div className="flex justify-between"><span className="text-stone-500">Total Duration</span><span className="text-stone-900">{trace.duration_ms.toFixed(2)}ms</span></div>
          <div className="flex justify-between"><span className="text-stone-500">Gateway</span><span className="text-stone-900">{trace.gateway_id}</span></div>
          <div className="flex justify-between"><span className="text-stone-500">Agent</span><span className="text-stone-900">{trace.agent_id}</span></div>
          <div className="flex justify-between"><span className="text-stone-500">Framework</span><span className="text-stone-900">{trace.framework}</span></div>
          <div className="flex justify-between"><span className="text-stone-500">Type</span><span className="text-stone-900">{trace.is_mcp ? "MCP Tool" : "HTTP Tool"}</span></div>
          {trace.endpoint && <div className="col-span-2 flex justify-between"><span className="text-stone-500">Endpoint</span><span className="text-amber-700 font-mono text-xs">{trace.endpoint}</span></div>}
          {trace.model && <div className="col-span-2 flex justify-between"><span className="text-stone-500">Model</span><span className="text-indigo-700 font-medium text-xs">{trace.model}</span></div>}
          <div className="flex justify-between"><span className="text-stone-500">Risk Score</span><span className={`${trace.score > 70 ? "text-rose-600" : trace.score > 30 ? "text-amber-600" : "text-emerald-600"}`}>{trace.score}/100</span></div>
          <div className="flex justify-between"><span className="text-stone-500">Decision</span><span className={`${trace.tier === "block" ? "text-rose-600" : trace.tier === "allow" ? "text-emerald-600" : "text-amber-600"}`}>{trace.tier.toUpperCase()}</span></div>
          {trace.blocked_reason && <div className="col-span-2 flex justify-between"><span className="text-stone-500">Blocked Reason</span><span className="text-rose-600">{trace.blocked_reason}</span></div>}
          {trace.step && <div className="col-span-2 flex justify-between"><span className="text-stone-500">Step</span><span className="text-stone-900">{trace.step}</span></div>}
          {trace.session_id && <div className="col-span-2 flex justify-between"><span className="text-stone-500">Session</span><span className="text-stone-900 font-mono">{trace.session_id}</span></div>}
          <div className="col-span-2 flex justify-between"><span className="text-stone-500">Timestamp</span><span className="text-stone-900">{new Date(trace.timestamp * 1000).toISOString()}</span></div>
        </div>
        {trace.params && Object.keys(trace.params).length > 0 && (
          <details className="border-t border-stone-200 pt-2">
            <summary className="text-xs text-stone-500 cursor-pointer hover:text-stone-700">
              Parameters ({Object.keys(trace.params).length} fields)
            </summary>
            <pre className="mt-2 text-xs text-stone-700 bg-stone-100 rounded-xl p-2 overflow-x-auto max-h-40 overflow-y-auto">
              {JSON.stringify(trace.params, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </div>
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight text-stone-900">Live Traces</h1>
          <div className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-500 animate-pulse" : "bg-rose-400"}`} />
            <span className="text-xs text-stone-500">{connected ? "Connected" : "Disconnected"}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-stone-500">{stats.total} events | {stats.allowed} allowed | {stats.blocked} blocked | avg {stats.avgDuration}ms</span>
          <button
            onClick={() => setPaused(!paused)}
            className="btn-emerald text-xs"
          >
            {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
            {paused ? "Resume" : "Pause"}
          </button>
          <button
            onClick={() => setTraces([])}
            className="inline-flex items-center gap-1.5 rounded-xl bg-red-50 border border-red-200 px-4 py-2.5 text-xs font-medium text-red-700 shadow-sm transition hover:bg-red-100 hover:border-red-300 active:scale-[0.98]"
          >
            <Trash2 className="h-3.5 w-3.5" /> Clear
          </button>
        </div>
      </div>

      <div
        ref={scrollRef}
        className="h-[calc(100vh-200px)] overflow-y-auto card"
      >
        {traces.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-16">
            <Radio className="h-8 w-8 text-stone-300 animate-pulse" />
            <p className="text-sm text-stone-500">Waiting for traces...</p>
            <p className="text-xs text-stone-400">Tool calls from gateways will appear here in real-time</p>
          </div>
        )}

        <div className="divide-y divide-stone-100">
          {(() => {
            // Group traces by session_id (ungrouped traces get rendered individually)
            const groups: { session_id: string; plan: string; traces: { trace: TraceEvent; idx: number }[] }[] = [];
            const seen_sessions = new Map<string, number>();

            traces.forEach((trace, i) => {
              if (trace.session_id) {
                if (seen_sessions.has(trace.session_id)) {
                  groups[seen_sessions.get(trace.session_id)!].traces.push({ trace, idx: i });
                } else {
                  seen_sessions.set(trace.session_id, groups.length);
                  groups.push({ session_id: trace.session_id, plan: trace.plan, traces: [{ trace, idx: i }] });
                }
              } else {
                groups.push({ session_id: "", plan: "", traces: [{ trace, idx: i }] });
              }
            });

            return groups.map((group, gi) => {
              if (group.session_id && group.traces.length > 1) {
                // Grouped session view
                const isGroupExpanded = expanded.has(group.traces[0].idx);
                const toggleGroup = () => setExpanded(prev => {
                  const next = new Set(prev);
                  const key = group.traces[0].idx;
                  if (next.has(key)) next.delete(key); else next.add(key);
                  return next;
                });
                const totalDuration = group.traces.reduce((s, t) => s + t.trace.duration_ms, 0);
                const allAllowed = group.traces.every(t => t.trace.tier === "allow");
                const blockedCount = group.traces.filter(t => t.trace.tier === "block").length;
                return (
                  <div key={`g-${gi}`} className={`border-l-2 ${allAllowed ? "border-emerald-300 bg-emerald-50/50" : "border-amber-300 bg-amber-50/50"}`}>
                    <div className="flex items-center gap-4 px-4 py-2.5 cursor-pointer hover:bg-stone-50" onClick={toggleGroup}>
                      <button className="text-stone-400">
                        {isGroupExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                      </button>
                      <span className="w-16 text-xs text-stone-500 tabular-nums">
                        {new Date(group.traces[0].trace.timestamp * 1000).toLocaleTimeString()}
                      </span>
                      <span className="badge bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-200">
                        session
                      </span>
                      <span className="flex-1 text-sm text-stone-900">
                        {group.plan || `Session ${group.session_id}`}
                      </span>
                      <span className="text-xs text-stone-500">{group.traces.length} steps</span>
                      <span className="text-xs text-stone-400">{totalDuration.toFixed(0)}ms total</span>
                      <span className="text-xs text-stone-500">{group.traces[0].trace.agent_id}</span>
                      {blockedCount > 0 && <span className="text-xs text-rose-600">{blockedCount} blocked</span>}
                    </div>
                    {isGroupExpanded && (
                      <div className="pl-10 pb-2 space-y-0.5">
                        {group.traces.map(({ trace, idx }) => {
                          const stepExpanded = expanded.has(idx + 10000);
                          const toggleStep = (e: React.MouseEvent) => {
                            e.stopPropagation();
                            setExpanded(prev => {
                              const next = new Set(prev);
                              const key = idx + 10000;
                              if (next.has(key)) next.delete(key); else next.add(key);
                              return next;
                            });
                          };
                          return (
                            <div key={idx}>
                              <div className={`flex items-center gap-3 px-3 py-1.5 rounded-xl border-l-2 ${TIER_STYLES[trace.tier] || ""} cursor-pointer hover:bg-stone-50`} onClick={toggleStep}>
                                <button className="text-stone-400">
                                  {stepExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                                </button>
                                <span className={`w-14 badge text-center ${TIER_BADGES[trace.tier] || "text-stone-500"}`}>
                                  {trace.tier}
                                </span>
                                <span className="w-8 text-right text-xs text-stone-500 tabular-nums">{trace.score}</span>
                                <span className="flex-1 font-mono text-xs text-stone-900">
                                  {trace.is_mcp && <span className="mr-1 text-violet-600">[MCP]</span>}
                                  {trace.action}
                                </span>
                                <span className="text-xs text-stone-500">{trace.duration_ms.toFixed(1)}ms</span>
                                {trace.endpoint && <span className="text-xs text-stone-400 font-mono truncate max-w-40">{trace.endpoint}</span>}
                                {trace.model && <span className="text-xs text-indigo-600 font-mono">{trace.model}</span>}
                                {trace.step && <span className="text-xs text-stone-400 truncate max-w-48">{trace.step}</span>}
                              </div>
                              {stepExpanded && renderSpanDetail(trace)}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              }

              // Single trace (no session)
              const trace = group.traces[0].trace;
              const i = group.traces[0].idx;
              const isExpanded = expanded.has(i);
              const toggle = () => setExpanded(prev => {
                const next = new Set(prev);
                if (next.has(i)) next.delete(i); else next.add(i);
                return next;
              });
            return (
              <div key={`s-${gi}`} className={`border-l-2 ${TIER_STYLES[trace.tier] || ""}`}>
                <div className="flex items-center gap-4 px-4 py-2 cursor-pointer hover:bg-stone-50" onClick={toggle}>
                  <button className="text-stone-400">
                    {isExpanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                  </button>
                  <span className="w-16 text-xs text-stone-500 tabular-nums">
                    {new Date(trace.timestamp * 1000).toLocaleTimeString()}
                  </span>
                  <span className={`w-16 badge text-center ${TIER_BADGES[trace.tier] || "text-stone-500"}`}>
                    {trace.tier}
                  </span>
                  <span className="w-10 text-right text-xs text-stone-500 tabular-nums">{trace.score}</span>
                  <span className="flex-1 font-mono text-sm text-stone-900">
                    {trace.is_mcp && <span className="mr-1.5 text-violet-600">[MCP]</span>}
                    {trace.action}
                  </span>
                  <span className="w-20 text-right text-xs text-stone-500">{trace.duration_ms.toFixed(1)}ms</span>
                  <span className="w-24 truncate text-xs text-stone-400" title={trace.gateway_id}>{trace.gateway_id}</span>
                  <span className="w-20 truncate text-xs text-stone-400" title={trace.agent_id}>{trace.agent_id}</span>
                  {trace.blocked_reason && (
                    <span className="text-xs text-rose-600 truncate max-w-32" title={trace.blocked_reason}>
                      {trace.blocked_reason}
                    </span>
                  )}
                </div>
                {isExpanded && renderSpanDetail(trace)}
              </div>
            );
            });
          })()}
        </div>
      </div>
    </div>
  );
}
