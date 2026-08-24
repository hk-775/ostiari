export type ArchitectureLane = "client" | "gateway" | "control" | "target";

export interface RuntimeStep {
  lane: ArchitectureLane;
  title: string;
  detail: string;
  contract?: string;
}

export interface RuntimeScenario {
  id: string;
  name: string;
  endpoint: string;
  summary: string;
  accent: string;
  steps: RuntimeStep[];
}

export interface DeploymentView {
  id: string;
  name: string;
  profiles: string[];
  summary: string;
  ingress: string[];
  compute: string[];
  state: string[];
  controls: string[];
  agentcore?: string[];
}

export const GATE_CHAIN = [
  { label: "Delegation", detail: "A2A edge, trust, and chain depth when the target is a peer agent" },
  { label: "Authorization", detail: "Endpoint, tool, model, and provider grants for the verified agent" },
  { label: "Quota", detail: "Request rate, token ceiling, projected spend, and agent budget" },
  { label: "Risk", detail: "Policy rules, parameter signals, and anomaly scoring" },
  { label: "Approval", detail: "Intervene-tier calls pause with HTTP 202 when HITL is enabled" },
  { label: "Payment", detail: "Metered pricing or downstream x402 settlement before side effects" },
  { label: "Execute", detail: "HTTP, MCP, A2A, or LLM provider dispatch" },
  { label: "Trace", detail: "Durable delivery of decisions, usage, cost, and payment events" },
] as const;

