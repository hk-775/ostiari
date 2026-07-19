import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes, Navigate, useLocation } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Gateways } from "./pages/Gateways";
import { Tools } from "./pages/Tools";
import { Policies } from "./pages/Policies";
import { McpServers } from "./pages/McpServers";
import { AuditLog } from "./pages/AuditLog";
import { Costs } from "./pages/Costs";
import { LiveTraces } from "./pages/LiveTraces";
import { ShadowReport } from "./pages/ShadowReport";
import { ProtocolGovernance } from "./pages/ProtocolGovernance";
import { Compliance } from "./pages/Compliance";
import { Metering } from "./pages/Metering";
import { Payments } from "./pages/Payments";
import { Roi } from "./pages/Roi";
import { Discovery } from "./pages/Discovery";
import { TokenBroker } from "./pages/TokenBroker";
import { Experiments } from "./pages/Experiments";
import { Models } from "./pages/Models";
import { Efficiency } from "./pages/Efficiency";
import { Quotas } from "./pages/Quotas";
import { Sandbox } from "./pages/Sandbox";
import { Agents } from "./pages/Agents";
import { Architecture } from "./pages/Architecture";
import { Landing } from "./pages/Landing";
import { LoginPage } from "./pages/LoginPage";
import { Users } from "./pages/Users";
import { Providers } from "./pages/Providers";
import { useAuthStore } from "./stores/authStore";
import { AgentQuotas } from "./pages/AgentQuotas";

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchInterval: 10000 } },
});

function RequireAuth({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const location = useLocation();

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);

  if (user && user.role !== "admin") {
    return <Navigate to="/dashboard" replace />;
  }
  return <>{children}</>;
}

function AuthInitializer({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, fetchMe, user } = useAuthStore();

  useEffect(() => {
    if (isAuthenticated && !user) {
      fetchMe();
    }
  }, [isAuthenticated, user, fetchMe]);

  return <>{children}</>;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthInitializer>
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/login" element={<LoginPage />} />
            <Route element={<RequireAuth><Layout /></RequireAuth>}>
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
              <Route path="/shadow-report" element={<ShadowReport />} />
              <Route path="/quotas" element={<Quotas />} />
              <Route path="/agent-quotas" element={<AgentQuotas />} />
              <Route path="/protocol-governance" element={<ProtocolGovernance />} />
              <Route path="/sandbox" element={<Sandbox />} />
              <Route path="/audit" element={<AuditLog />} />
              <Route path="/compliance" element={<Compliance />} />
              <Route path="/metering" element={<Metering />} />
              <Route path="/payments" element={<Payments />} />
              <Route path="/roi" element={<Roi />} />
              <Route path="/discovery" element={<Discovery />} />
              <Route path="/token-broker" element={<TokenBroker />} />
              <Route path="/providers" element={<RequireAdmin><Providers /></RequireAdmin>} />
              <Route path="/users" element={<RequireAdmin><Users /></RequireAdmin>} />
            </Route>
          </Routes>
        </AuthInitializer>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
