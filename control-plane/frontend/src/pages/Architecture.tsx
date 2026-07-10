import { useState, useEffect, useRef } from "react";
import { Play, Pause, RotateCcw, ChevronRight, Volume2, VolumeX, X, Maximize2 } from "lucide-react";

const AUDIO_PREFIX = "/audio";
const SCENARIO_AUDIO_KEYS: Record<string, string> = {
  "http": "http",
  "agent-tool": "agent",
  "mcp-embedded": "mcp-local",
  "mcp-remote": "mcp-remote",
  "frontier": "frontier",
};

// ─── NODES ────────────────────────────────────────────────────────────────────

const NODES = [
  // Client Agents (left column)
  { id: "agent-1", x: 30, y: 80, label: "CRM Agent", sub: "OpenAI · Java", group: "agent" },
  { id: "agent-2", x: 30, y: 160, label: "Ops Agent", sub: "Strands · Python", group: "agent" },
  { id: "agent-3", x: 30, y: 240, label: "DevOps Agent", sub: "LangGraph", group: "agent" },
  { id: "agent-4", x: 30, y: 320, label: "Analytics Agent", sub: "CrewAI · Go", group: "agent" },
  { id: "agent-5", x: 30, y: 400, label: "DevOps Smart Agent", sub: "LLM-Driven", group: "agent" },

  // Agent Gateway (center) — includes AxonLLM embedded
  { id: "gw-invoke", x: 240, y: 40, label: "Intent-Invoke Orchestrator", sub: "Routes to sub-modules", group: "gateway" },
  { id: "gw-auth", x: 240, y: 100, label: "Agent Auth", sub: "Per-agent grants", group: "gateway" },
  { id: "gw-quota", x: 240, y: 160, label: "Quota", sub: "Rate · Budget · Tokens", group: "gateway" },
  { id: "gw-policy", x: 240, y: 220, label: "Policy Engine", sub: "Allow / Block / Score", group: "gateway" },
  { id: "gw-axon", x: 240, y: 280, label: "AxonLLM Engine", sub: "Embedded (in-process)", group: "axon" },
  { id: "gw-router", x: 240, y: 340, label: "Tool Router", sub: "Resolve → dispatch", group: "gateway" },
  { id: "gw-trace", x: 240, y: 470, label: "Trace + Cost", sub: "Report to Control Plane", group: "gateway" },

  // Tool Providers (right side — 5 types)
  { id: "tool-http", x: 500, y: 50, label: "Email Service", sub: "HTTP endpoint", group: "http-tool" },
  { id: "tool-http2", x: 500, y: 110, label: "Database", sub: "HTTP endpoint", group: "http-tool" },

  { id: "tool-agent", x: 500, y: 180, label: "Research Agent", sub: "Agent-as-Tool", group: "agent-tool" },
  { id: "tool-agent2", x: 500, y: 240, label: "Writer Agent", sub: "Agent-as-Tool", group: "agent-tool" },

  { id: "mcp-local", x: 500, y: 310, label: "Filesystem MCP", sub: "Embedded (in-process)", group: "mcp-embedded" },
  { id: "mcp-remote", x: 500, y: 370, label: "GitHub MCP", sub: "Remote (HTTP/SSE)", group: "mcp-remote" },

  { id: "llm-claude", x: 500, y: 440, label: "Claude Sonnet", sub: "Anthropic API", group: "llm-tool" },
  { id: "llm-gpt", x: 500, y: 500, label: "GPT-4o", sub: "OpenAI API", group: "llm-tool" },
  { id: "llm-bedrock", x: 500, y: 560, label: "Bedrock", sub: "AWS API", group: "llm-tool" },

  // Control Plane (bottom center)
  { id: "cp", x: 350, y: 600, label: "Control Plane", sub: "Config · Monitor · Enforce", group: "cp" },
];

// ─── COLORS ────────────────────────────────────────────────────────────────────