export const RUNTIME_SCENARIOS: RuntimeScenario[] = [
  {
    id: "direct-tool",
    name: "Direct tool",
    endpoint: "POST /tool/{action}",
    summary: "A deterministic agent names the tool. The gateway governs it before any side effect.",
    accent: "#0f766e",
    steps: [
      {
        lane: "client",
        title: "Agent requests a tool",
        detail: "The caller sends an action, JSON parameters, and its verified agent identity.",
        contract: "POST /tool/send_email",
      },
      {
        lane: "gateway",
        title: "Resolve the target",
        detail: "The gateway looks in the HTTP registry, MCP registry, and A2A registry. Unknown actions return 404.",
      },
      {
        lane: "gateway",
        title: "Authorize the agent",
        detail: "Per-agent grants are checked before quota, policy evaluation, or downstream execution.",
      },
      {
        lane: "gateway",
        title: "Enforce quota",
        detail: "Fleet or agent rate and budget limits can stop the call with HTTP 429.",
      },
      {
        lane: "gateway",
        title: "Score policy and risk",
        detail: "The Guard evaluates policy, parameters, and anomaly signals into allow, intervene, or block.",
      },
      {
        lane: "gateway",
        title: "Settle payment",
        detail: "Metered pricing runs after safety checks. Passthrough mode can also settle a downstream x402 challenge.",
      },
      {
        lane: "target",
        title: "Execute the HTTP tool",
        detail: "The proxy forwards trace context, enforces response limits, and returns the downstream status.",
      },
      {
        lane: "control",
        title: "Persist telemetry",
        detail: "The gateway reports the decision, score, duration, endpoint, usage, cost, and payment result.",
      },
    ],
  },
  {
    id: "hitl",
    name: "Human approval",
    endpoint: "POST /tool/{action} + X-Approval-Id",
    summary: "An intervene-tier call pauses, is decided in the control plane, and only executes after an approved retry.",
    accent: "#b45309",
    steps: [
      {
        lane: "client",
        title: "Agent requests a risky action",
        detail: "The request enters the same direct-tool path with the agent identity and parameters.",
      },
      {
        lane: "gateway",
        title: "Authorization and quota pass",
        detail: "Least-privilege and budget checks run before the action is scored.",
      },
      {
        lane: "gateway",
        title: "Guard returns intervene",
        detail: "The raw score falls between the configured allow and block thresholds.",
      },
      {
        lane: "control",
        title: "Create a pending approval",
        detail: "With HITL enabled, the gateway records an approval and responds with HTTP 202 instead of executing.",
        contract: "202 + approval_id",
      },
      {
        lane: "control",
        title: "Operator decides",
        detail: "An authenticated operator approves or denies the exact action and redacted parameters.",
      },
      {
        lane: "client",
        title: "Agent retries with the decision",
        detail: "The caller resubmits the same action with X-Approval-Id.",
      },
      {
        lane: "gateway",
        title: "Verify approval and payment",
        detail: "A denial returns 403. An approval continues through the payment gate.",
      },
      {
        lane: "target",
        title: "Execute once",
        detail: "The side effect occurs only after a valid approval and successful settlement.",
      },
      {
        lane: "control",
        title: "Record the complete trail",
        detail: "The pending decision, operator outcome, retry, and final execution remain linked in telemetry.",
      },
    ],
  },
  {
    id: "stdio-mcp",
    name: "Stdio MCP",
    endpoint: "POST /tool/fs.*",
    summary: "The source demo starts real MCP subprocesses with npx and governs their discovered tools through the same gateway.",
    accent: "#6d28d9",
    steps: [
      {
        lane: "client",
        title: "Agent calls a discovered MCP tool",
        detail: "The action uses the configured server prefix, such as fs.list_directory.",
      },
      {
        lane: "gateway",
        title: "Resolve the MCP registration",
        detail: "The MCP manager maps the qualified tool name to its connected server and input schema.",
      },
      {
        lane: "gateway",
        title: "Run authorization, quota, and risk",
        detail: "MCP tools use the same least-privilege and policy path as HTTP tools.",
      },
      {
        lane: "gateway",
        title: "Dispatch JSON-RPC over stdio",
        detail: "The gateway sends tools/call to the child process. No extra network service is required.",
      },
      {
        lane: "target",
        title: "Filesystem MCP executes",
        detail: "The demo server is sandboxed to /tmp/ostiari-mcp-sandbox and returns real content.",
      },
      {
        lane: "gateway",
        title: "Normalize the MCP result",
        detail: "Text content and errors are converted into the gateway response shape.",
      },
      {
        lane: "control",
        title: "Report an MCP trace",
        detail: "Telemetry identifies the mcp:// endpoint and the connected server.",
      },
    ],
  },
  {
    id: "a2a",
    name: "A2A delegation",
    endpoint: "POST /tool/a2a.{agent}",
    summary: "A governed agent delegates to a discovered peer without losing caller identity or chain provenance.",
    accent: "#0369a1",
    steps: [
      {
        lane: "client",
        title: "Agent calls a peer",
        detail: "The peer is exposed as an a2a.* tool after its agent card is discovered.",
      },
      {
        lane: "gateway",
        title: "Resolve the agent card",
        detail: "The gateway verifies that the target peer is still registered and connected.",
      },
      {
        lane: "gateway",
        title: "Check delegation policy",
        detail: "Allowed edges, effective trust, and maximum chain depth are evaluated before normal tool gates.",
      },
      {
        lane: "gateway",
        title: "Authorize, quota, and score",
        detail: "The caller must also be granted the a2a.* action and pass quota and Guard policy.",
      },
      {
        lane: "target",
        title: "Forward the A2A task",
        detail: "The gateway preserves X-Agent-Id, X-Delegation-Chain, and the session identifier.",
      },
      {
        lane: "target",
        title: "Peer agent responds",
        detail: "The downstream task state, messages, and artifacts return through the caller's gateway.",
      },
      {
        lane: "control",
        title: "Trace the delegation chain",
        detail: "The control plane can show the caller, callee, chain, trust decision, and outcome.",
      },
    ],
  },
  {
    id: "llm-intent",
    name: "LLM intent",
    endpoint: "POST /invoke",
    summary: "The embedded AxonLLM router selects a provider, then every generated tool call is governed before execution.",
    accent: "#be185d",
    steps: [
      {
        lane: "client",
        title: "Agent sends intent",
        detail: "The request carries messages plus the verified agent and session context.",
      },
      {
        lane: "gateway",
        title: "Authorize endpoint and model access",
        detail: "The gateway checks /invoke, requested model, provider access, and per-agent token limits.",
      },
      {
        lane: "gateway",
        title: "Apply input security and quota",
        detail: "Prompt controls run and projected model cost is reserved before a provider call.",
      },
      {
        lane: "gateway",
        title: "Route through AxonLLM",
        detail: "Operator policy, experiments, provider health, and fallback order select a concrete model route.",
      },
      {
        lane: "target",
        title: "Provider returns tool calls",
        detail: "Anthropic, OpenAI, Bedrock, or another configured provider returns a plan or final response.",
      },
      {
        lane: "gateway",
        title: "Govern each generated tool",
        detail: "Per-tool authorization, quota, and Guard policy run before HTTP or MCP execution.",
      },
      {
        lane: "gateway",
        title: "Continue model rounds",
        detail: "Tool results are returned to the selected model until a final answer or the round limit.",
      },
      {
        lane: "control",
        title: "Report usage and decisions",
        detail: "Model tokens, cost, route, experiment, tool calls, and blocked actions are reported together.",
      },
    ],
  },
];

