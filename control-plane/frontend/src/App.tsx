import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Gateways } from "./pages/Gateways";
import { Tools } from "./pages/Tools";
import { Policies } from "./pages/Policies";
import { McpServers } from "./pages/McpServers";
import { AuditLog } from "./pages/AuditLog";
import { Costs } from "./pages/Costs";
import { LiveTraces } from "./pages/LiveTraces";
import { Experiments } from "./pages/Experiments";
import { Models } from "./pages/Models";
import { Efficiency } from "./pages/Efficiency";
import { Quotas } from "./pages/Quotas";
import { Sandbox } from "./pages/Sandbox";
import { Agents } from "./pages/Agents";
import { Architecture } from "./pages/Architecture";
import { Landing } from "./pages/Landing";
import { AgentQuotas } from "./pages/AgentQuotas";

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchInterval: 10000 } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route element={<Layout />}>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/architecture" element={<Architecture />} />
            <Route path="/gateways" element={<Gateways />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/tools" element={<Tools />} />
            <Route path="/mcp-servers" element={<McpServers />} />
            <Route path="/policies" element={<Policies />} />
            <Route path="/costs" element={<Costs />} />
            <Route path="/experiments" element={<Experiments />} />
            <Route path="/models" element={<Models />} />
            <Route path="/efficiency" element={<Efficiency />} />
            <Route path="/traces" element={<LiveTraces />} />
            <Route path="/quotas" element={<Quotas />} />
            <Route path="/agent-quotas" element={<AgentQuotas />} />
            <Route path="/sandbox" element={<Sandbox />} />
            <Route path="/audit" element={<AuditLog />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
