import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { useAuthStore } from "../stores/authStore";
import { LogIn, Shield } from "lucide-react";
import { API_BASE } from "../lib/api";
import { safeReturnPath, SSO_RETURN_KEY } from "../lib/sso";

// The backend seeds admin@ostiari.ai / admin only outside production — in
// production `_seed_admin` refuses that credential and requires
// OSTIARI_ADMIN_PASSWORD. So prefilling unconditionally would show a password
// that cannot work there. On in dev; opt in for a deployed demo with
// VITE_DEMO_LOGIN=true.
const DEMO_LOGIN =
  import.meta.env.DEV || import.meta.env.VITE_DEMO_LOGIN === "true";

interface SSOConfig {
  enabled: boolean;
  provider: string | null;
  login_url: string | null;
}

const SSO_ERRORS: Record<string, string> = {
  sso_failed: "The identity provider declined or could not complete sign-in",
  token_exchange_failed: "The identity provider could not establish a session",
  no_id_token: "The identity provider did not return an identity token",
  invalid_token: "The identity provider returned an invalid identity token",
  no_email: "The identity provider did not provide an email address",
  account_disabled: "This account is disabled",
  provider_unavailable: "The identity provider is unavailable",
  invalid_callback: "The identity provider returned an incomplete response",
  invalid_state: "The sign-in request expired or could not be verified",
  sso_unavailable: "Single sign-on configuration is unavailable",
};

function providerLabel(provider: string | null): string {
  if (provider === "okta") return "Okta";
  if (provider === "cognito") return "Amazon Cognito";
  if (provider === "azure_ad") return "Microsoft";
  return "Single sign-on";
}

export function LoginPage() {
  // Prefilled with the seeded demo admin so the demo is one click to sign in.
  const [email, setEmail] = useState(DEMO_LOGIN ? "admin@ostiari.ai" : "");
  const [password, setPassword] = useState(DEMO_LOGIN ? "admin" : "");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [sso, setSSO] = useState<SSOConfig | null>(null);
  const login = useAuthStore((s) => s.login);
  const navigate = useNavigate();
  const location = useLocation();

  const from = (location.state as {
    from?: { pathname?: string; search?: string; hash?: string };
  } | null)?.from;
  const returnTo = safeReturnPath(
    from?.pathname
      ? `${from.pathname}${from.search || ""}${from.hash || ""}`
      : sessionStorage.getItem(SSO_RETURN_KEY),
  );

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const code = params.get("error");
    if (!code) return;
    const detail = params.get("detail");
    setError(detail || SSO_ERRORS[code] || "Single sign-on failed");
    navigate("/login", { replace: true, state: location.state });
  }, [location.search, location.state, navigate]);

  useEffect(() => {
    const controller = new AbortController();
    void fetch(`${API_BASE}/api/auth/sso/config`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : null)
      .then((config: SSOConfig | null) => {
        if (config?.enabled && config.login_url) setSSO(config);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(email, password);
      sessionStorage.removeItem(SSO_RETURN_KEY);
      navigate(returnTo, { replace: true });
    } catch (err: any) {
      setError(err.message || "Invalid credentials");
    } finally {
      setLoading(false);
    }
  };

  const handleSSO = () => {
    if (!sso?.login_url) return;
    setError("");
    sessionStorage.setItem(SSO_RETURN_KEY, returnTo);
    const loginURL = sso.login_url.startsWith("http")
      ? sso.login_url
      : `${API_BASE.replace(/\/$/, "")}/${sso.login_url.replace(/^\//, "")}`;
    window.location.assign(loginURL);
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-stone-50 to-stone-100">
      <div className="w-full max-w-md">
        <div className="rounded-2xl border border-stone-200 bg-white p-8 shadow-lg">
          {/* Header */}
          <div className="mb-8 text-center">
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-violet-50 border border-violet-200">
              <Shield className="h-7 w-7 text-violet-600" />
            </div>
            <h1 className="text-2xl font-bold tracking-tight text-stone-900">Ostiari</h1>
            <p className="mt-1 text-sm text-stone-500">Sign in to the control plane</p>
          </div>

          {/* Error */}
          {error && (
            <div className="mb-4 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {error}
            </div>
          )}

          {/* Form */}
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-stone-700 mb-1.5">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="input w-full"
                placeholder="admin@company.com"
                required
                autoFocus
              />
            </div>
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-stone-700 mb-1.5">
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="input w-full"
                placeholder="Enter your password"
                required
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="btn-primary w-full mt-2 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? "Signing in..." : "Sign in"}
            </button>
          </form>

          {sso && (
            <>
              <div className="my-5 flex items-center gap-3" aria-hidden="true">
                <div className="h-px flex-1 bg-stone-200" />
                <span className="text-xs font-medium uppercase text-stone-400">or</span>
                <div className="h-px flex-1 bg-stone-200" />
              </div>
              <button
                type="button"
                onClick={handleSSO}
                className="btn-secondary w-full justify-center"
              >
                <LogIn className="h-4 w-4" />
                Continue with {providerLabel(sso.provider)}
              </button>
            </>
          )}

          {/* Demo credentials (prefilled above) */}
          {DEMO_LOGIN && (
            <div className="mt-5 rounded-xl border border-violet-100 bg-violet-50/50 px-4 py-2.5 text-center text-xs text-stone-500">
              Demo login prefilled — <span className="font-medium text-stone-700">admin@ostiari.ai</span> / <span className="font-medium text-stone-700">admin</span>
            </div>
          )}
        </div>

        <p className="mt-6 text-center text-xs text-stone-400">
          Ostiari Control Plane v0.1.0
        </p>
      </div>
    </div>
  );
}