export const DEPLOYMENT_VIEWS: DeploymentView[] = [
  {
    id: "local",
    name: "Local launcher",
    profiles: ["local-demo", "local-empty"],
    summary: "Docker Compose on one machine with a single governed gateway.",
    ingress: ["Browser -> :9000", "Agents -> :8421", "Operators/API -> :8400"],
    compute: ["React frontend", "FastAPI control plane", "Ostiari gateway with embedded AxonLLM"],
    state: ["SQLite named volume", "Valkey for shared runtime state"],
    controls: ["Read-only containers", "Health-gated startup", "Optional functional demo tools and seed job"],
  },
  {
    id: "source-demo",
    name: "Source demo",
    profiles: ["make demo-full"],
    summary: "The broadest local walkthrough for maintainers and evaluators.",
    ingress: ["Browser -> Vite :9000", "Four gateway ports", "A2A demo agent :9200"],
    compute: ["Four role-specific gateways", "Nine seeded agent records", "Demo HTTP service", "Real npx MCP subprocesses"],
    state: ["SQLite control-plane database", "In-process demo registries where documented"],
    controls: ["Destructive-tool block policies", "A2A delegation", "Simulated payments", "Live traces and sandbox"],
  },
  {
    id: "aws",
    name: "AWS evaluation",
    profiles: ["aws-demo", "aws-empty"],
    summary: "A cost-aware two-AZ CDK stack without an AgentCore runtime.",
    ingress: ["CIDR-restricted internet-facing ALB", "HTTP dashboard/API", "Gateway listener on :8421"],
    compute: ["ECS/Fargate frontend", "ECS/Fargate control plane", "ECS/Fargate gateway", "Optional demo-tools service"],
    state: ["Encrypted RDS PostgreSQL", "ElastiCache Serverless for Valkey", "Cloud Map private discovery"],
    controls: ["No NAT gateway", "Fargate public IPs for application services", "CloudWatch logs", "Deployment circuit breakers"],
  },
  {
    id: "agentcore",
    name: "AWS with AgentCore",
    profiles: ["aws-agentcore-demo", "aws-agentcore-empty"],
    summary: "The AWS evaluation stack plus an IAM-authorized AgentCore validation bridge.",
    ingress: ["CIDR-restricted ALB", "IAM-authorized AgentCore /invocations"],
    compute: ["ECS/Fargate platform services", "ARM64 AgentCore runtime", "Private gateway validation bridge"],
    state: ["RDS PostgreSQL", "Serverless Valkey", "Cloud Map", "Runtime log groups"],
    controls: ["Exactly two supported AgentCore AZs", "Private application subnets", "One NAT gateway", "X-Ray and CloudWatch telemetry"],
    agentcore: [
      "The runtime calls the private gateway /validate endpoint.",
      "Production can obtain an OAuth client-credentials token before validation.",
      "AWS-managed agentic_ai ENIs may outlive runtime deletion briefly.",
    ],
  },
  {
    id: "production",
    name: "Production",
    profiles: ["production", "production-agentcore"],
    summary: "A reviewed, fail-closed deployment from immutable artifacts.",
    ingress: ["TLS-only ALB", "Optional Route 53 aliases", "Explicit operator/VPN CIDRs"],
    compute: ["Private Fargate services", "Minimum two tasks per core service", "CPU autoscaling", "Optional AgentCore runtime"],
    state: ["Multi-AZ RDS with deletion protection", "Retained serverless Valkey", "Retained ALB logs in S3"],
    controls: ["Two NAT gateways", "WAF rate limiting", "OIDC workload and agent identity", "Secrets Manager", "CloudWatch alarms"],
    agentcore: [
      "Digest-pinned ARM64 image from ECR.",
      "Separate OAuth client and secret for the AgentCore bridge.",
      "Scoped trust policy, ECR pull, logs, metrics, and X-Ray permissions.",
    ],
  },
];

export const PROFILE_NAMES = [
  "local-demo",
  "local-empty",
  "aws-demo",
  "aws-empty",
  "aws-agentcore-demo",
  "aws-agentcore-empty",
  "production",
  "production-agentcore",
] as const;

export const DEMO_MODES = [
  {
    name: "Launcher demo",
    command: "./deploy/ostiari local up --profile local-demo",
    facts: ["One gateway", "Functional HTTP demo tools", "Seeded dashboard", "Simulated external payments"],
  },
  {
    name: "Source demo",
    command: "make demo-full",
    facts: ["Four gateways", "Nine agent records", "Real stdio MCP subprocesses", "A2A demo agent"],
  },
] as const;
