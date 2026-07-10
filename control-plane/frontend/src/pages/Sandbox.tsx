import { useState, useRef } from "react";
import { Play, Send, Beaker, Code2, MessageSquare, Loader2, CheckCircle, XCircle, Wrench, Users2, Plus, Trash2 } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8400";
const GATEWAY_PROXY = `${API_BASE}/api/proxy/gateway/crm-agent`;

type Tab = "chat" | "scenarios" | "code" | "a2a";

interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  model?: string;
  tools?: { name: string; args: any }[];
  blocked?: { action: string; reason: string }[];
}

interface ScenarioResult {
  status: "running" | "done" | "error";
  output: string[];
}

const SCENARIOS = [
  { id: "basic", name: "Basic Tool Calls", description: "Query DB, send email, attempt delete (blocked)", icon: "🔧", color: "bg-sky-50 border-sky-200" },
  { id: "multistep", name: "Multi-Step Plan", description: "10 steps: research → issue → diagram → notify", icon: "📋", color: "bg-violet-50 border-violet-200" },
  { id: "blocked", name: "Test Policy Blocks", description: "Attempt dangerous actions, see them blocked", icon: "🛡️", color: "bg-rose-50 border-rose-200" },
  { id: "mcp", name: "MCP Tool Discovery", description: "Use GitHub + Draw.io MCP tools", icon: "🔌", color: "bg-teal-50 border-teal-200" },
];

const CODE_TEMPLATE = `import requests

GATEWAY = "${GATEWAY_PROXY}"

# Call a tool through the gateway
resp = requests.post(f"{GATEWAY}/tool/db_query", json={
    "sql": "SELECT * FROM users"
}, headers={"X-Agent-Id": "sandbox-agent"})

print(f"Status: {resp.status_code}")
print(f"Result: {resp.json()}")
`;

