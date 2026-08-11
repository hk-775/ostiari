import { useEffect, useRef, useState } from "react";
import { CircleAlert, LoaderCircle, ShieldCheck } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { safeReturnPath, SSO_RETURN_KEY } from "../lib/sso";
import { useAuthStore } from "../stores/authStore";

export function SSOCallbackPage() {
  const completeSSO = useAuthStore((state) => state.completeSSO);
  const navigate = useNavigate();
  const started = useRef(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const query = new URLSearchParams(window.location.search);
    const token = fragment.get("token") || query.get("token");
    const callbackError = fragment.get("error") || query.get("error");
    const returnTo = safeReturnPath(sessionStorage.getItem(SSO_RETURN_KEY));

    sessionStorage.removeItem(SSO_RETURN_KEY);
    window.history.replaceState(null, document.title, window.location.pathname);

    if (callbackError) {
      setError("The identity provider did not complete sign-in");
      return;
    }
    if (!token) {
      setError("Single sign-on did not return an access token");
      return;
    }

    void completeSSO(token)
      .then(() => navigate(returnTo, { replace: true }))
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "Unable to complete sign-in");
      });
  }, [completeSSO, navigate]);

  return (
    <main className="flex min-h-screen items-center justify-center bg-stone-50 px-6">
      <div className="w-full max-w-sm rounded-lg border border-stone-200 bg-white p-8 text-center shadow-sm">
        {error ? (
          <>
            <CircleAlert className="mx-auto h-9 w-9 text-rose-600" />
            <h1 className="mt-4 text-lg font-semibold text-stone-900">Sign-in failed</h1>
            <p className="mt-2 text-sm text-stone-600">{error}</p>
            <button
              type="button"
              className="btn-secondary mt-6 w-full justify-center"
              onClick={() => navigate("/login", { replace: true })}
            >
              Return to sign in
            </button>
          </>
        ) : (
          <>
            <div className="relative mx-auto h-10 w-10">
              <ShieldCheck className="absolute inset-0 h-10 w-10 text-violet-600" />
              <LoaderCircle className="absolute -inset-1 h-12 w-12 animate-spin text-violet-200" />
            </div>
            <h1 className="mt-4 text-lg font-semibold text-stone-900">Completing sign-in</h1>
            <p className="mt-2 text-sm text-stone-500">Validating your control-plane session.</p>
          </>
        )}
      </div>
    </main>
  );
}
