"""Deployment-environment helpers.

A single, explicit production signal so security-sensitive defaults can be
permissive in dev/demo but fail-closed in production. Set OSTIARI_ENV=production
(or prod) in real deployments.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ORG_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_production() -> bool:
    """True when OSTIARI_ENV indicates a production deployment."""
    return os.environ.get("OSTIARI_ENV", "").strip().lower() in ("production", "prod")


def env_flag(name: str) -> bool:
    """Return whether an environment flag is explicitly enabled."""
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def tenancy_mode() -> str:
    configured = os.environ.get("OSTIARI_TENANCY_MODE", "").strip().lower()
    if configured:
        return configured
    return "single"


def configured_org_id() -> str:
    return os.environ.get("OSTIARI_ORG_ID", "").strip() or "default"


def control_plane_replicas() -> int:
    raw = os.environ.get("OSTIARI_CONTROL_PLANE_REPLICAS", "1").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def tenant_is_allowed(org_id: str) -> bool:
    if not _ORG_ID_PATTERN.fullmatch(org_id):
        return False
    mode = tenancy_mode()
    if mode == "single":
        return org_id == configured_org_id()
    return mode == "multi"


def validate_production_posture() -> None:
    """Refuse a production control plane with fail-open or ephemeral settings."""
    if not is_production():
        return

    errors: list[str] = []

    if not env_flag("OSTIARI_REQUIRE_AUTH"):
        errors.append("OSTIARI_REQUIRE_AUTH must be enabled")
    if not env_flag("OSTIARI_NO_DEMO"):
        errors.append("OSTIARI_NO_DEMO must be enabled")
    if tenancy_mode() not in {"single", "multi"}:
        errors.append("OSTIARI_TENANCY_MODE must be 'single' or 'multi'")
    replicas = control_plane_replicas()
    if replicas < 1:
        errors.append("OSTIARI_CONTROL_PLANE_REPLICAS must be a positive integer")
    if not os.environ.get("REDIS_URL", "").strip():
        errors.append(
            "REDIS_URL is required for production rate limits, trace fan-out, "
            "and replica coordination"
        )
    org_id = os.environ.get("OSTIARI_ORG_ID", "").strip()
    if not org_id:
        errors.append("OSTIARI_ORG_ID must be set")
    elif not _ORG_ID_PATTERN.fullmatch(org_id):
        errors.append(
            "OSTIARI_ORG_ID must be 1-64 letters, digits, dots, underscores, or hyphens"
        )

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url.startswith("postgresql+asyncpg://"):
        errors.append("DATABASE_URL must use postgresql+asyncpg://")
    if not os.environ.get("OSTIARI_GATEWAY_CALLBACK_ALLOW", "").strip():
        errors.append("OSTIARI_GATEWAY_CALLBACK_ALLOW must be set")

    for name, minimum in (
        ("OSTIARI_JWT_SECRET", 32),
        ("OSTIARI_ADMIN_PASSWORD", 16),
        ("OSTIARI_CONFIG_ADMIN_KEY", 32),
        ("OSTIARI_GATEWAY_AGENT_TOKEN", 32),
    ):
        value = os.environ.get(name, "").strip()
        if len(value) < minimum:
            errors.append(f"{name} must be set and at least {minimum} characters")

    workload_issuer = os.environ.get(
        "OSTIARI_WORKLOAD_OIDC_ISSUER",
        "",
    ).strip()
    if urlparse(workload_issuer).scheme != "https" or not urlparse(
        workload_issuer
    ).netloc:
        errors.append("OSTIARI_WORKLOAD_OIDC_ISSUER must be an HTTPS issuer")
    if not os.environ.get("OSTIARI_WORKLOAD_OIDC_AUDIENCE", "").strip():
        errors.append("OSTIARI_WORKLOAD_OIDC_AUDIENCE must be set")
    for name in ("OSTIARI_SERVICE_TOKEN", "OSTIARI_INGEST_KEY"):
        if os.environ.get(name, "").strip():
            errors.append(
                f"{name} is a legacy shared credential and may not be set in production"
            )

    if not os.environ.get("OSTIARI_GATEWAY_AGENT_ID", "").strip():
        errors.append("OSTIARI_GATEWAY_AGENT_ID must be set")

    encryption_key = os.environ.get("OSTIARI_ENCRYPTION_KEY", "").strip()
    if not encryption_key:
        errors.append("OSTIARI_ENCRYPTION_KEY must be set")
    else:
        try:
            from cryptography.fernet import Fernet

            Fernet(encryption_key.encode())
        except Exception:  # noqa: BLE001 - return one aggregated startup error
            errors.append("OSTIARI_ENCRYPTION_KEY must be a valid Fernet key")

    origins = [
        value.strip()
        for value in os.environ.get("OSTIARI_CORS_ORIGINS", "").split(",")
        if value.strip()
    ]
    if not origins:
        errors.append("OSTIARI_CORS_ORIGINS must explicitly list HTTPS origins")
    elif any(
        origin == "*" or urlparse(origin).scheme != "https" or not urlparse(origin).netloc
        for origin in origins
    ):
        errors.append("OSTIARI_CORS_ORIGINS may contain only explicit HTTPS origins")

    frontend_url = os.environ.get("OSTIARI_FRONTEND_URL", "").strip()
    if frontend_url and urlparse(frontend_url).scheme != "https":
        errors.append("OSTIARI_FRONTEND_URL must use HTTPS")

    browser_oidc = any(
        os.environ.get(name, "").strip()
        for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET")
    )
    if browser_oidc:
        for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
            if not os.environ.get(name, "").strip():
                errors.append(f"{name} is required when browser SSO is configured")
        redirect_uri = os.environ.get("OIDC_REDIRECT_URI", "").strip()
        if urlparse(redirect_uri).scheme != "https":
            errors.append("OIDC_REDIRECT_URI must use HTTPS in production")
        if not frontend_url:
            errors.append("OSTIARI_FRONTEND_URL is required with browser SSO")

    if os.environ.get("OSTIARI_AUTH_MODE", "local").strip().lower() == "oidc":
        if not os.environ.get("OSTIARI_OIDC_ISSUER", "").strip():
            errors.append("OSTIARI_OIDC_ISSUER is required for direct OIDC auth")
        if not os.environ.get("OSTIARI_OIDC_AUDIENCE", "").strip():
            errors.append("OSTIARI_OIDC_AUDIENCE is required for direct OIDC auth")

    for name in (
        "OSTIARI_LOGIN_ATTEMPTS_PER_MINUTE",
        "OSTIARI_LOGIN_SOURCE_ATTEMPTS_PER_MINUTE",
    ):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            limit_value = int(raw)
        except ValueError:
            errors.append(f"{name} must be an integer")
            continue
        if limit_value < 1 or limit_value > 10_000:
            errors.append(f"{name} must be between 1 and 10000")

    if errors:
        details = "\n- ".join(errors)
        raise RuntimeError(
            "Refusing insecure production control-plane configuration:\n"
            f"- {details}"
        )


def data_dir() -> Path:
    """Directory for the development SQLite DB and legacy state import.

    Honors ``OSTIARI_DATA_DIR``, else falls back to ``<repo>/control-plane/data``
    for a dev checkout.

    Exists because the two callers previously derived this from ``__file__``
    independently, with a different number of ``.parent`` hops, and so disagreed:
    the database landed in ``control-plane/data`` while legacy state.json landed in
    ``control-plane/backend/data``. In the container (which runs from ``/app``)
    that split put state.json in ``/app/data`` — a root-owned directory the
    non-root runtime user could not create. Runtime configuration now lives in
    SQL; the JSON path remains only for one-time upgrades from older versions.

    Deliberately no mkdir here: a resolver that has a filesystem side effect at
    import time is exactly the problem being fixed. Callers create it when they
    are about to write.
    """
    override = os.environ.get("OSTIARI_DATA_DIR", "").strip()
    if override:
        return Path(override)
    # …/control-plane/backend/control_plane/env.py → …/control-plane/data
    return Path(__file__).resolve().parent.parent.parent / "data"


def default_sqlite_url() -> str:
    """SQLite URL under :func:`data_dir`, creating the directory.

    Used only when ``DATABASE_URL`` is unset, i.e. dev checkouts — every image and
    manifest in ``deploy/`` sets it. Deliberately a function rather than a module
    constant: as a constant the mkdir ran at *import* time, so merely importing the
    app wrote to disk.

    Both ``control_plane.database`` and ``alembic/env.py`` call this. They used to
    each derive the path from their own ``__file__`` with a different number of
    ``.parent`` hops, which is a silent-drift hazard: `alembic upgrade head` with no
    DATABASE_URL would migrate one file while the app opened another, so the
    migration reported success and the app still failed on the missing column.
    """
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{d / 'control_plane.db'}"


# The known-insecure default JWT secret; refused in production.
DEFAULT_DEV_JWT_SECRET = "ostiari-dev-secret-change-in-prod"
