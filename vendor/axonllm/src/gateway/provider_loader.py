"""Load provider configurations from YAML + environment variables."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.gateway.provider_config import ProviderConfig
from src.gateway.provider_routes import ProviderRoute

try:
    import boto3
    from botocore.config import Config
except ImportError:  # The embedded HTTP router does not require AWS.
    boto3 = None
    Config = None

# Environment variable names for API keys per provider. Env vars take
# precedence over any api_key in providers.yaml, so secrets stay out of config
# files (see providers.yaml.example).
_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "google_ai": "GOOGLE_AI_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "ai21": "AI21_API_KEY",
}

_PROVIDER_SECRET_ARN_ENV = "AXON_PROVIDER_SECRET_ARN"
_PROVIDER_SECRET_VERSION_ENV = "AXON_PROVIDER_SECRET_VERSION"
_PROVIDER_SECRET_BOOTSTRAP_VERSION = "bootstrap"
_GCP_CREDENTIALS_JSON_ENV = "GCP_CREDENTIALS_JSON"
_GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_GOOGLE_REFRESH_MARGIN_SECONDS = 300.0
_GOOGLE_REFRESH_RETRY_SECONDS = 30.0
_SECRETS_MANAGER_CONFIG = (
    Config(
        connect_timeout=3,
        read_timeout=5,
        retries={"mode": "standard", "total_max_attempts": 3},
    )
    if Config is not None
    else None
)
_BASE_URL_ENV_MAP = {
    "azure_openai": "AZURE_OPENAI_ENDPOINT",
    "vertex_ai": "VERTEX_AI_ENDPOINT",
}
_EXTRA_PARAM_ENV_MAP = {
    "vertex_ai": {
        "project": "GCP_PROJECT_ID",
        "location": "GCP_LOCATION",
    },
}
_ALLOWED_SECRET_FIELDS = frozenset(
    {
        *_ENV_KEY_MAP.values(),
        _GCP_CREDENTIALS_JSON_ENV,
        *_BASE_URL_ENV_MAP.values(),
        *(
            env_name
            for provider_fields in _EXTRA_PARAM_ENV_MAP.values()
            for env_name in provider_fields.values()
        ),
    }
)

logger = logging.getLogger(__name__)


class GoogleCredentialProvider:
    """Keep a refreshable Google token ready without request-path network I/O."""

    def __init__(
        self,
        credentials: Any,
        *,
        request: Any | None = None,
        project_id: str | None = None,
        auto_refresh: bool = True,
    ) -> None:
        self._credentials = credentials
        self._request = request or _google_refresh_request()
        self._state_lock = threading.Lock()
        self._refreshing = False
        self._timer: threading.Timer | None = None
        self._workers: set[threading.Thread] = set()
        self._closed = False
        self._auto_refresh = auto_refresh
        self.project_id = project_id
        try:
            if not self._valid_token():
                self._refresh_credentials()
            elif self._auto_refresh:
                self._schedule_refresh()
        except BaseException:
            self.close()
            raise

    def get_token(self) -> str:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Google credential provider is closed")
        token = getattr(self._credentials, "token", None)
        if getattr(self._credentials, "valid", False) and isinstance(
            token,
            str,
        ) and token:
            return token
        self._start_background_refresh()
        raise RuntimeError(
            "Google access token is unavailable while refresh is pending"
        )

    def _valid_token(self) -> bool:
        token = getattr(self._credentials, "token", None)
        return (
            getattr(self._credentials, "valid", False)
            and isinstance(token, str)
            and bool(token)
        )

    def _refresh_credentials(self) -> None:
        self._credentials.refresh(self._request)
        if not self._valid_token():
            raise RuntimeError(
                "Google credential refresh returned no access token"
            )
        if self._auto_refresh:
            self._schedule_refresh()

    def _refresh_delay(self) -> float:
        expiry = getattr(self._credentials, "expiry", None)
        if not isinstance(expiry, datetime):
            return _GOOGLE_REFRESH_MARGIN_SECONDS
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        return max(1.0, remaining - _GOOGLE_REFRESH_MARGIN_SECONDS)

    def _schedule_refresh(
        self,
        delay: float | None = None,
    ) -> None:
        timer = threading.Timer(
            self._refresh_delay() if delay is None else delay,
            self._start_background_refresh,
        )
        timer.daemon = True
        with self._state_lock:
            if self._closed:
                return
            previous = self._timer
            self._timer = timer
            if previous is not None:
                previous.cancel()
            timer.start()

    def _start_background_refresh(self) -> None:
        with self._state_lock:
            pending = self._timer
            self._timer = None
            if self._closed or self._refreshing:
                if pending is not None:
                    pending.cancel()
                return
            self._refreshing = True
            if pending is not None:
                pending.cancel()
            worker = threading.Thread(
                target=self._background_refresh,
                name="axon-vertex-credential-refresh",
                daemon=True,
            )
            self._workers.add(worker)
            try:
                worker.start()
            except BaseException:
                self._workers.discard(worker)
                self._refreshing = False
                raise

    def _background_refresh(self) -> None:
        retry = False
        try:
            self._credentials.refresh(self._request)
            if not self._valid_token():
                raise RuntimeError(
                    "Google credential refresh returned no access token"
                )
        except Exception as exc:
            retry = True
            logger.warning(
                "Vertex AI credential background refresh failed (%s)",
                type(exc).__name__,
            )
        finally:
            with self._state_lock:
                self._refreshing = False
                self._workers.discard(threading.current_thread())
                should_reschedule = self._auto_refresh and not self._closed
        if should_reschedule:
            self._schedule_refresh(
                _GOOGLE_REFRESH_RETRY_SECONDS if retry else None
            )

    def start_auto_refresh(self) -> None:
        """Start timer ownership only after provider loading has succeeded."""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Google credential provider is closed")
            if self._auto_refresh:
                return
            self._auto_refresh = True
        self._schedule_refresh()

    def close(self, timeout_seconds: float = 10.0) -> None:
        """Stop future refreshes and briefly join any bounded refresh worker."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be non-negative")
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            timer = self._timer
            self._timer = None
            workers = tuple(self._workers)
        if timer is not None:
            timer.cancel()
        deadline = time.monotonic() + float(timeout_seconds)
        current = threading.current_thread()
        for worker in workers:
            if worker is current:
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))


