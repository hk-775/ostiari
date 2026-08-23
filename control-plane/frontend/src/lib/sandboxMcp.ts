export interface GatewayMcpTool {
  name: string;
  description?: string;
  server?: string;
  input_schema?: Record<string, unknown>;
}

const SAFE_MCP_CALLS: Record<string, Record<string, unknown>> = {
  "fs.list_allowed_directories": {},
  "fs.read_text_file": { path: "/tmp/ostiari-mcp-sandbox/README.txt" },
  "fs.read_file": { path: "/tmp/ostiari-mcp-sandbox/README.txt" },
  "fs.list_directory": { path: "/tmp/ostiari-mcp-sandbox" },
};

export function selectSafeMcpCall(
  tools: GatewayMcpTool[],
): { name: string; params: Record<string, unknown> } | undefined {
  for (const [name, params] of Object.entries(SAFE_MCP_CALLS)) {
    if (tools.some((tool) => tool.name === name)) {
      return { name, params };
    }
  }
  return undefined;
}