const GROUP_COLORS: Record<string, { fill: string; stroke: string; text: string }> = {
  "agent": { fill: "#eff6ff", stroke: "#3b82f6", text: "#1d4ed8" },
  "gateway": { fill: "#fffbeb", stroke: "#f59e0b", text: "#92400e" },
  "axon": { fill: "#fce7f3", stroke: "#ec4899", text: "#9d174d" },
  "http-tool": { fill: "#ecfdf5", stroke: "#10b981", text: "#065f46" },
  "agent-tool": { fill: "#fef3c7", stroke: "#f59e0b", text: "#78350f" },
  "mcp-embedded": { fill: "#f5f3ff", stroke: "#8b5cf6", text: "#5b21b6" },
  "mcp-remote": { fill: "#ede9fe", stroke: "#7c3aed", text: "#4c1d95" },
  "llm-tool": { fill: "#fff7ed", stroke: "#ea580c", text: "#9a3412" },
  "cp": { fill: "#fdf4ff", stroke: "#a855f7", text: "#6b21a8" },
};

// ─── SCENARIOS ────────────────────────────────────────────────────────────────

const SCENARIOS = [
  {
    id: "http",
    name: "Direct Tool Call",
    icon: "🔧",
    color: "#10b981",
    context: "PATH 1: The agent already knows which tool to call (POST /tool/{action}). No LLM involved. This is for scripts, deterministic agents, or when the agent's own LLM already decided. Flow: Auth → Quota → Policy → Execute.",
    description: "POST /tool/{action} — Agent decides, Gateway validates + executes",
    path: ["agent-1", "gw-invoke", "gw-auth", "gw-quota", "gw-policy", "gw-router", "tool-http", "gw-trace", "cp"],
    steps: [
      "CRM Agent sends POST /tool/send_email to the Orchestrator — agent already knows what to call.",
      "Orchestrator receives request. Passes to auth pipeline.",
      "Agent Auth: checks CRM Agent has 'send_email' in its per-agent grants → allowed.",
      "Quota: rate limit (42/60 RPM) + budget ($3.20/$25) → within limits. Records request.",
      "Policy Engine: evaluates risk score (send_email → +20, total 20 ≤ 30) → allow tier",
      "Tool Router resolves 'send_email' → HTTP endpoint. No AxonLLM (agent chose the tool).",
      "Proxies to Email Service → {message_id: 'msg-123'}",
      "Trace Reporter: action, tier, score, duration, params, endpoint. Cost = $0 (no LLM).",
      "Control Plane stores in Live Traces. Agent receives result.",
    ],
  },
  {
    id: "agent-tool",
    name: "Agent-as-Tool",
    icon: "🤖",
    color: "#f59e0b",
    context: "One agent calls another agent's exposed tools. The target agent may use its own LLM internally, but from the gateway's perspective, it's just another HTTP tool endpoint. AxonLLM is not invoked unless the calling agent uses /invoke.",
    description: "Agent → Gateway → Another Agent exposing tools",
    path: ["agent-2", "gw-invoke", "gw-auth", "gw-quota", "gw-policy", "gw-router", "tool-agent", "gw-trace", "cp"],
    steps: [
      "Ops Agent sends POST /tool/research.summarize to the Orchestrator.",
      "Orchestrator receives request. Passes to auth pipeline.",
      "Agent Auth: checks Ops Agent has 'research.*' in its grants → allowed.",
      "Quota: checks rate + budget → ✓ within limits",
      "Policy Engine: 'research.summarize' has no block rules, score 0 → ✓ allow",
      "Tool Router resolves 'research.summarize' → Research Agent endpoint. AxonLLM not invoked (the target agent handles its own LLM calls internally).",
      "Proxies to Research Agent — it processes using its own LLM + tools behind its own gateway",
      "Trace Reporter records agent-to-agent call. Both gateways trace independently.",
      "Control Plane shows cross-agent communication. Cost attributed to calling agent's budget.",
    ],
  },
  {
    id: "mcp-embedded",
    name: "Embedded MCP",
    icon: "📦",
    color: "#8b5cf6",
    context: "MCP server runs inside the gateway process (zero network hop). This is how local tools like filesystem access work. AxonLLM is not involved — MCP tools are called directly in memory.",
    description: "Agent → Gateway → In-process MCP Server (no network)",
    path: ["agent-3", "gw-invoke", "gw-auth", "gw-quota", "gw-policy", "gw-router", "mcp-local", "gw-trace", "cp"],
    steps: [
      "DevOps Agent sends POST /tool/fs.read_file to the Orchestrator.",
      "Orchestrator receives request. Passes to auth pipeline.",
      "Agent Auth: checks DevOps Agent has 'fs.*' in its grants → allowed.",
      "Quota: checks limits → ✓ OK",
      "Policy Engine: 'fs.read_file' is in allow list → ✓ allow (score 0)",
      "Tool Router resolves 'fs.read_file' → Embedded MCP. Calls directly in memory via MCP Client. No AxonLLM (not an LLM operation).",
      "MCP server returns file content in ~1ms. Zero network hop — same process.",
      "Trace Reporter records: endpoint=mcp://fs, duration=1.2ms, tier=allow.",
      "Control Plane receives trace — shows [MCP] badge and embedded mode indicator.",
    ],
  },
  {
    id: "mcp-remote",
    name: "Remote MCP",
    icon: "🌐",
    color: "#7c3aed",
    context: "MCP server runs as a separate service connected via HTTP/SSE. Tools were auto-discovered on connection. Same interface as embedded MCP, just over network. AxonLLM not involved.",
    description: "Agent → Gateway → Remote MCP Server (HTTP/SSE)",
    path: ["agent-4", "gw-invoke", "gw-auth", "gw-quota", "gw-policy", "gw-router", "mcp-remote", "gw-trace", "cp"],
    steps: [
      "Analytics Agent sends POST /tool/github.create_issue to the Orchestrator.",
      "Orchestrator receives request. Passes to auth pipeline.",
      "Agent Auth: checks Analytics Agent has 'github.create_issue' → allowed.",
      "Quota: checks limits → ✓ OK",
      "Policy Engine: risk_adjust +15, total 15 ≤ 30 → ✓ allow tier",
      "Tool Router resolves 'github.create_issue' → Remote MCP at http://github-mcp:3000. No AxonLLM (direct MCP protocol call, not an LLM operation).",
      "Sends MCP JSON-RPC: tools/call('create_issue', {repo, title}) → 'Created #42'",
      "Trace Reporter records: endpoint=mcp://github, params, duration=230ms.",
      "Control Plane shows trace with session grouping. Cost = $0 (no LLM tokens).",
    ],
  },
  {
    id: "frontier",
    name: "LLM-Driven (Intent)",
    icon: "🧠",
    color: "#ec4899",
    context: "PATH 2: Agent sends intent. The Agent Gateway uses its embedded LLM Gateway to get a tool plan. Today this calls a frontier model (Claude/GPT). Tomorrow: a small specialized LLM embedded in the gateway will generate tool plans instantly (~50ms vs ~500ms). The frontier model is only needed for Round 2 (final answer synthesis). Everything else stays the same.",
    description: "POST /invoke — LLM decides tools, THEN Auth + Quota + Policy per tool",
    path: ["agent-5", "gw-invoke", "gw-auth", "gw-quota", "gw-axon", "llm-claude", "gw-axon", "gw-auth", "gw-policy", "gw-router", "mcp-remote", "gw-router", "mcp-remote", "gw-router", "mcp-remote", "gw-router", "gw-invoke", "gw-axon", "llm-claude", "gw-axon", "gw-invoke", "agent-5", "gw-invoke", "gw-trace", "cp"],
    steps: [
      "CONTEXT: DevOps Smart Agent does not call tools directly or generate tool call plans by calling LLMs itself. All this heavy lifting is offloaded to the Agent Gateway. The Orchestrator is the single entry and exit point for all agent communication. The Agent Gateway can be deployed as a standalone service or as a gateway inside Kubernetes pods.",
      "DevOps Smart Agent sends intent to the Orchestrator: 'Commit to branch, push to remote, and create a Pull Request'.",
      "Orchestrator passes request to Agent Auth. Gate 1: can this agent use /invoke? → allowed.",
      "Auth passes to Quota. Quota #1 (pre-LLM): estimates cost, checks budget → within limits.",
      "Quota passes to AxonLLM Engine. ROUND 1: generate a tool plan for this intent.",
      "AxonLLM routes to Claude to decide the tool plan. FUTURE: a small embedded LLM will do this locally in ~50ms.",
      "Claude returns 3 tools: [1] github.commit [2] github.push [3] github.create_pull_request. Plan returns to AxonLLM.",
      "AxonLLM passes plan to Agent Auth. Per-tool Gate 2: checks grants for commit, push, create PR → all allowed.",
      "Auth passes to Policy. Per-tool Quota #2 + Policy: commit OK, push OK, create PR risk+40 within threshold → allowed.",
      "Policy passes to Tool Router. Resolves all 3 tools to GitHub MCP Server.",
      "Tool Router dispatches tool 1: github.commit → GitHub MCP Server.",
      "GitHub MCP responds: 'Committed abc123 to feature-branch'. Result returns to Tool Router.",
      "Tool Router dispatches tool 2: github.push → GitHub MCP Server.",
      "GitHub MCP responds: 'Pushed to origin/feature-branch'. Result returns to Tool Router.",
      "Tool Router dispatches tool 3: github.create_pull_request → GitHub MCP Server.",
      "GitHub MCP responds: 'Created PR #47: feature-branch → main'. Result returns to Tool Router.",
      "All 3 tool results collected. Tool Router passes results back to the Orchestrator.",
      "ROUND 2 (Final Answer): Orchestrator sends tool results to AxonLLM Engine for response synthesis.",
      "AxonLLM routes to Claude. Claude synthesizes: 'Done! Committed, pushed, and PR #47 created.'",
      "Claude returns the final answer to AxonLLM.",
      "AxonLLM passes final response back to the Orchestrator.",
      "Orchestrator delivers response to DevOps Smart Agent. Agent's job is done.",
      "Control returns to Orchestrator for housekeeping.",
      "Orchestrator sends cost + trace data to Trace Reporter.",
      "Trace Reporter sends 2 LLM rounds + 3 tool executions to Control Plane. Done.",
    ],
  },
];