class _BoundedGoogleRequest:
    def __init__(self, request: Any) -> None:
        self._request = request

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        requested_timeout = kwargs.get("timeout")
        if (
            not isinstance(requested_timeout, (int, float))
            or isinstance(requested_timeout, bool)
            or requested_timeout <= 0
        ):
            kwargs["timeout"] = 10
        else:
            kwargs["timeout"] = min(float(requested_timeout), 10)
        return self._request(*args, **kwargs)


def _google_refresh_request() -> Any:
    try:
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise RuntimeError(
            "Vertex AI requires the google-auth dependency"
        ) from exc
    return _BoundedGoogleRequest(Request())


def _load_google_credential_provider(
    secret_values: dict[str, str],
) -> GoogleCredentialProvider | None:
    credentials_json = (
        os.environ.get(_GCP_CREDENTIALS_JSON_ENV, "")
        or secret_values.get(_GCP_CREDENTIALS_JSON_ENV, "")
    )
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError as exc:
        if credentials_json or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        ):
            raise RuntimeError(
                "Vertex AI requires the google-auth dependency"
            ) from exc
        return None

    try:
        if credentials_json:
            payload = json.loads(credentials_json)
            if not isinstance(payload, dict):
                raise ValueError(
                    "GCP_CREDENTIALS_JSON must contain a JSON object"
                )
            credential_type = payload.get("type")
            if credential_type not in {
                "external_account",
                "service_account",
            }:
                raise ValueError(
                    "GCP_CREDENTIALS_JSON type must be external_account "
                    "or service_account"
                )
            credentials, project_id = (
                google.auth.load_credentials_from_dict(
                    payload,
                    scopes=[_GOOGLE_CLOUD_SCOPE],
                )
            )
        else:
            credentials, project_id = google.auth.default(
                scopes=[_GOOGLE_CLOUD_SCOPE],
            )
    except DefaultCredentialsError:
        if credentials_json:
            raise ValueError(
                "GCP_CREDENTIALS_JSON could not be loaded"
            ) from None
        return None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "refreshable Vertex AI credentials are malformed"
        ) from exc
    try:
        return GoogleCredentialProvider(
            credentials,
            project_id=project_id,
            auto_refresh=False,
        )
    except Exception as exc:
        raise RuntimeError(
            "Unable to initialize refreshable Vertex AI credentials"
        ) from exc


def load_provider_configs(config_path: str = "config/providers.yaml") -> dict[str, ProviderConfig]:
    """Load provider configs from YAML, with env var overrides for API keys.

    Returns a dict of provider_name -> ProviderConfig for providers that
    have valid credentials (either from the YAML file or env vars).
    Providers without credentials are silently skipped.
    """
    routes = load_provider_routes(config_path)
    configs: dict[str, ProviderConfig] = {}
    for route in routes:
        configs.setdefault(route.provider, route.to_provider_config())
    return configs


