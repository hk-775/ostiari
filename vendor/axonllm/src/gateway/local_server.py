"""Package-local development server for the AxonLLM gateway.

Run: AXON_LOAD_DEMO_DATA=true python -m src.gateway.local_server
Then open: http://localhost:8000/admin/dashboard
"""

import os
from pathlib import Path
import sys
import threading
import webbrowser

import uvicorn
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.gateway import bootstrap as gateway_bootstrap
from src.gateway.admin.pricing_drift import audit_pricing, format_startup_notice
from src.gateway.admin.routes import AdminAPI
from src.gateway.config_loader import load_app_config, load_pricing_config
from src.gateway.dev_env import load_dev_env_file
from src.gateway.model_leaderboard import ModelLeaderboard
from src.gateway.model_registry import ModelRegistry


_RUNTIME_ROOT = Path(__file__).resolve().parent / "resources" / "runtime"


class _PackagedAdminAPI(AdminAPI):
    async def architecture(self, _request: Request) -> RedirectResponse:
        """Use the interactive architecture page already shipped in the wheel."""
        return RedirectResponse("/architecture.html", status_code=307)


class _PackagedModelLeaderboard(ModelLeaderboard):
    def load(
        self,
        config_path: str,
        valid_models: set[str] | None = None,
    ) -> None:
        """Resolve bootstrap's repository-relative default inside the wheel."""
        if Path(config_path) == Path("config/leaderboard.yaml"):
            config_path = str(
                _RUNTIME_ROOT / "config" / "leaderboard.yaml"
            )
        super().load(config_path, valid_models)


def _configure_packaged_runtime() -> None:
    """Point default runtime paths at immutable resources shipped in the wheel."""
    invocation_dir = Path.cwd()
    local_env = invocation_dir / ".env"
    if local_env.is_file():
        os.environ.setdefault("AXON_DEV_ENV_FILE", str(local_env))

    config_dir = _RUNTIME_ROOT / "config"
    defaults = {
        "AXON_MODELS_CONFIG": "models.yaml",
        "AXON_PROVIDERS_CONFIG": "providers.yaml",
        "AXON_PRICING_CONFIG": "pricing.yaml",
        "AXON_DEMO_SEED_CONFIG": "demo_seed.yaml",
        "AXON_CATALOG_CONFIG": "catalog.yaml",
        "AXON_ENSEMBLE_CONFIG": "ensemble.yaml",
        "AXON_SPOKES_CONFIG": "spokes.yaml",
    }
    for name, filename in defaults.items():
        configured = os.environ.get(name)
        if configured is None:
            path = config_dir / filename
        else:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = invocation_dir / path
        os.environ[name] = str(path.resolve())

    # AdminAPI intentionally resolves the public site from a project root.
    # Installed wheels have no project root, so bind that lookup to the
    # packaged runtime tree before building routes.
    from src.gateway.admin import routes as admin_routes

    admin_routes._PROJECT_ROOT = _RUNTIME_ROOT


def build_app() -> tuple:
    """Build the Starlette app and return (app, app_config)."""
    _configure_packaged_runtime()
    # Read the local .env before the demo-data default below is applied: the
    # loader's gate is whether the operator set AXON_LOAD_DEMO_DATA themselves.
    # Applying the direct-development default first would make an implicit demo
    # run read .env. It never overwrites an existing variable, so injected
    # secrets always win.
    load_dev_env_file()

    # Default to loading demo data when running the dev server directly
    if "AXON_LOAD_DEMO_DATA" not in os.environ:
        os.environ["AXON_LOAD_DEMO_DATA"] = "true"

    # This is the local-development entrypoint. Container and AWS deployments
    # inject their profile explicitly, so only an unconfigured direct run gets
    # the development contract.
    if "AXON_DEPLOYMENT_PROFILE" not in os.environ:
        os.environ["AXON_DEPLOYMENT_PROFILE"] = "development"

    # The local dev server is meant to be run without credentials so the admin
    # dashboard (which sends no auth header yet — see task #10) works out of the
    # box. Production defaults to ENFORCE; only this dev entrypoint opts out, and
    # only when the operator hasn't set AXON_AUTH_MODE themselves.
    if "AXON_AUTH_MODE" not in os.environ:
        os.environ["AXON_AUTH_MODE"] = "LOG_ONLY"

    app_config = load_app_config()
    original_admin_api = gateway_bootstrap.AdminAPI
    original_leaderboard = gateway_bootstrap.ModelLeaderboard
    gateway_bootstrap.AdminAPI = _PackagedAdminAPI
    gateway_bootstrap.ModelLeaderboard = _PackagedModelLeaderboard
    try:
        app = gateway_bootstrap.build_starlette_app(app_config)
    finally:
        gateway_bootstrap.AdminAPI = original_admin_api
        gateway_bootstrap.ModelLeaderboard = original_leaderboard
    return app, app_config


def main() -> None:
    """Start the packaged server without pre-bind external diagnostics."""
    app, app_config = build_app()

    registry = ModelRegistry()
    registry.load(app_config.models_config_path)

    base = f"http://localhost:{app_config.server_port}"
    print(f"\n  Dashboard: {base}/admin/dashboard")
    print(f"  Chat:      {base}/chat")

    # --- Pricing coverage ---
    # Reuse the registry already loaded above rather than building a second one.
    drift = audit_pricing(registry, load_pricing_config(app_config.pricing_config_path))
    drift_url = f"{base}/admin/pricing-drift"
    print(f"  Pricing:   {drift_url}\n")

    notice = format_startup_notice(drift, drift_url)
    if notice:
        print(notice + "\n")
        # Open the page rather than only printing about it. An unpriced model is
        # unavailable in production and under-accounted in development, and a
        # line in the startup scroll is exactly the kind of warning that gets
        # missed.
        #
        # Two guards: an explicit AXON_NO_BROWSER and a tty check. A CI runner or
        # non-interactive local process has nobody watching a browser, and while
        # webbrowser.open merely fails there, asking is pointless.
        opt_out = os.environ.get("AXON_NO_BROWSER", "").lower() in ("1", "true", "yes")
        if not opt_out and sys.stdout.isatty():
            # uvicorn.run blocks, so the open has to be deferred to a timer: the
            # server is not accepting connections yet at this point. daemon, so
            # a Ctrl-C in the first second and a half exits immediately rather
            # than waiting on the timer.
            def _open() -> None:
                try:
                    webbrowser.open(drift_url)
                except Exception:  # pragma: no cover - platform dependent
                    pass

            timer = threading.Timer(1.5, _open)
            timer.daemon = True
            timer.start()
    elif drift.total_mappings:
        print(f"  ✓ All {drift.total_mappings} provider mappings are priced.\n")

    # Live provider diagnostics run on demand behind the authenticated admin
    # route. They must not delay the packaged server from accepting health
    # checks when an external service is slow or unavailable.
    if not app_config.load_demo_data:
        print(f"  Readiness: {base}/admin/production-checklist\n")

    # Flush before handing off to uvicorn, which reconfigures logging and can
    # otherwise interleave its own output through the banner above. The gap is
    # only ever reported, never fatal: the gateway routes correctly either way,
    # and a dev server that refused to start over a missing price would be worse
    # than one that bills at zero.
    sys.stdout.flush()
    uvicorn.run(app, host=app_config.server_host, port=app_config.server_port)


if __name__ == "__main__":
    main()