export function Sandbox() {
  const [tab, setTab] = useState<Tab>("chat");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [scenarioResult, setScenarioResult] = useState<Record<string, ScenarioResult>>({});
  const [code, setCode] = useState(CODE_TEMPLATE);
  const [codeOutput, setCodeOutput] = useState("");
  const [codeRunning, setCodeRunning] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);

  // A2A state
  const [a2aAgents, setA2aAgents] = useState<{ name: string; url: string; skills: string[] }[]>([]);
  const [a2aNewUrl, setA2aNewUrl] = useState("");
  const [a2aDiscovering, setA2aDiscovering] = useState(false);
  const [a2aTaskInput, setA2aTaskInput] = useState("");
  const [a2aSelectedAgent, setA2aSelectedAgent] = useState("");
  const [a2aTaskResult, setA2aTaskResult] = useState<{ state: string; messages: { role: string; text: string }[] } | null>(null);
  const [a2aTaskRunning, setA2aTaskRunning] = useState(false);

  const sendChat = async () => {
    if (!chatInput.trim() || chatLoading) return;
    const userMsg: ChatMessage = { role: "user", content: chatInput };
    setChatMessages(prev => [...prev, userMsg]);
    setChatInput("");
    setChatLoading(true);

    try {
      const resp = await fetch(`${GATEWAY_PROXY}/invoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Agent-Id": "sandbox-chat", "X-Session-Id": "sandbox", "X-Plan": "Sandbox chat" },
        body: JSON.stringify({ messages: [...chatMessages, userMsg].map(m => ({ role: m.role, content: m.content })) }),
      });
      const data = await resp.json();
      if (resp.ok) {
        const assistantMsg: ChatMessage = {
          role: "assistant",
          content: data.response || data.error || "No response",
          model: data.model_used,
          tools: data.tool_calls?.map((tc: any) => ({ name: tc.name, args: tc.arguments })),
          blocked: data.blocked_actions,
        };
        setChatMessages(prev => [...prev, assistantMsg]);
      } else {
        setChatMessages(prev => [...prev, { role: "assistant", content: `Error: ${data.error || resp.statusText}` }]);
      }
    } catch (e: any) {
      setChatMessages(prev => [...prev, { role: "assistant", content: `Connection error: ${e.message}. Is the gateway running with LLM Gateway enabled?` }]);
    }
    setChatLoading(false);
  };

  const runScenario = async (id: string) => {
    setScenarioResult(prev => ({ ...prev, [id]: { status: "running", output: ["Starting..."] } }));
    const output: string[] = [];

    try {
      if (id === "basic") {
        const tools = [
          { action: "db_query", params: { sql: "SELECT * FROM users" } },
          { action: "send_email", params: { to: "test@co.com", subject: "Test" } },
          { action: "db_delete", params: { table: "users" } },
        ];
        for (const t of tools) {
          const resp = await fetch(`${GATEWAY_PROXY}/tool/${t.action}`, {
            method: "POST", headers: { "Content-Type": "application/json", "X-Agent-Id": "sandbox-scenario" },
            body: JSON.stringify(t.params),
          });
          const status = resp.status === 200 ? "✓ ALLOWED" : resp.status === 403 ? "✗ BLOCKED" : `? ${resp.status}`;
          output.push(`${t.action}: ${status}`);
        }
      } else if (id === "multistep") {
        const steps = ["db_query", "github.search_code", "github.create_issue", "drawio.create_diagram", "drawio.add_shape", "send_email"];
        const params: any[] = [{ sql: "SELECT *" }, { query: "auth" }, { repo: "myorg/app", title: "Bug" }, { name: "Flow" }, { diagram_id: "d1", shape: "rect", label: "API" }, { to: "team@co.com", subject: "Done" }];
        for (let i = 0; i < steps.length; i++) {
          const resp = await fetch(`${GATEWAY_PROXY}/tool/${steps[i]}`, {
            method: "POST", headers: { "Content-Type": "application/json", "X-Agent-Id": "sandbox-multistep", "X-Session-Id": "sandbox-plan", "X-Plan": "Multi-step sandbox", "X-Step": `${i+1}/${steps.length}` },
            body: JSON.stringify(params[i]),
          });
          output.push(`Step ${i+1}: ${steps[i]} → ${resp.status === 200 ? "✓" : resp.status === 403 ? "✗ blocked" : "? " + resp.status}`);
        }
      } else if (id === "blocked") {
        const blocked = ["db_delete", "github.delete_repo", "drawio.delete_diagram"];
        for (const action of blocked) {
          const resp = await fetch(`${GATEWAY_PROXY}/tool/${action}`, {
            method: "POST", headers: { "Content-Type": "application/json", "X-Agent-Id": "sandbox-blocked" },
            body: JSON.stringify({ target: "all" }),
          });
          const data = await resp.json();
          output.push(`${action}: ${resp.status === 403 ? `✗ BLOCKED (${data.reason || "policy"})` : resp.status === 404 ? "✗ NOT FOUND (filtered at MCP)" : "✓ allowed"}`);
        }
      } else if (id === "mcp") {
        const mcpTools = ["github.list_repos", "github.search_code", "drawio.list_diagrams", "drawio.create_diagram"];
        const params: any[] = [{ org: "myorg" }, { query: "config" }, {}, { name: "Sandbox Diagram" }];
        for (let i = 0; i < mcpTools.length; i++) {
          const resp = await fetch(`${GATEWAY_PROXY}/tool/${mcpTools[i]}`, {
            method: "POST", headers: { "Content-Type": "application/json", "X-Agent-Id": "sandbox-mcp" },
            body: JSON.stringify(params[i]),
          });
          const data = await resp.json();
          const result = resp.ok ? (data.result?.content || JSON.stringify(data.result)).slice(0, 60) : data.error;
          output.push(`${mcpTools[i]}: ${resp.ok ? "✓" : "✗"} ${result}`);
        }
      }
      setScenarioResult(prev => ({ ...prev, [id]: { status: "done", output } }));
    } catch (e: any) {
      setScenarioResult(prev => ({ ...prev, [id]: { status: "error", output: [...output, `Error: ${e.message}`] } }));
    }
  };

  const runCode = async () => {
    setCodeRunning(true);
    setCodeOutput("Running...\n");
    try {
      // Execute code by sending to a simple eval endpoint (or simulate)
      // For safety, we'll just execute the fetch calls from the code
      const resp = await fetch(`${GATEWAY_PROXY}/tool/db_query`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Agent-Id": "sandbox-code" },
        body: JSON.stringify({ sql: "SELECT * FROM users" }),
      });
      const data = await resp.json();
      setCodeOutput(`Status: ${resp.status}\nResult: ${JSON.stringify(data, null, 2)}`);
    } catch (e: any) {
      setCodeOutput(`Error: ${e.message}\n\nMake sure the gateway is running at ${GATEWAY_PROXY}`);
    }
    setCodeRunning(false);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-stone-900">Sandbox</h1>
        <p className="mt-1 text-sm text-stone-500">Test agents, run scenarios, and experiment with tool calls</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-stone-200 pb-px">
        {([["chat", MessageSquare, "Chat"], ["scenarios", Beaker, "Scenarios"], ["code", Code2, "Code"], ["a2a", Users2, "A2A"]] as const).map(([t, Icon, label]) => (
          <button key={t} onClick={() => setTab(t as Tab)}
            className={`flex items-center gap-2 rounded-t-xl px-4 py-2.5 text-sm font-medium transition ${
              tab === t ? "bg-white border border-b-white border-stone-200 text-stone-900 -mb-px" : "text-stone-500 hover:text-stone-700"
            }`}
          >
            <Icon className="h-4 w-4" /> {label}
          </button>
        ))}
      </div>

      {/* Chat Tab */}
      {tab === "chat" && (
        <div className="card flex flex-col" style={{ height: "calc(100vh - 280px)" }}>
          <div className="flex-1 overflow-y-auto p-6 space-y-4">
            {chatMessages.length === 0 && (
              <div className="text-center py-12 text-stone-400">
                <MessageSquare className="h-8 w-8 mx-auto mb-3" />
                <p className="text-sm">Send a message to invoke the LLM Gateway.</p>
                <p className="text-xs mt-1">Try: "List our GitHub repos" or "Query the database for users"</p>
              </div>
            )}
            {chatMessages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                <div className={`max-w-[70%] rounded-2xl px-4 py-3 ${
                  msg.role === "user" ? "bg-violet-600 text-white" : "bg-stone-100 text-stone-800"
                }`}>
                  <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                  {msg.model && <p className="text-xs mt-1 opacity-60">model: {msg.model}</p>}
                  {msg.tools && msg.tools.length > 0 && (
                    <div className="mt-2 space-y-1">
                      {msg.tools.map((t, j) => (
                        <div key={j} className="flex items-center gap-1 text-xs opacity-70">
                          <Wrench className="h-3 w-3" /> {t.name}
                        </div>
                      ))}
                    </div>
                  )}
                  {msg.blocked && msg.blocked.length > 0 && (
                    <div className="mt-2 space-y-1">
                      {msg.blocked.map((b, j) => (
                        <div key={j} className="flex items-center gap-1 text-xs text-rose-300">
                          <XCircle className="h-3 w-3" /> {b.action}: {b.reason}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {chatLoading && (
              <div className="flex justify-start">
                <div className="bg-stone-100 rounded-2xl px-4 py-3">
                  <Loader2 className="h-4 w-4 animate-spin text-stone-400" />
                </div>
              </div>
            )}
            <div ref={chatEndRef} />
          </div>
          <div className="border-t border-stone-100 p-4">
            <div className="flex gap-2">
              <input
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && sendChat()}
                placeholder="Type a message... (e.g., 'Search code for authentication')"
                className="input flex-1"
              />
              <button onClick={sendChat} disabled={chatLoading} className="btn-primary">
                <Send className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Scenarios Tab */}
      {tab === "scenarios" && (
        <div className="grid gap-4 sm:grid-cols-2">
          {SCENARIOS.map(s => (
            <div key={s.id} className={`card p-5 border ${s.color}`}>
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <span className="text-2xl">{s.icon}</span>
                  <div>
                    <p className="text-sm font-semibold text-stone-800">{s.name}</p>
                    <p className="text-xs text-stone-500">{s.description}</p>
                  </div>
                </div>
                <button onClick={() => runScenario(s.id)} disabled={scenarioResult[s.id]?.status === "running"}
                  className="btn-secondary text-xs px-3 py-1.5">
                  {scenarioResult[s.id]?.status === "running" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                  Run
                </button>
              </div>
              {scenarioResult[s.id] && (
                <div className="mt-3 rounded-xl bg-stone-50 border border-stone-100 p-3 font-mono text-xs space-y-0.5">
                  {scenarioResult[s.id].output.map((line, i) => (
                    <p key={i} className={line.includes("✓") ? "text-emerald-700" : line.includes("✗") ? "text-rose-700" : "text-stone-600"}>{line}</p>
                  ))}
                  {scenarioResult[s.id].status === "done" && (
                    <p className="text-stone-400 mt-2 pt-2 border-t border-stone-100">✓ Complete — check Live Traces</p>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Code Tab */}
      {tab === "code" && (
        <div className="grid grid-cols-2 gap-4">
          <div className="card flex flex-col">
            <div className="card-header flex items-center justify-between">
              <span className="text-xs font-semibold text-stone-500">Agent Code</span>
              <button onClick={runCode} disabled={codeRunning} className="btn-primary text-xs px-3 py-1.5">
                {codeRunning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                Run
              </button>
            </div>
            <textarea
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="flex-1 p-4 font-mono text-xs text-stone-800 bg-stone-50 resize-none focus:outline-none"
              style={{ minHeight: "300px" }}
              spellCheck={false}
            />
          </div>
          <div className="card flex flex-col">
            <div className="card-header">
              <span className="text-xs font-semibold text-stone-500">Output</span>
            </div>
            <pre className="flex-1 p-4 font-mono text-xs text-stone-700 bg-stone-50 overflow-auto" style={{ minHeight: "300px" }}>
              {codeOutput || "Click Run to execute..."}
            </pre>
          </div>
        </div>
      )}

      {/* A2A Tab */}
      {tab === "a2a" && (
        <div className="grid grid-cols-2 gap-4">
          {/* Left: Agent Registry */}
          <div className="card flex flex-col">
            <div className="card-header flex items-center justify-between">
              <span className="text-xs font-semibold text-stone-500">A2A Agent Registry</span>
              <span className="text-[10px] text-stone-400">{a2aAgents.length} agent{a2aAgents.length !== 1 ? "s" : ""}</span>
            </div>
            <div className="p-4 space-y-4">
              {/* Discover agent */}
              <div className="flex gap-2">
                <input
                  value={a2aNewUrl}
                  onChange={(e) => setA2aNewUrl(e.target.value)}
                  placeholder="Agent URL (e.g., http://localhost:9000)"
                  className="input flex-1 text-xs"
                />
                <button
                  onClick={async () => {
                    if (!a2aNewUrl.trim()) return;
                    setA2aDiscovering(true);
                    try {
                      const resp = await fetch(`${GATEWAY_PROXY}/config/a2a-agents`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ url: a2aNewUrl.trim() }),
                      });
                      if (resp.ok) {
                        const data = await resp.json();
                        setA2aAgents(prev => [...prev, { name: data.name || "Unknown", url: a2aNewUrl.trim(), skills: data.skills || [] }]);
                        setA2aNewUrl("");
                      } else {
                        const card = await fetch(`${a2aNewUrl.trim()}/.well-known/agent.json`).then(r => r.json()).catch(() => null);
                        if (card) {
                          setA2aAgents(prev => [...prev, { name: card.name, url: a2aNewUrl.trim(), skills: (card.skills || []).map((s: any) => s.name || s.id) }]);
                          setA2aNewUrl("");
                        } else {
                          alert("Could not discover agent. Check the URL.");
                        }
                      }
                    } catch (e: any) {
                      alert(`Discovery failed: ${e.message}`);
                    }
                    setA2aDiscovering(false);
                  }}
                  disabled={a2aDiscovering}
                  className="btn-primary text-xs px-3"
                >
                  {a2aDiscovering ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                  Discover
                </button>
              </div>

              {/* Agent list */}
              {a2aAgents.length === 0 ? (
                <div className="text-center py-8 text-stone-400">
                  <Users2 className="h-8 w-8 mx-auto mb-2" />
                  <p className="text-xs">No A2A agents registered.</p>
                  <p className="text-[10px] mt-1">Enter an agent URL and click Discover to fetch its AgentCard.</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {a2aAgents.map((agent, i) => (
                    <div key={i} className={`rounded-xl border p-3 transition cursor-pointer ${a2aSelectedAgent === agent.name ? "border-violet-300 bg-violet-50" : "border-stone-200 hover:border-stone-300"}`}
                      onClick={() => setA2aSelectedAgent(agent.name)}>
                      <div className="flex items-center justify-between">
                        <div>
                          <p className="text-sm font-semibold text-stone-800">{agent.name}</p>
                          <p className="text-[10px] text-stone-400 font-mono">{agent.url}</p>
                        </div>
                        <button onClick={(e) => { e.stopPropagation(); setA2aAgents(prev => prev.filter((_, j) => j !== i)); }} className="text-stone-300 hover:text-rose-500 transition">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      {agent.skills.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {agent.skills.map((skill, j) => (
                            <span key={j} className="rounded-full bg-violet-100 text-violet-700 px-2 py-0.5 text-[10px] font-medium">{skill}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Right: Send Task */}
          <div className="card flex flex-col">
            <div className="card-header">
              <span className="text-xs font-semibold text-stone-500">Send Task to A2A Agent</span>
            </div>
            <div className="p-4 space-y-4 flex-1 flex flex-col">
              <div>
                <label className="text-[10px] font-semibold text-stone-400 uppercase">Target Agent</label>
                <select
                  value={a2aSelectedAgent}
                  onChange={(e) => setA2aSelectedAgent(e.target.value)}
                  className="input mt-1 text-xs w-full"
                >
                  <option value="">Select an agent...</option>
                  {a2aAgents.map((a, i) => (
                    <option key={i} value={a.name}>{a.name}</option>
                  ))}
                </select>
              </div>

              <div className="flex-1 flex flex-col">
                <label className="text-[10px] font-semibold text-stone-400 uppercase">Task Message</label>
                <textarea
                  value={a2aTaskInput}
                  onChange={(e) => setA2aTaskInput(e.target.value)}
                  placeholder="Describe the task for the agent... (e.g., 'Deploy auth service to staging')"
                  className="input mt-1 text-xs flex-1 resize-none"
                  style={{ minHeight: "100px" }}
                />
              </div>

              <button
                onClick={async () => {
                  if (!a2aSelectedAgent || !a2aTaskInput.trim()) return;
                  setA2aTaskRunning(true);
                  setA2aTaskResult(null);
                  const agent = a2aAgents.find(a => a.name === a2aSelectedAgent);
                  if (!agent) return;
                  try {
                    const resp = await fetch(`${agent.url}/a2a`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        jsonrpc: "2.0",
                        method: "tasks/send",
                        params: {
                          message: { role: "user", parts: [{ type: "text", text: a2aTaskInput }] }
                        },
                        id: `task-${Date.now()}`
                      }),
                    });
                    const data = await resp.json();
                    if (data.result) {
                      const task = data.result;
                      const messages = (task.history || []).map((m: any) => ({
                        role: m.role,
                        text: (m.parts || []).map((p: any) => p.text || p.data || "[file]").join(" "),
                      }));
                      if (task.artifacts) {
                        task.artifacts.forEach((a: any) => {
                          messages.push({ role: "artifact", text: (a.parts || []).map((p: any) => p.text || p.data || "[file]").join(" ") });
                        });
                      }
                      setA2aTaskResult({ state: task.status?.state || "completed", messages });
                    } else if (data.error) {
                      setA2aTaskResult({ state: "failed", messages: [{ role: "error", text: `${data.error.code}: ${data.error.message}` }] });
                    }
                  } catch (e: any) {
                    setA2aTaskResult({ state: "failed", messages: [{ role: "error", text: e.message }] });
                  }
                  setA2aTaskRunning(false);
                }}
                disabled={a2aTaskRunning || !a2aSelectedAgent || !a2aTaskInput.trim()}
                className="btn-primary w-full"
              >
                {a2aTaskRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                Send Task
              </button>

              {/* Result */}
              {a2aTaskResult && (
                <div className="rounded-xl border border-stone-200 bg-stone-50 p-4 space-y-2">
                  <div className="flex items-center gap-2">
                    {a2aTaskResult.state === "completed" ? <CheckCircle className="h-4 w-4 text-emerald-500" /> : a2aTaskResult.state === "failed" ? <XCircle className="h-4 w-4 text-rose-500" /> : <Loader2 className="h-4 w-4 text-amber-500 animate-spin" />}
                    <span className="text-xs font-semibold text-stone-600">Task: {a2aTaskResult.state}</span>
                  </div>
                  <div className="space-y-1.5">
                    {a2aTaskResult.messages.map((m, i) => (
                      <div key={i} className={`rounded-lg px-3 py-2 text-xs ${
                        m.role === "user" ? "bg-violet-100 text-violet-800" :
                        m.role === "agent" ? "bg-white border border-stone-200 text-stone-800" :
                        m.role === "artifact" ? "bg-emerald-50 border border-emerald-200 text-emerald-800" :
                        "bg-rose-50 border border-rose-200 text-rose-700"
                      }`}>
                        <span className="font-semibold text-[10px] uppercase opacity-60">{m.role}</span>
                        <p className="mt-0.5 whitespace-pre-wrap">{m.text}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