def _load_provider_routes_unowned(
    config_path: str = "config/providers.yaml",
    *,
    google_refreshers: dict[int, Any],
) -> list[ProviderRoute]:
    """Build routes while exposing refreshers to the ownership wrapper.

    A legacy provider document without ``routes`` becomes one
    ``<provider>:default`` route. A provider with ``routes`` can declare multiple
    independently weighted credentials/endpoints while inheriting provider-level
    defaults.
    """
    routes: list[ProviderRoute] = []
    secret_values = _load_provider_secret()

    # Production images intentionally exclude providers.yaml because operators
    # may put secrets in it. The distributable example contains only endpoint
    # metadata and is safe to combine with injected environment credentials.
    path = Path(config_path)
    if not path.exists() and path.name == "providers.yaml":
        example = path.with_name("providers.yaml.example")
        if example.exists():
            path = example
    yaml_providers: dict = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        yaml_providers = raw.get("providers", {})

    for provider_name, provider_data in yaml_providers.items():
        if not isinstance(provider_data, dict):
            continue

        declared_routes = provider_data.get("routes")
        if isinstance(declared_routes, list):
            candidates = [
                _merged_route_document(provider_data, route_data)
                for route_data in declared_routes
                if isinstance(route_data, dict)
            ]
        else:
            candidates = [dict(provider_data)]

        google_credential_provider: GoogleCredentialProvider | None = None
        google_credentials_loaded = False
        for index, route_data in enumerate(candidates):
            auth_type = route_data.get("auth_type", "api_key")
            credential_provider = None
            if auth_type == "gcp_service_account":
                if not google_credentials_loaded:
                    google_credential_provider = (
                        _load_google_credential_provider(secret_values)
                    )
                    if google_credential_provider is not None:
                        google_refreshers[id(google_credential_provider)] = (
                            google_credential_provider
                        )
                    google_credentials_loaded = True
                credential_provider = google_credential_provider
                credentials = (
                    {"credential_source": "google-auth"}
                    if credential_provider is not None
                    else {}
                )
            else:
                credentials = _build_credentials(
                    provider_name,
                    route_data,
                    secret_values,
                )
            if not credentials:
                continue

            endpoint_fallback = (
                route_data.get("endpoint")
                or route_data.get("base_url", "")
            )
            endpoint_env = route_data.get("endpoint_env")
            if endpoint_env:
                base_url = _configuration_value(
                    str(endpoint_env),
                    secret_values,
                    endpoint_fallback,
                )
            elif route_data.get("endpoint"):
                base_url = endpoint_fallback
            else:
                base_url = _configuration_value(
                    _BASE_URL_ENV_MAP.get(provider_name),
                    secret_values,
                    endpoint_fallback,
                )
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError(
                    f"{provider_name} is credentialled but has no base_url"
                )

            extra_params = dict(route_data.get("extra_params", {}))
            for field_name, env_name in _EXTRA_PARAM_ENV_MAP.get(
                provider_name,
                {},
            ).items():
                configured = _configuration_value(
                    env_name,
                    secret_values,
                    extra_params.get(field_name, ""),
                )
                if configured:
                    extra_params[field_name] = configured
            if provider_name == "vertex_ai":
                missing = [
                    field
                    for field in ("project", "location")
                    if not extra_params.get(field)
                ]
                if missing:
                    raise ValueError(
                        "vertex_ai is credentialled but is missing "
                        + ", ".join(missing)
                    )
                credential_project = getattr(
                    credential_provider,
                    "project_id",
                    None,
                )
                if (
                    credential_project
                    and credential_project != extra_params["project"]
                ):
                    raise ValueError(
                        "vertex_ai project does not match the Google credential "
                        "project"
                    )

            route_id = str(
                route_data.get("route_id")
                or route_data.get("id")
                or (
                    f"{provider_name}:default"
                    if len(candidates) == 1
                    else f"{provider_name}:route-{index + 1}"
                )
            )
            routes.append(
                ProviderRoute.from_dict(
                    {
                        **route_data,
                        "route_id": route_id,
                        "provider": provider_name,
                        "endpoint": base_url,
                        "credentials": credentials,
                        "extra_params": extra_params,
                        "credential_provider": credential_provider,
                    }
                )
            )

    return routes