// ─── COMPONENT ────────────────────────────────────────────────────────────────

export function Architecture() {
  const [playing, setPlaying] = useState(false);
  const [playAllMode, setPlayAllMode] = useState(false);
  const [activeScenario, setActiveScenario] = useState(0);
  const [activeStep, setActiveStep] = useState(-1);
  const [narrativeStep, setNarrativeStep] = useState(-1);
  const [highlightedNodes, setHighlightedNodes] = useState<Set<string>>(new Set());
  const [audioEnabled, setAudioEnabled] = useState(true);
  const [fullscreen, setFullscreen] = useState(false);
  const intervalRef = useRef<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const scenario = SCENARIOS[activeScenario];

  const playAudio = (scenarioId: string, step: number): Promise<void> => {
    return new Promise((resolve) => {
      if (!audioEnabled) {
        setTimeout(resolve, 2500);
        return;
      }
      const key = SCENARIO_AUDIO_KEYS[scenarioId];
      if (!key) { setTimeout(resolve, 2500); return; }
      const src = `${AUDIO_PREFIX}/${key}-${step}.mp3`;
      if (audioRef.current) {
        audioRef.current.pause();
      }
      const audio = new Audio(src);
      audioRef.current = audio;
      audio.volume = 0.8;
      const startTime = Date.now();
      const MIN_DISPLAY_MS = 1800;
      const PAUSE_AFTER_MS = 600;
      const done = () => {
        const elapsed = Date.now() - startTime;
        const remaining = Math.max(0, MIN_DISPLAY_MS - elapsed);
        setTimeout(resolve, remaining + PAUSE_AFTER_MS);
      };
      audio.onended = done;
      audio.onerror = () => setTimeout(resolve, 2500);
      audio.play().catch(() => setTimeout(resolve, 2500));
    });
  };

  const playingRef = useRef(false);
  playingRef.current = playing;
  const playAllRef = useRef(false);
  playAllRef.current = playAllMode;
  const scenarioRef = useRef(scenario);
  scenarioRef.current = scenario;

  useEffect(() => {
    if (playing) {
      let cancelled = false;

      const runSequence = async () => {
        const sc = scenarioRef.current;
        // Clear any previous highlights at the start
        setHighlightedNodes(new Set());
        setActiveStep(-1);
        setNarrativeStep(-1);
        await new Promise(r => setTimeout(r, 300));

        // steps[] drives narration, path[] drives visuals — same length
        const totalSteps = sc.steps.length;
        for (let step = 0; step < totalSteps; step++) {
          if (cancelled || !playingRef.current) break;
          // Update visual and narrative together
          setActiveStep(step);
          setNarrativeStep(step);
          setHighlightedNodes(new Set(sc.path.slice(0, step + 1)));
          // Let React render before starting audio
          await new Promise(r => requestAnimationFrame(() => setTimeout(r, 50)));
          if (cancelled || !playingRef.current) break;
          await playAudio(sc.id, step);
          if (cancelled || !playingRef.current) break;
        }

        if (!cancelled && playingRef.current) {
          if (playAllRef.current) {
            // Play All mode: advance to next scenario
            await new Promise(r => setTimeout(r, 1500));
            if (!cancelled && playingRef.current) {
              setHighlightedNodes(new Set());
              setActiveStep(-1);
              setNarrativeStep(-1);
              setActiveScenario(prev => (prev + 1) % SCENARIOS.length);
            }
          } else {
            // Single scenario mode: stop playing, exit fullscreen
            setPlaying(false);
            setFullscreen(false);
          }
        }
      };

      runSequence();
      return () => { cancelled = true; };
    } else {
      if (audioRef.current) audioRef.current.pause();
    }
    return undefined;
  }, [playing, activeScenario]);

  const getNode = (id: string) => NODES.find(n => n.id === id);

  const getEdgePath = (fromId: string, toId: string) => {
    const from = getNode(fromId);
    const to = getNode(toId);
    if (!from || !to) return "";
    const x1 = from.x + 70, y1 = from.y + 17;
    const x2 = to.x, y2 = to.y + 17;
    const midX = (x1 + x2) / 2;
    return `M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}`;
  };

  const getReturnEdgePath = (fromId: string, toId: string) => {
    const from = getNode(fromId);
    const to = getNode(toId);
    if (!from || !to) return "";
    const x1 = from.x, y1 = from.y + 22;
    const x2 = to.x + 140, y2 = to.y + 22;
    const midX = (x1 + x2) / 2;
    return `M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}`;
  };

  const renderSvgContent = () => (
    <>
      <defs>
        <marker id="arrow-active" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto" markerUnits="userSpaceOnUse">
          <path d="M0,0 L10,5 L0,10 Z" fill={scenario.color} />
        </marker>
        <marker id="arrow-inactive" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
          <path d="M0,0 L7,3.5 L0,7 Z" fill="#d6d3d1" />
        </marker>
        <marker id="arrow-struct-in" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#f59e0b" fillOpacity="0.5" />
        </marker>
        <marker id="arrow-struct-out" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#3b82f6" fillOpacity="0.5" />
        </marker>
        <marker id="arrow-struct-tool" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#10b981" fillOpacity="0.5" />
        </marker>
        <marker id="arrow-struct-return" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#a78bfa" fillOpacity="0.5" />
        </marker>
      </defs>
      <rect x={15} y={55} width={150} height={370} rx={10} fill="#eff6ff" fillOpacity={0.5} stroke="#3b82f6" strokeWidth={1} strokeDasharray="4 3" />
      <text x={90} y={48} fontSize="9" fontWeight="700" fill="#1d4ed8" textAnchor="middle">CLIENT AGENTS</text>
      <rect x={230} y={20} width={150} height={470} rx={10} fill="#fffbeb" fillOpacity={0.5} stroke="#f59e0b" strokeWidth={2} strokeDasharray="5 3" />
      <text x={305} y={14} fontSize="9" fontWeight="700" fill="#92400e" textAnchor="middle">AGENT GATEWAY</text>
      <text x={565} y={40} fontSize="8" fontWeight="600" fill="#065f46" textAnchor="middle">HTTP TOOLS</text>
      <text x={565} y={170} fontSize="8" fontWeight="600" fill="#78350f" textAnchor="middle">AGENT-AS-TOOL</text>
      <text x={565} y={298} fontSize="8" fontWeight="600" fill="#5b21b6" textAnchor="middle">MCP SERVERS</text>
      <text x={565} y={430} fontSize="8" fontWeight="600" fill="#9a3412" textAnchor="middle">FRONTIER MODELS</text>
      <text x={565} y={443} fontSize="7" fill="#9a3412" textAnchor="middle">(via AxonLLM)</text>
      {[
        { from: "agent-1", to: "gw-invoke" }, { from: "agent-2", to: "gw-invoke" }, { from: "agent-3", to: "gw-invoke" }, { from: "agent-4", to: "gw-invoke" }, { from: "agent-5", to: "gw-invoke" },
      ].map(({ from, to }) => (<path key={`struct-${from}-${to}`} d={getEdgePath(from, to)} fill="none" stroke="#93c5fd" strokeWidth={1} opacity={0.3} strokeDasharray="3 3" markerEnd="url(#arrow-struct-in)" />))}
      {[
        { from: "gw-router", to: "tool-http" }, { from: "gw-router", to: "tool-http2" }, { from: "gw-router", to: "tool-agent" }, { from: "gw-router", to: "tool-agent2" }, { from: "gw-router", to: "mcp-local" }, { from: "gw-router", to: "mcp-remote" }, { from: "gw-axon", to: "llm-claude" }, { from: "gw-axon", to: "llm-gpt" }, { from: "gw-axon", to: "llm-bedrock" },
      ].map(({ from, to }) => (<path key={`struct-${from}-${to}`} d={getEdgePath(from, to)} fill="none" stroke="#6ee7b7" strokeWidth={1} opacity={0.3} strokeDasharray="3 3" markerEnd="url(#arrow-struct-tool)" />))}
      {[
        { from: "tool-http", to: "gw-router" }, { from: "tool-http2", to: "gw-router" }, { from: "tool-agent", to: "gw-router" }, { from: "tool-agent2", to: "gw-router" }, { from: "mcp-local", to: "gw-router" }, { from: "mcp-remote", to: "gw-router" }, { from: "llm-claude", to: "gw-axon" }, { from: "llm-gpt", to: "gw-axon" }, { from: "llm-bedrock", to: "gw-axon" },
      ].map(({ from, to }) => (<path key={`struct-ret-${from}-${to}`} d={getReturnEdgePath(from, to)} fill="none" stroke="#c4b5fd" strokeWidth={1} opacity={0.25} strokeDasharray="3 3" markerEnd="url(#arrow-struct-return)" />))}
      {[
        { from: "gw-invoke", to: "agent-1" }, { from: "gw-invoke", to: "agent-2" }, { from: "gw-invoke", to: "agent-3" }, { from: "gw-invoke", to: "agent-4" }, { from: "gw-invoke", to: "agent-5" },
      ].map(({ from, to }) => (<path key={`struct-ret-${from}-${to}`} d={getReturnEdgePath(from, to)} fill="none" stroke="#c4b5fd" strokeWidth={1} opacity={0.25} strokeDasharray="3 3" markerEnd="url(#arrow-struct-return)" />))}
      {scenario.path.map((nodeId, i) => {
        if (i === 0) return null;
        const prevId = scenario.path[i - 1];
        const isCurrent = activeStep === i;
        const isPast = activeStep > i;
        const isActive = isCurrent || isPast;
        return (<path key={`${i}-${prevId}-${nodeId}`} d={getEdgePath(prevId, nodeId)} fill="none" stroke={isActive ? scenario.color : "#d6d3d1"} strokeWidth={isCurrent ? 3.5 : isPast ? 2 : 1.5} opacity={isCurrent ? 1 : isPast ? 0.3 : 0.1} strokeDasharray={isActive ? "" : "4 3"} markerEnd={isActive ? "url(#arrow-active)" : "url(#arrow-inactive)"} style={{ transition: "all 0.4s" }} />);
      })}
      {NODES.map(node => {
        const colors = GROUP_COLORS[node.group] || GROUP_COLORS["gateway"];
        const isActive = highlightedNodes.has(node.id);
        return (
          <g key={node.id}>
            <rect x={node.x} y={node.y} width={140} height={35} rx={8} fill={isActive ? colors.fill : "#fafaf9"} stroke={isActive ? colors.stroke : "#e7e5e4"} strokeWidth={isActive ? 2.5 : 1} style={{ transition: "all 0.3s", filter: isActive ? `drop-shadow(0 0 10px ${colors.stroke}50)` : "" }} />
            <text x={node.x + 70} y={node.y + 15} fontSize="9.5" fontWeight="600" fill={isActive ? colors.text : "#44403c"} textAnchor="middle">{node.label}</text>
            <text x={node.x + 70} y={node.y + 27} fontSize="7.5" fill="#78716c" textAnchor="middle">{node.sub}</text>
          </g>
        );
      })}
      {activeStep >= 0 && activeStep < scenario.path.length && (() => {
        const node = getNode(scenario.path[activeStep]);
        if (!node) return null;
        return (
          <circle cx={node.x + 70} cy={node.y + 17} r={5} fill={scenario.color} opacity={0.8}>
            <animate attributeName="r" values="4;9;4" dur="0.8s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.8;0.3;0.8" dur="0.8s" repeatCount="indefinite" />
          </circle>
        );
      })()}
    </>
  );

  const renderSteps = () => scenario.steps.map((step, i) => {
    const isActive = narrativeStep === i;
    const isPast = narrativeStep > i;
    return (
      <div
        key={i}
        ref={isActive ? (el) => { el?.scrollIntoView({ behavior: "smooth", block: "nearest" }); } : undefined}
        className={`rounded-lg border px-3 py-2 text-xs transition-all duration-300 ${
          isActive ? "border-stone-300 bg-white shadow-sm" : isPast ? "border-stone-100 bg-stone-50 opacity-60" : "border-stone-100 bg-stone-50/50 opacity-40"
        }`}
        style={isActive ? { borderLeftColor: scenario.color, borderLeftWidth: 3 } : {}}
      >
        <div className="flex items-start gap-2">
          <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[9px] font-bold ${
            isActive ? "text-white" : isPast ? "bg-stone-200 text-stone-500" : "bg-stone-100 text-stone-400"
          }`} style={isActive ? { backgroundColor: scenario.color } : {}}>
            {i + 1}
          </span>
          <p className={`leading-relaxed ${isActive ? "text-stone-800 font-medium" : "text-stone-500"}`}>{step}</p>
        </div>
      </div>
    );
  });

  return (
    <div className="space-y-5">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-stone-900">Architecture</h1>
        <p className="mt-1 text-sm text-stone-500">Everything is a tool — watch how agents access HTTP services, other agents, MCP servers, and frontier models through the Agent Gateway</p>
      </div>

      {/* Controls */}
      <div className="card p-4 flex items-center gap-3 flex-wrap">
        <button
          onClick={() => { if (playing) { setPlaying(false); setPlayAllMode(false); setFullscreen(false); } else { setPlayAllMode(true); setActiveStep(-1); setNarrativeStep(-1); setHighlightedNodes(new Set()); setFullscreen(true); setPlaying(true); } }}
          className={`inline-flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-medium shadow-sm transition active:scale-95 ${playing && playAllMode ? "bg-rose-600 text-white hover:bg-rose-700" : "bg-emerald-600 text-white hover:bg-emerald-700"}`}
        >
          {playing && playAllMode ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {playing && playAllMode ? "Stop" : "Play All"}
        </button>
        <button onClick={() => { setPlaying(false); setPlayAllMode(false); setFullscreen(false); setActiveStep(-1); setNarrativeStep(-1); setHighlightedNodes(new Set()); if (audioRef.current) audioRef.current.pause(); }} className="btn-secondary text-sm">
          <RotateCcw className="h-4 w-4" /> Reset
        </button>
        <button
          onClick={() => setAudioEnabled(!audioEnabled)}
          className={`inline-flex items-center gap-1.5 rounded-xl px-3 py-2.5 text-sm font-medium transition border ${audioEnabled ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-stone-200 text-stone-400"}`}
          title={audioEnabled ? "Narration on" : "Narration off"}
        >
          {audioEnabled ? <Volume2 className="h-4 w-4" /> : <VolumeX className="h-4 w-4" />}
        </button>
        <div className="h-6 w-px bg-stone-200" />
        {SCENARIOS.map((s, i) => (
          <button
            key={s.id}
            onClick={() => { setPlaying(false); setPlayAllMode(false); setTimeout(() => { setActiveScenario(i); setActiveStep(-1); setNarrativeStep(-1); setHighlightedNodes(new Set()); setFullscreen(true); setTimeout(() => setPlaying(true), 200); }, 100); }}
            className={`rounded-xl px-3 py-2 text-xs font-medium transition border ${
              activeScenario === i
                ? `bg-white shadow-sm`
                : "border-stone-200 text-stone-500 hover:bg-stone-50"
            }`}
            style={activeScenario === i ? { borderColor: s.color, color: s.color } : {}}
          >
            <span className="mr-1">{s.icon}</span>{s.name}
          </button>
        ))}
      </div>

      {fullscreen && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-6">
          <button
            onClick={() => { setPlaying(false); setPlayAllMode(false); setFullscreen(false); if (audioRef.current) audioRef.current.pause(); }}
            className="absolute top-4 right-4 z-50 rounded-full bg-white/10 hover:bg-white/20 p-2 text-white transition"
            title="Close"
          >
            <X className="h-6 w-6" />
          </button>
          <div className="w-full h-full grid grid-cols-3 gap-6 max-w-[1600px]">
            <div className="col-span-2 bg-white rounded-2xl p-6 overflow-hidden flex flex-col shadow-2xl">
              {/* Scenario title in fullscreen */}
              <div className="mb-3 flex items-center gap-3">
                <span className="text-2xl">{scenario.icon}</span>
                <h2 className="text-lg font-bold" style={{ color: scenario.color }}>{scenario.name}</h2>
                <span className="text-sm text-stone-500 ml-auto">Step {Math.max(0, narrativeStep)} / {scenario.steps.length - 1}</span>
              </div>
              <svg viewBox="0 0 680 650" className="w-full flex-1">
                {renderSvgContent()}
              </svg>
            </div>
            <div className="bg-white rounded-2xl p-6 flex flex-col overflow-hidden shadow-2xl">
              <div className="shrink-0 mb-3">
                <p className="text-xs text-stone-600 font-medium">{scenario.description}</p>
              </div>
              <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
                {renderSteps()}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-3 gap-4" style={{ height: "calc(100vh - 220px)" }}>
        {/* SVG Diagram (2/3 width) */}
        <div className="col-span-2 card p-4 overflow-hidden flex flex-col">
          {/* Unified concept banner */}
          <div className="mb-3 rounded-lg bg-stone-50 border border-stone-100 px-4 py-2 text-center">
            <p className="text-xs text-stone-500">
              <span className="font-semibold text-stone-700">Unified model:</span> Agent calls <code className="bg-white px-1 rounded text-[10px] font-mono border">POST /tool/&#123;action&#125;</code> — the gateway resolves it to the right provider
            </p>
          </div>

          <svg viewBox="0 0 680 650" className="w-full flex-1">
            {renderSvgContent()}
          </svg>
        </div>

        {/* Step narration (1/3 width) */}
        <div className="card p-5 flex flex-col overflow-hidden">
          <div className="shrink-0 mb-3">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-lg">{scenario.icon}</span>
              <h3 className="text-sm font-bold" style={{ color: scenario.color }}>{scenario.name}</h3>
            </div>
            <p className="text-xs text-stone-600 font-medium">{scenario.description}</p>
            <div className="mt-2 rounded-lg bg-stone-50 border border-stone-100 p-2.5">
              <p className="text-[10px] font-semibold text-stone-400 uppercase mb-1">Context</p>
              <p className="text-xs text-stone-600 leading-relaxed">{scenario.context}</p>
            </div>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2" id="steps-panel">
            {renderSteps()}
          </div>
        </div>
      </div>
    </div>
  );
}
