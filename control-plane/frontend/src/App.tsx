import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  BrowserRouter,
  HashRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router";
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
import { Approvals } from "./pages/Approvals";
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
import { SSOCallbackPage } from "./pages/SSOCallbackPage";
import { IS_PUBLIC_SITE } from "./lib/publicSite";

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

const PROTECTED_ROUTES = [
  { path: "/dashboard", element: <Dashboard /> },
  { path: "/architecture", element: <Architecture /> },
  { path: "/gateways", element: <Gateways /> },
  { path: "/agents", element: <Agents /> },
  { path: "/tools", element: <Tools /> },
  { path: "/mcp-servers", element: <McpServers /> },
  { path: "/policies", element: <Policies /> },
  { path: "/costs", element: <Costs /> },
  { path: "/experiments", element: <Experiments /> },
  { path: "/models", element: <Models /> },
  { path: "/efficiency", element: <Efficiency /> },
  { path: "/traces", element: <LiveTraces /> },
  { path: "/shadow-report", element: <ShadowReport /> },
  { path: "/quotas", element: <Quotas /> },
  { path: "/agent-quotas", element: <AgentQuotas /> },
  { path: "/protocol-governance", element: <ProtocolGovernance /> },
  { path: "/sandbox", element: <Sandbox /> },
  { path: "/audit", element: <AuditLog /> },
  { path: "/compliance", element: <Compliance /> },
  { path: "/metering", element: <Metering /> },
  { path: "/payments", element: <Payments /> },
  { path: "/roi", element: <Roi /> },
  { path: "/discovery", element: <Discovery /> },
  { path: "/approvals", element: <Approvals /> },
  { path: "/token-broker", element: <TokenBroker /> },
  {
    path: "/providers",
    element: (
      <RequireAdmin>
        <Providers />
      </RequireAdmin>
    ),
  },
  {
    path: "/users",
    element: (
      <RequireAdmin>
        <Users />
      </RequireAdmin>
    ),
  },
] as const;

export const PROTECTED_ROUTE_PATHS = PROTECTED_ROUTES.map((route) => route.path);

function PublicArchitecturePage() {
  return (
    <main className="min-h-screen bg-stone-50">
      <div className="mx-auto max-w-6xl px-4 py-5 md:px-8 md:py-8">
        <Architecture />
      </div>
    </main>
  );
}

function PublicControlPlaneRedirect() {
  useEffect(() => {
    window.location.replace("https://github.com/hk-775/ostiari#see-it-in-90-seconds");
  }, []);

  return null;
}

function PublicRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/architecture" element={<PublicArchitecturePage />} />
      <Route path="/dashboard" element={<PublicControlPlaneRedirect />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function ControlPlaneRoutes() {
  return (
    <AuthInitializer>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/login" element={<LoginPage />} />
        <Route path="/auth/sso-callback" element={<SSOCallbackPage />} />
        <Route element={<RequireAuth><Layout /></RequireAuth>}>
          {PROTECTED_ROUTES.map((route) => (
            <Route key={route.path} path={route.path} element={route.element} />
          ))}
        </Route>
      </Routes>
    </AuthInitializer>
  );
}

export default function App() {
  const Router = IS_PUBLIC_SITE ? HashRouter : BrowserRouter;

  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        {IS_PUBLIC_SITE ? <PublicRoutes /> : <ControlPlaneRoutes />}
      </Router>
    </QueryClientProvider>
  );
}