def _close_google_refreshers(refreshers: dict[int, Any]) -> None:
    for provider in refreshers.values():
        close = getattr(provider, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception:
            logger.warning(
                "Vertex AI credential refresher cleanup failed",
                exc_info=True,
            )


def load_provider_routes(
    config_path: str = "config/providers.yaml",
) -> list[ProviderRoute]:
    """Load provider routes and transfer ownership only after full validation."""
    google_refreshers: dict[int, Any] = {}
    try:
        routes = _load_provider_routes_unowned(
            config_path,
            google_refreshers=google_refreshers,
        )
        for provider in google_refreshers.values():
            start = getattr(provider, "start_auto_refresh", None)
            if callable(start):
                start()
    except BaseException:
        _close_google_refreshers(google_refreshers)
        raise
    return routes


def _merged_route_document(
    provider_data: dict[str, Any],
    route_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge provider defaults with one route, excluding the routes collection."""
    base = {key: value for key, value in provider_data.items() if key != "routes"}
    merged = {**base, **route_data}
    merged["extra_headers"] = {
        **(base.get("extra_headers") or {}),
        **(route_data.get("extra_headers") or {}),
    }
    merged["extra_params"] = {
        **(base.get("extra_params") or {}),
        **(route_data.get("extra_params") or {}),
    }
    return merged


def _load_provider_secret() -> dict[str, str]:
    secret_arn = os.environ.get(_PROVIDER_SECRET_ARN_ENV, "").strip()
    if not secret_arn:
        return {}
    if boto3 is None or _SECRETS_MANAGER_CONFIG is None:
        raise RuntimeError(
            "Secrets Manager provider loading requires the "
            "'axon-llm[aws-control]' or 'axon-llm[server]' extra"
        )
    requested_version = os.environ.get(
        _PROVIDER_SECRET_VERSION_ENV,
        "",
    ).strip()
    request: dict[str, str] = {"SecretId": secret_arn}
    if (
        requested_version
        and requested_version != _PROVIDER_SECRET_BOOTSTRAP_VERSION
    ):
        request["VersionId"] = requested_version
    try:
        response = boto3.client(
            "secretsmanager",
            config=_SECRETS_MANAGER_CONFIG,
        ).get_secret_value(**request)
        if "VersionId" in request and (
            response.get("VersionId") != request["VersionId"]
        ):
            raise ValueError(
                "provider secret version does not match the runtime revision"
            )
        secret_string = response.get("SecretString")
        if not isinstance(secret_string, str):
            raise ValueError("provider secret must use SecretString")
        payload = json.loads(secret_string)
        if not isinstance(payload, dict):
            raise ValueError("provider secret must contain a JSON object")
        values: dict[str, str] = {}
        for field_name in _ALLOWED_SECRET_FIELDS:
            value = payload.get(field_name)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                raise ValueError(
                    f"provider secret field {field_name} must be a string"
                )
            values[field_name] = value
        return values
    except Exception as exc:
        raise RuntimeError(
            "Unable to load the configured provider credential secret"
        ) from exc


def _configuration_value(
    env_name: str | None,
    secret_values: dict[str, str],
    fallback: object,
) -> object:
    if env_name is None:
        return fallback
    return (
        os.environ.get(env_name, "")
        or secret_values.get(env_name, "")
        or fallback
    )


def _build_credentials(
    provider_name: str,
    provider_data: dict,
    secret_values: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build credentials dict from YAML data + env var overrides."""
    secret_values = secret_values or {}
    auth_type = provider_data.get("auth_type", "api_key")

    if auth_type == "api_key":
        env_var = provider_data.get("api_key_env") or _ENV_KEY_MAP.get(
            provider_name, ""
        )
        api_key = (
            os.environ.get(env_var, "")
            or secret_values.get(env_var, "")
            or provider_data.get("api_key", "")
        )
        if api_key:
            return {"api_key": api_key}
        return {}

    if auth_type == "azure_key":
        env_var = provider_data.get("api_key_env") or _ENV_KEY_MAP.get(
            provider_name, ""
        )
        api_key = (
            os.environ.get(env_var, "")
            or secret_values.get(env_var, "")
            or provider_data.get("api_key", "")
        )
        if api_key:
            return {"api_key": api_key}
        return {}

    if auth_type == "aws_credentials":
        return {
            "access_key": os.environ.get(
                provider_data.get("access_key_env", "AWS_ACCESS_KEY_ID"),
                provider_data.get("access_key", ""),
            ),
            "secret_key": os.environ.get(
                provider_data.get("secret_key_env", "AWS_SECRET_ACCESS_KEY"),
                provider_data.get("secret_key", ""),
            ),
            "session_token": os.environ.get(
                provider_data.get("session_token_env", "AWS_SESSION_TOKEN"),
                provider_data.get("session_token", ""),
            ),
            "region": os.environ.get(
                provider_data.get("region_env", "AWS_DEFAULT_REGION"),
                provider_data.get("region", "us-east-1"),
            ),
        }

    if auth_type == "gcp_service_account":
        return {}

    return {}
