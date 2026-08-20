"""Rotating control-plane workload credentials for a gateway.

Production supports two short-lived credential sources:

* a token file written by a projected-token volume or credential sidecar;
* OAuth 2.0 client credentials, with the access token cached only until its
  refresh window.

The legacy shared service/ingest keys remain available only for local
development compatibility.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx


class WorkloadCredentialError(RuntimeError):
    """Raised when a required workload credential cannot be loaded."""


@dataclass(frozen=True)
class _OAuthConfig:
    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    scope: str = ""
    audience: str = ""
    auth_method: str = "client_secret_basic"


_oauth_cache: tuple[_OAuthConfig, str, float] | None = None
_oauth_lock: asyncio.Lock | None = None
_oauth_lock_loop: asyncio.AbstractEventLoop | None = None


def _is_production() -> bool:
    return os.environ.get("OSTIARI_ENV", "").strip().lower() in {
        "production",
        "prod",
    }


def _read_bounded_file(path: str, *, setting: str) -> str:
    file_path = Path(path)
    try:
        if not file_path.is_file():
            raise WorkloadCredentialError(
                f"{setting} is not a regular file: {file_path}"
            )
        if file_path.stat().st_size > 64 * 1024:
            raise WorkloadCredentialError(
                f"{setting} exceeds the 64 KiB safety limit"
            )
        value = file_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkloadCredentialError(
            f"Cannot read {setting}: {exc}"
        ) from exc
    if not value:
        raise WorkloadCredentialError(f"{setting} is empty")
    return value


def _read_token_file(path: str) -> str:
    return _read_bounded_file(path, setting="OSTIARI_WORKLOAD_TOKEN_FILE")


def _oauth_settings_present() -> bool:
    return any(
        os.environ.get(name, "").strip()
        for name in (
            "OSTIARI_WORKLOAD_TOKEN_URL",
            "OSTIARI_WORKLOAD_CLIENT_ID",
            "OSTIARI_WORKLOAD_CLIENT_SECRET",
            "OSTIARI_WORKLOAD_CLIENT_SECRET_FILE",
            "OSTIARI_WORKLOAD_SCOPE",
            "OSTIARI_WORKLOAD_TOKEN_AUDIENCE",
            "OSTIARI_WORKLOAD_CLIENT_AUTH_METHOD",
        )
    )


def _oauth_config() -> _OAuthConfig | None:
    if not _oauth_settings_present():
        return None

    token_url = os.environ.get("OSTIARI_WORKLOAD_TOKEN_URL", "").strip()
    client_id = os.environ.get("OSTIARI_WORKLOAD_CLIENT_ID", "").strip()
    secret_file = os.environ.get(
        "OSTIARI_WORKLOAD_CLIENT_SECRET_FILE",
        "",
    ).strip()
    secret_value = os.environ.get("OSTIARI_WORKLOAD_CLIENT_SECRET", "").strip()
    if secret_file and secret_value:
        raise WorkloadCredentialError(
            "Configure only one of OSTIARI_WORKLOAD_CLIENT_SECRET_FILE or "
            "OSTIARI_WORKLOAD_CLIENT_SECRET"
        )
    client_secret = (
        _read_bounded_file(
            secret_file,
            setting="OSTIARI_WORKLOAD_CLIENT_SECRET_FILE",
        )
        if secret_file
        else secret_value
    )

    missing = [
        name
        for name, value in (
            ("OSTIARI_WORKLOAD_TOKEN_URL", token_url),
            ("OSTIARI_WORKLOAD_CLIENT_ID", client_id),
            (
                "OSTIARI_WORKLOAD_CLIENT_SECRET_FILE or "
                "OSTIARI_WORKLOAD_CLIENT_SECRET",
                client_secret,
            ),
        )
        if not value
    ]
    if missing:
        raise WorkloadCredentialError(
            "Incomplete OAuth workload credential: missing " + ", ".join(missing)
        )

    parsed = urlparse(token_url)
    if _is_production() and (
        parsed.scheme != "https" or not parsed.netloc
    ):
        raise WorkloadCredentialError(
            "OSTIARI_WORKLOAD_TOKEN_URL must use HTTPS in production"
        )

    auth_method = os.environ.get(
        "OSTIARI_WORKLOAD_CLIENT_AUTH_METHOD",
        "client_secret_basic",
    ).strip()
    if auth_method not in {"client_secret_basic", "client_secret_post"}:
        raise WorkloadCredentialError(
            "OSTIARI_WORKLOAD_CLIENT_AUTH_METHOD must be "
            "'client_secret_basic' or 'client_secret_post'"
        )

    return _OAuthConfig(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=os.environ.get("OSTIARI_WORKLOAD_SCOPE", "").strip(),
        audience=os.environ.get(
            "OSTIARI_WORKLOAD_TOKEN_AUDIENCE",
            "",
        ).strip(),
        auth_method=auth_method,
    )


async def _request_oauth_token(config: _OAuthConfig) -> tuple[str, float]:
    data = {"grant_type": "client_credentials"}
    if config.scope:
        data["scope"] = config.scope
    if config.audience:
        data["audience"] = config.audience

    auth: httpx.BasicAuth | None = None
    if config.auth_method == "client_secret_basic":
        auth = httpx.BasicAuth(config.client_id, config.client_secret)
    else:
        data["client_id"] = config.client_id
        data["client_secret"] = config.client_secret

    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
        ) as client:
            response = await client.post(config.token_url, data=data, auth=auth)
    except httpx.HTTPError as exc:
        raise WorkloadCredentialError(
            "Workload token endpoint is unavailable"
        ) from exc
    if not 200 <= response.status_code < 300:
        raise WorkloadCredentialError(
            f"Workload token endpoint returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
        token = str(payload["access_token"]).strip()
        expires_in = float(payload.get("expires_in", 300))
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkloadCredentialError(
            "Workload token endpoint returned an invalid response"
        ) from exc
    if not token:
        raise WorkloadCredentialError(
            "Workload token endpoint returned an empty access token"
        )
    if expires_in <= 0:
        raise WorkloadCredentialError(
            "Workload token endpoint returned an invalid expiry"
        )
    return token, expires_in


def _lock_for_current_loop() -> asyncio.Lock:
    global _oauth_lock, _oauth_lock_loop
    loop = asyncio.get_running_loop()
    if _oauth_lock is None or _oauth_lock_loop is not loop:
        _oauth_lock = asyncio.Lock()
        _oauth_lock_loop = loop
    return _oauth_lock


async def _oauth_token(config: _OAuthConfig) -> str:
    global _oauth_cache
    now = time.monotonic()
    if (
        _oauth_cache is not None
        and _oauth_cache[0] == config
        and now < _oauth_cache[2]
    ):
        return _oauth_cache[1]

    async with _lock_for_current_loop():
        now = time.monotonic()
        if (
            _oauth_cache is not None
            and _oauth_cache[0] == config
            and now < _oauth_cache[2]
        ):
            return _oauth_cache[1]
        token, expires_in = await _request_oauth_token(config)
        refresh_after = max(1.0, expires_in - min(60.0, expires_in * 0.2))
        _oauth_cache = (config, token, now + refresh_after)
        return token


def reset_workload_credentials() -> None:
    """Clear cached OAuth state for tests and explicit reconfiguration."""
    global _oauth_cache, _oauth_lock, _oauth_lock_loop
    _oauth_cache = None
    _oauth_lock = None
    _oauth_lock_loop = None


async def workload_token() -> str:
    """Return the current short-lived workload token."""
    token_file = os.environ.get("OSTIARI_WORKLOAD_TOKEN_FILE", "").strip()
    oauth = _oauth_config()
    if token_file and oauth is not None:
        raise WorkloadCredentialError(
            "Configure either OSTIARI_WORKLOAD_TOKEN_FILE or OAuth client "
            "credentials, not both"
        )
    if token_file:
        return _read_token_file(token_file)
    if oauth is not None:
        return await _oauth_token(oauth)

    token = os.environ.get("OSTIARI_WORKLOAD_TOKEN", "").strip()
    if token:
        if _is_production():
            raise WorkloadCredentialError(
                "OSTIARI_WORKLOAD_TOKEN is static and forbidden in production"
            )
        return token

    if _is_production():
        raise WorkloadCredentialError(
            "Production gateway requires OSTIARI_WORKLOAD_TOKEN_FILE or "
            "OAuth client credentials"
        )
    return ""


async def machine_headers(*, legacy: str = "service") -> dict[str, str]:
    """Build machine-auth headers for a control-plane request."""
    token = await workload_token()
    if token:
        return {"Authorization": f"Bearer {token}"}

    if legacy == "ingest":
        value = os.environ.get("OSTIARI_INGEST_KEY", "").strip()
        return {"X-Ingest-Key": value} if value else {}
    value = os.environ.get("OSTIARI_SERVICE_TOKEN", "").strip()
    return {"X-Ostiari-Service-Key": value} if value else {}


def validate_production_credential() -> None:
    """Fail startup unless one rotating production credential is configured."""
    if not _is_production():
        return
    if os.environ.get("OSTIARI_WORKLOAD_TOKEN", "").strip():
        raise WorkloadCredentialError(
            "OSTIARI_WORKLOAD_TOKEN is forbidden in production"
        )
    for name in ("OSTIARI_SERVICE_TOKEN", "OSTIARI_INGEST_KEY"):
        if os.environ.get(name, "").strip():
            raise WorkloadCredentialError(
                f"{name} is a legacy shared credential and forbidden in production"
            )

    token_file = os.environ.get("OSTIARI_WORKLOAD_TOKEN_FILE", "").strip()
    oauth = _oauth_config()
    if token_file and oauth is not None:
        raise WorkloadCredentialError(
            "Configure either OSTIARI_WORKLOAD_TOKEN_FILE or OAuth client "
            "credentials, not both"
        )
    if token_file:
        _read_token_file(token_file)
        return
    if oauth is not None:
        return
    raise WorkloadCredentialError(
        "Production gateway requires OSTIARI_WORKLOAD_TOKEN_FILE or OAuth "
        "client credentials"
    )
