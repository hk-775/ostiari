"""Application-managed Cognito browser sessions for CloudFront endpoints."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Protocol
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from src.gateway.config import MAX_BROWSER_SESSION_SECONDS
from src.gateway.models import AuthMethod, RequestContext

if TYPE_CHECKING:
    from src.gateway.auth.oidc_service import OIDCService
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "__Host-axon-session"
CSRF_COOKIE_NAME = "__Host-axon-csrf"
FLOW_COOKIE_NAME = "__Host-axon-login"
LOGIN_PATH = "/auth/login"
CALLBACK_PATH = "/auth/callback"
LOGOUT_PATH = "/auth/logout"
SIGNED_OUT_PATH = "/auth/signed-out"
CONFIG_PATH = "/auth/config"
BROWSER_AUTH_PATHS = frozenset(
    {
        LOGIN_PATH,
        CALLBACK_PATH,
        LOGOUT_PATH,
        SIGNED_OUT_PATH,
        CONFIG_PATH,
    }
)

DEFAULT_RETURN_TO = "/admin/dashboard"
SESSION_TOKEN_BYTES = 32
SESSION_TOKEN_LENGTH = 43
FLOW_TOKEN_BYTES = 32
FLOW_TOKEN_LENGTH = 43
MAX_AUTHORIZATION_CODE_BYTES = 4096
MAX_REFRESH_TOKEN_BYTES = 16 * 1024
MAX_TOKEN_RESPONSE_BYTES = 128 * 1024
MAX_TOKEN_LIFETIME_SECONDS = 3600
REFRESH_LEASE_SECONDS = 15
TOKEN_HTTP_TIMEOUT_SECONDS = 5.0
TOKEN_HTTP_CONNECT_TIMEOUT_SECONDS = 2.0
SCHEMA_VERSION = 1

_OPAQUE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_RESERVED_RETURN_PREFIXES = (
    "/auth",
    "/oauth2",
    "/saml",
    "/scim",
)
_NON_LOGIN_PATHS = frozenset({"/", "/health", "/ready"})
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


class BrowserAuthError(ValueError):
    """A sanitized invalid browser-authentication request."""


class BrowserSessionUnavailable(RuntimeError):
    """The authoritative browser session or token service is unavailable."""


def _https_origin(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{name} must be an HTTPS origin") from exc
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value.encode("utf-8")) > 2048
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port == 0
        or any(
            not character.isprintable() or character.isspace()
            for character in value
        )
    ):
        raise ValueError(f"{name} must be an HTTPS origin")
    return value.rstrip("/")


def _fixed_https_url(value: str, path: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{name} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS URL ending in {path}")
    return value


def _safe_return_to(value: str) -> str:
    """Validate a same-origin protected navigation target."""
    if not isinstance(value, str):
        raise BrowserAuthError("return_to must be a string")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise BrowserAuthError("return_to is not valid UTF-8") from exc
    if (
        not value
        or encoded_length > 2048
        or _INVALID_PERCENT_ESCAPE.search(value)
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        raise BrowserAuthError("return_to is invalid")

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise BrowserAuthError("return_to is malformed") from exc
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise BrowserAuthError("return_to must be a same-origin path")

    decoded_path = parsed.path
    for _ in range(3):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    lowered_path = decoded_path.casefold()
    if (
        decoded_path.startswith("//")
        or "//" in decoded_path
        or "\\" in decoded_path
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in decoded_path
        )
        or any(
            segment in {".", ".."}
            for segment in decoded_path.split("/")
        )
        or any(
            lowered_path == prefix
            or lowered_path.startswith(f"{prefix}/")
            for prefix in _RESERVED_RETURN_PREFIXES
        )
        or lowered_path in _NON_LOGIN_PATHS
    ):
        raise BrowserAuthError(
            "return_to must identify a protected application path"
        )
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _token_urlsafe(byte_count: int) -> str:
    value = secrets.token_urlsafe(byte_count)
    # token_urlsafe(32) is fixed at 43 characters, but keep generation closed
    # over the exact cookie/state grammar if an implementation ever changes.
    if _OPAQUE_TOKEN_PATTERN.fullmatch(value) is None:
        raise BrowserSessionUnavailable("secure token generation failed")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _flow_key(state: str) -> dict[str, str]:
    return {
        "PK": f"BROWSER_AUTH_FLOW#{_digest(state)}",
        "SK": "FLOW",
    }


def _session_key(token: str) -> dict[str, str]:
    return {
        "PK": f"BROWSER_SESSION#{_digest(token)}",
        "SK": "SESSION",
    }


def valid_session_token(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == SESSION_TOKEN_LENGTH
        and _OPAQUE_TOKEN_PATTERN.fullmatch(value)
    )


def _cookie_values(
    cookie_headers: list[str],
    cookie_name: str,
) -> list[str]:
    values: list[str] = []
    for raw_cookie in cookie_headers:
        for part in raw_cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == cookie_name:
                values.append(value.strip())
    return values


def browser_session_cookie_values(cookie_headers: list[str]) -> list[str]:
    """Return every app-session cookie, preserving malformed duplicates."""
    return _cookie_values(cookie_headers, SESSION_COOKIE_NAME)


def browser_flow_cookie_values(cookie_headers: list[str]) -> list[str]:
    """Return every login-flow cookie, preserving malformed duplicates."""
    return _cookie_values(cookie_headers, FLOW_COOKIE_NAME)


@dataclass(frozen=True)
class BrowserSessionConfig:
    """Cognito public-client and local session settings."""

    hosted_ui_url: str
    client_id: str
    callback_url: str
    signed_out_url: str
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    logout_endpoint: str = ""
    session_max_seconds: int = MAX_BROWSER_SESSION_SECONDS
    flow_ttl_seconds: int = 600
    default_return_to: str = DEFAULT_RETURN_TO

    def __post_init__(self) -> None:
        hosted_ui_url = _https_origin(
            self.hosted_ui_url,
            "hosted_ui_url",
        )
        callback_url = _fixed_https_url(
            self.callback_url,
            CALLBACK_PATH,
            "callback_url",
        )
        signed_out_url = _fixed_https_url(
            self.signed_out_url,
            SIGNED_OUT_PATH,
            "signed_out_url",
        )
        if (
            urlsplit(callback_url).netloc
            != urlsplit(signed_out_url).netloc
        ):
            raise ValueError(
                "callback_url and signed_out_url must share an origin"
            )
        endpoints = {
            "authorization_endpoint": (
                self.authorization_endpoint
                or f"{hosted_ui_url}/oauth2/authorize"
            ),
            "token_endpoint": (
                self.token_endpoint
                or f"{hosted_ui_url}/oauth2/token"
            ),
            "logout_endpoint": (
                self.logout_endpoint
                or f"{hosted_ui_url}/logout"
            ),
        }
        endpoint_paths = {
            "authorization_endpoint": "/oauth2/authorize",
            "token_endpoint": "/oauth2/token",
            "logout_endpoint": "/logout",
        }
        for name, value in endpoints.items():
            normalized = _fixed_https_url(
                value,
                endpoint_paths[name],
                name,
            )
            if (
                f"{urlsplit(normalized).scheme}://"
                f"{urlsplit(normalized).netloc}"
                != hosted_ui_url
            ):
                raise ValueError(
                    f"{name} must use the hosted UI origin"
                )
            object.__setattr__(self, name, normalized)
        if (
            not isinstance(self.client_id, str)
            or not self.client_id
            or self.client_id != self.client_id.strip()
            or len(self.client_id.encode("utf-8")) > 2048
            or any(
                not character.isprintable() or character.isspace()
                for character in self.client_id
            )
        ):
            raise ValueError("client_id is invalid")
        for name, value, minimum, maximum in (
            (
                "session_max_seconds",
                self.session_max_seconds,
                300,
                MAX_BROWSER_SESSION_SECONDS,
            ),
            (
                "flow_ttl_seconds",
                self.flow_ttl_seconds,
                60,
                900,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(
                    f"{name} must be between {minimum} and {maximum}"
                )
        _safe_return_to(self.default_return_to)
        object.__setattr__(self, "hosted_ui_url", hosted_ui_url)


class BrowserSessionStore(Protocol):
    """Distributed one-time-flow and opaque-session storage contract."""

    async def create_flow(self, item: dict[str, Any]) -> bool:
        ...

    async def consume_flow(
        self,
        key: dict[str, str],
        *,
        now: int,
    ) -> dict[str, Any] | None:
        ...

    async def create_session(self, item: dict[str, Any]) -> bool:
        ...

    async def get_session(
        self,
        key: dict[str, str],
    ) -> dict[str, Any] | None:
        ...

    async def replace_session(
        self,
        item: dict[str, Any],
        *,
        expected_revision: int,
        now: int,
    ) -> bool:
        ...

    async def delete_session(
        self,
        key: dict[str, str],
        *,
        expected_revision: int | None = None,
    ) -> bool:
        ...


def _conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return bool(
        isinstance(response, dict)
        and isinstance(response.get("Error"), dict)
        and response["Error"].get("Code")
        == "ConditionalCheckFailedException"
    )


def _native(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: _native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_native(item) for item in value]
    return value


class DynamoBrowserSessionStore:
    """Use the existing AxonLLM state table for fleet-wide browser sessions."""

    def __init__(self, persistence: DynamoPersistence) -> None:
        if not persistence.enabled:
            raise RuntimeError(
                "browser sessions require enabled DynamoDB persistence"
            )
        self._persistence = persistence

    async def create_flow(self, item: dict[str, Any]) -> bool:
        return await self._conditional_put(
            item,
            "attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )

    async def consume_flow(
        self,
        key: dict[str, str],
        *,
        now: int,
    ) -> dict[str, Any] | None:
        def _delete() -> dict[str, Any] | None:
            try:
                response = self._persistence._get_table().delete_item(
                    Key=key,
                    ConditionExpression=(
                        "attribute_exists(PK) AND expires_at > :now"
                    ),
                    ExpressionAttributeValues={":now": now},
                    ReturnValues="ALL_OLD",
                )
            except Exception as exc:
                if _conditional_failure(exc):
                    return None
                raise
            attributes = response.get("Attributes")
            return _native(attributes) if isinstance(attributes, dict) else None

        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:
            logger.error("Browser OAuth flow read failed", exc_info=True)
            raise BrowserSessionUnavailable(
                "browser authentication state is unavailable"
            ) from exc

    async def create_session(self, item: dict[str, Any]) -> bool:
        return await self._conditional_put(
            item,
            "attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )

    async def get_session(
        self,
        key: dict[str, str],
    ) -> dict[str, Any] | None:
        def _get() -> dict[str, Any] | None:
            response = self._persistence._get_table().get_item(
                Key=key,
                ConsistentRead=True,
            )
            item = response.get("Item")
            return _native(item) if isinstance(item, dict) else None

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error("Browser session read failed", exc_info=True)
            raise BrowserSessionUnavailable(
                "browser session storage is unavailable"
            ) from exc

    async def replace_session(
        self,
        item: dict[str, Any],
        *,
        expected_revision: int,
        now: int,
    ) -> bool:
        return await self._conditional_put(
            item,
            "revision = :expected AND absolute_expires_at > :now",
            {
                ":expected": expected_revision,
                ":now": now,
            },
        )

    async def delete_session(
        self,
        key: dict[str, str],
        *,
        expected_revision: int | None = None,
    ) -> bool:
        def _delete() -> bool:
            kwargs: dict[str, Any] = {"Key": key}
            if expected_revision is not None:
                kwargs.update(
                    {
                        "ConditionExpression": "revision = :expected",
                        "ExpressionAttributeValues": {
                            ":expected": expected_revision,
                        },
                    }
                )
            try:
                self._persistence._get_table().delete_item(**kwargs)
                return True
            except Exception as exc:
                if _conditional_failure(exc):
                    return False
                raise

        try:
            return await asyncio.to_thread(_delete)
        except Exception as exc:
            logger.error("Browser session delete failed", exc_info=True)
            raise BrowserSessionUnavailable(
                "browser session storage is unavailable"
            ) from exc

    async def _conditional_put(
        self,
        item: dict[str, Any],
        condition: str,
        values: dict[str, Any] | None = None,
    ) -> bool:
        def _put() -> bool:
            kwargs: dict[str, Any] = {
                "Item": item,
                "ConditionExpression": condition,
            }
            if values is not None:
                kwargs["ExpressionAttributeValues"] = values
            try:
                self._persistence._get_table().put_item(**kwargs)
                return True
            except Exception as exc:
                if _conditional_failure(exc):
                    return False
                raise

        try:
            return await asyncio.to_thread(_put)
        except Exception as exc:
            logger.error("Browser session write failed", exc_info=True)
            raise BrowserSessionUnavailable(
                "browser session storage is unavailable"
            ) from exc


def _required_integer(
    item: dict[str, Any],
    name: str,
    *,
    minimum: int = 0,
) -> int:
    value = item.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} is out of range")
    return value


def _required_string(
    item: dict[str, Any],
    name: str,
    *,
    max_bytes: int,
) -> str:
    value = item.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _context_item(context: RequestContext) -> dict[str, Any]:
    return {
        "user_id": context.user_id,
        "project_id": context.project_id,
        "roles": list(context.roles),
        "scopes": list(context.scopes),
        "tenant_id": context.tenant_id,
        "business_unit": context.business_unit,
        "environment": context.environment,
        "email": context.email,
        "issuer": context.issuer,
        "subject": context.subject,
    }


def _context_from_item(item: Any) -> RequestContext:
    if not isinstance(item, dict):
        raise ValueError("session context is malformed")
    user_id = _required_string(item, "user_id", max_bytes=2048)
    project_id = item.get("project_id", "")
    if not isinstance(project_id, str) or len(project_id.encode("utf-8")) > 2048:
        raise ValueError("project_id is malformed")
    lists: dict[str, list[str]] = {}
    for name in ("roles", "scopes"):
        values = item.get(name, [])
        if (
            not isinstance(values, list)
            or len(values) > 256
            or any(
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 1024
                for value in values
            )
        ):
            raise ValueError(f"{name} is malformed")
        lists[name] = list(values)
    optional: dict[str, str | None] = {}
    for name in (
        "tenant_id",
        "business_unit",
        "environment",
        "email",
        "issuer",
        "subject",
    ):
        value = item.get(name)
        if value is not None and (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 2048
        ):
            raise ValueError(f"{name} is malformed")
        optional[name] = value
    return RequestContext(
        user_id=user_id,
        project_id=project_id,
        roles=lists["roles"],
        scopes=lists["scopes"],
        auth_method=AuthMethod.OIDC_JWT,
        tenant_id=optional["tenant_id"],
        business_unit=optional["business_unit"],
        environment=optional["environment"],
        email=optional["email"],
        issuer=optional["issuer"],
        subject=optional["subject"],
    )


@dataclass(frozen=True)
class _StoredSession:
    item: dict[str, Any]
    context: RequestContext
    revision: int
    refresh_token: str
    refresh_after: int
    refresh_lease_until: int
    absolute_expires_at: int


class BrowserSessionService:
    """Own the PKCE flow, token refresh, and opaque distributed sessions."""

    def __init__(
        self,
        *,
        config: BrowserSessionConfig,
        store: BrowserSessionStore,
        oidc_service: OIDCService,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._store = store
        self._oidc_service = oidc_service
        self._clock = clock

    def login_url(self, return_to: str | None = None) -> str:
        target = _safe_return_to(return_to or self.config.default_return_to)
        return f"{LOGIN_PATH}?{urlencode({'return_to': target})}"

    async def begin_login(
        self,
        return_to: str | None = None,
    ) -> tuple[str, str]:
        target = _safe_return_to(return_to or self.config.default_return_to)
        state = _token_urlsafe(FLOW_TOKEN_BYTES)
        nonce = _token_urlsafe(FLOW_TOKEN_BYTES)
        verifier = _token_urlsafe(FLOW_TOKEN_BYTES)
        now = int(self._clock())
        item: dict[str, Any] = {
            **_flow_key(state),
            "entity_type": "browser_auth_flow",
            "schema_version": SCHEMA_VERSION,
            "nonce": nonce,
            "code_verifier": verifier,
            "return_to": target,
            "created_at": now,
            "expires_at": now + self.config.flow_ttl_seconds,
        }
        if not await self._store.create_flow(item):
            raise BrowserSessionUnavailable(
                "browser authentication state collision"
            )
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.callback_url,
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.config.authorization_endpoint}?{query}", state

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
    ) -> tuple[str, str]:
        if (
            not isinstance(code, str)
            or not code
            or len(code.encode("utf-8")) > MAX_AUTHORIZATION_CODE_BYTES
            or not isinstance(state, str)
            or len(state) != FLOW_TOKEN_LENGTH
            or _OPAQUE_TOKEN_PATTERN.fullmatch(state) is None
        ):
            raise BrowserAuthError("authorization response is invalid")

        now = int(self._clock())
        flow = await self._store.consume_flow(_flow_key(state), now=now)
        if flow is None:
            raise BrowserAuthError(
                "authorization state is invalid, expired, or already used"
            )
        try:
            if (
                flow.get("entity_type") != "browser_auth_flow"
                or _required_integer(flow, "schema_version") != SCHEMA_VERSION
            ):
                raise ValueError("flow is invalid")
            created_at = _required_integer(
                flow,
                "created_at",
                minimum=1,
            )
            expires_at = _required_integer(
                flow,
                "expires_at",
                minimum=1,
            )
            if (
                created_at > now
                or expires_at <= now
                or expires_at - created_at
                > self.config.flow_ttl_seconds
            ):
                raise ValueError("flow lifetime is invalid")
            nonce = _required_string(flow, "nonce", max_bytes=512)
            verifier = _required_string(
                flow,
                "code_verifier",
                max_bytes=512,
            )
            return_to = _safe_return_to(
                _required_string(flow, "return_to", max_bytes=2048)
            )
        except (BrowserAuthError, ValueError) as exc:
            raise BrowserAuthError(
                "authorization state is invalid"
            ) from exc

        tokens = await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "code": code,
                "redirect_uri": self.config.callback_url,
                "code_verifier": verifier,
            }
        )
        if tokens is None:
            raise BrowserAuthError("authorization code exchange failed")
        context, refresh_token, expires_in = await self._validated_tokens(
            tokens,
            expected_nonce=nonce,
            require_refresh_token=True,
        )

        absolute_expires_at = now + min(
            self.config.session_max_seconds,
            MAX_BROWSER_SESSION_SECONDS,
        )
        for _ in range(3):
            session_token = _token_urlsafe(SESSION_TOKEN_BYTES)
            item = {
                **_session_key(session_token),
                "entity_type": "browser_session",
                "schema_version": SCHEMA_VERSION,
                "revision": 1,
                "context": _context_item(context),
                "refresh_token": refresh_token,
                "refresh_after": self._refresh_after(
                    now,
                    expires_in,
                    absolute_expires_at,
                ),
                "absolute_expires_at": absolute_expires_at,
                "created_at": now,
                "updated_at": now,
                "expires_at": absolute_expires_at,
            }
            if await self._store.create_session(item):
                return session_token, return_to
        raise BrowserSessionUnavailable("browser session creation failed")

    async def authenticate(
        self,
        session_token: str,
    ) -> RequestContext | None:
        if not valid_session_token(session_token):
            return None
        key = _session_key(session_token)
        item = await self._store.get_session(key)
        if item is None:
            return None
        try:
            session = self._stored_session(item, key)
        except ValueError:
            logger.warning("Rejected malformed browser session row")
            await self._store.delete_session(key)
            return None

        now = int(self._clock())
        if session.absolute_expires_at <= now:
            await self._store.delete_session(
                key,
                expected_revision=session.revision,
            )
            return None
        if session.refresh_lease_until > now:
            # Another replica owns the short refresh lease. The session claims
            # remain usable while that replica rotates the token.
            return session.context
        if session.refresh_after > now:
            return session.context
        return await self._refresh_session(key, session, now)

    async def logout(self, session_token: str | None) -> None:
        if valid_session_token(session_token):
            await self._store.delete_session(_session_key(session_token))

    def hosted_logout_url(self) -> str:
        return (
            f"{self.config.logout_endpoint}?"
            + urlencode(
                {
                    "client_id": self.config.client_id,
                    "logout_uri": self.config.signed_out_url,
                }
            )
        )

    async def _refresh_session(
        self,
        key: dict[str, str],
        session: _StoredSession,
        now: int,
    ) -> RequestContext | None:
        leased_item = {
            **session.item,
            "revision": session.revision + 1,
            "refresh_lease_until": min(
                now + REFRESH_LEASE_SECONDS,
                session.absolute_expires_at,
            ),
            "updated_at": now,
        }
        if not await self._store.replace_session(
            leased_item,
            expected_revision=session.revision,
            now=now,
        ):
            winner = await self._store.get_session(key)
            if winner is None:
                return None
            try:
                current = self._stored_session(winner, key)
            except ValueError:
                return None
            return (
                current.context
                if current.absolute_expires_at > now
                else None
            )
        leased_revision = session.revision + 1

        tokens = await self._request_tokens(
            {
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "refresh_token": session.refresh_token,
            }
        )
        if tokens is None:
            await self._store.delete_session(
                key,
                expected_revision=leased_revision,
            )
            return None

        try:
            context, refresh_token, expires_in = (
                await self._validated_tokens(
                    tokens,
                    expected_nonce=None,
                    require_refresh_token=False,
                    fallback_refresh_token=session.refresh_token,
                )
            )
        except BrowserAuthError:
            await self._store.delete_session(
                key,
                expected_revision=leased_revision,
            )
            return None
        if (
            context.issuer != session.context.issuer
            or context.subject != session.context.subject
        ):
            await self._store.delete_session(
                key,
                expected_revision=leased_revision,
            )
            return None
        updated = {
            **leased_item,
            "revision": leased_revision + 1,
            "context": _context_item(context),
            "refresh_token": refresh_token,
            "refresh_after": self._refresh_after(
                now,
                expires_in,
                session.absolute_expires_at,
            ),
            "updated_at": now,
        }
        updated.pop("refresh_lease_until", None)
        if await self._store.replace_session(
            updated,
            expected_revision=leased_revision,
            now=now,
        ):
            return context

        winner = await self._store.get_session(key)
        if winner is None:
            return None
        try:
            refreshed = self._stored_session(winner, key)
        except ValueError:
            return None
        if refreshed.absolute_expires_at <= now:
            return None
        return refreshed.context

    async def _validated_tokens(
        self,
        tokens: dict[str, Any],
        *,
        expected_nonce: str | None,
        require_refresh_token: bool,
        fallback_refresh_token: str = "",
    ) -> tuple[RequestContext, str, int]:
        id_token = tokens.get("id_token")
        expires_in = tokens.get("expires_in")
        refresh_token = tokens.get(
            "refresh_token",
            fallback_refresh_token,
        )
        if (
            not isinstance(id_token, str)
            or not id_token
            or len(id_token.encode("utf-8")) > 64 * 1024
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 1 <= expires_in <= MAX_TOKEN_LIFETIME_SECONDS
            or not isinstance(refresh_token, str)
            or len(refresh_token.encode("utf-8"))
            > MAX_REFRESH_TOKEN_BYTES
            or (require_refresh_token and not refresh_token)
            or (not require_refresh_token and not refresh_token)
        ):
            raise BrowserAuthError(
                "Cognito returned an invalid token response"
            )
        context = await self._oidc_service.validate_id_token(
            id_token,
            expected_nonce=expected_nonce,
        )
        if context is None:
            raise BrowserAuthError("Cognito ID token validation failed")
        return context, refresh_token, expires_in

    async def _request_tokens(
        self,
        form: dict[str, str],
    ) -> dict[str, Any] | None:
        """Post one bounded public-client token request without redirects."""
        try:
            import httpx
        except ImportError as exc:
            raise BrowserSessionUnavailable(
                "Cognito token exchange support is unavailable"
            ) from exc

        timeout = httpx.Timeout(
            TOKEN_HTTP_TIMEOUT_SECONDS,
            connect=TOKEN_HTTP_CONNECT_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(
            max_connections=2,
            max_keepalive_connections=0,
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with asyncio.timeout(TOKEN_HTTP_TIMEOUT_SECONDS):
                    async with client.stream(
                        "POST",
                        self.config.token_endpoint,
                        data=form,
                        headers={
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                            "Content-Type": (
                                "application/x-www-form-urlencoded"
                            ),
                        },
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in (400, 401):
                            return None
                        if response.status_code != 200:
                            raise BrowserSessionUnavailable(
                                "Cognito token endpoint is unavailable"
                            )
                        encoding = response.headers.get(
                            "content-encoding",
                            "",
                        ).strip().lower()
                        media_type = response.headers.get(
                            "content-type",
                            "",
                        ).partition(";")[0].strip().lower()
                        if (
                            encoding not in ("", "identity")
                            or media_type != "application/json"
                        ):
                            raise BrowserSessionUnavailable(
                                "Cognito token response is invalid"
                            )
                        content_length = response.headers.get(
                            "content-length"
                        )
                        if content_length is not None and (
                            not content_length.isascii()
                            or not content_length.isdigit()
                            or int(content_length)
                            > MAX_TOKEN_RESPONSE_BYTES
                        ):
                            raise BrowserSessionUnavailable(
                                "Cognito token response is invalid"
                            )
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_TOKEN_RESPONSE_BYTES:
                                raise BrowserSessionUnavailable(
                                    "Cognito token response is too large"
                                )
        except BrowserSessionUnavailable:
            raise
        except Exception as exc:
            logger.warning("Cognito token request failed", exc_info=True)
            raise BrowserSessionUnavailable(
                "Cognito token endpoint is unavailable"
            ) from exc

        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=self._reject_duplicate_json_keys,
                parse_constant=self._reject_non_finite_json,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise BrowserSessionUnavailable(
                "Cognito token response is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise BrowserSessionUnavailable(
                "Cognito token response is invalid"
            )
        return payload

    @staticmethod
    def _reject_duplicate_json_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate token response member")
            result[key] = value
        return result

    @staticmethod
    def _reject_non_finite_json(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    @staticmethod
    def _refresh_after(
        now: int,
        expires_in: int,
        absolute_expires_at: int,
    ) -> int:
        skew = min(60, max(5, expires_in // 10))
        return min(
            now + max(1, expires_in - skew),
            absolute_expires_at,
        )

    @staticmethod
    def _stored_session(
        item: dict[str, Any],
        key: dict[str, str],
    ) -> _StoredSession:
        if (
            item.get("PK") != key["PK"]
            or item.get("SK") != key["SK"]
            or item.get("entity_type") != "browser_session"
            or _required_integer(item, "schema_version") != SCHEMA_VERSION
        ):
            raise ValueError("item is not a browser session")
        revision = _required_integer(item, "revision", minimum=1)
        refresh_after = _required_integer(
            item,
            "refresh_after",
            minimum=1,
        )
        refresh_lease_until = item.get("refresh_lease_until", 0)
        if (
            isinstance(refresh_lease_until, bool)
            or not isinstance(refresh_lease_until, int)
            or refresh_lease_until < 0
        ):
            raise ValueError("refresh_lease_until is malformed")
        absolute_expires_at = _required_integer(
            item,
            "absolute_expires_at",
            minimum=1,
        )
        created_at = _required_integer(
            item,
            "created_at",
            minimum=1,
        )
        expires_at = _required_integer(item, "expires_at", minimum=1)
        if (
            expires_at != absolute_expires_at
            or absolute_expires_at < created_at
            or absolute_expires_at - created_at
            > MAX_BROWSER_SESSION_SECONDS
            or refresh_after > absolute_expires_at
            or refresh_lease_until > absolute_expires_at
        ):
            raise ValueError("session TTL does not match absolute expiry")
        refresh_token = _required_string(
            item,
            "refresh_token",
            max_bytes=MAX_REFRESH_TOKEN_BYTES,
        )
        return _StoredSession(
            item=item,
            context=_context_from_item(item.get("context")),
            revision=revision,
            refresh_token=refresh_token,
            refresh_after=refresh_after,
            refresh_lease_until=refresh_lease_until,
            absolute_expires_at=absolute_expires_at,
        )


def _error(
    status_code: int,
    *,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "type": "authentication_error",
                "code": code,
                "message": message,
            }
        },
        headers=_NO_STORE_HEADERS,
    )


def _query_value(
    request: Request,
    name: str,
) -> str | None:
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise BrowserAuthError("duplicate query parameter")
    return values[0] if values else None


class BrowserAuthAPI:
    """No-store browser endpoints for the CloudFront Cognito flow."""

    def __init__(self, service: BrowserSessionService) -> None:
        self.service = service

    async def login(self, request: Request) -> Response:
        if set(request.query_params).difference({"return_to"}):
            return _error(
                400,
                code="invalid_login_parameters",
                message="The login request is invalid.",
            )
        try:
            return_to = _query_value(request, "return_to")
            target, flow_token = await self.service.begin_login(return_to)
        except BrowserAuthError:
            return _error(
                400,
                code="invalid_return_to",
                message=(
                    "return_to must identify a protected same-origin path."
                ),
            )
        except BrowserSessionUnavailable:
            return _error(
                503,
                code="browser_auth_unavailable",
                message="Browser authentication is temporarily unavailable.",
            )
        response = RedirectResponse(target, status_code=302)
        response.set_cookie(
            FLOW_COOKIE_NAME,
            flow_token,
            max_age=self.service.config.flow_ttl_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.headers.update(_NO_STORE_HEADERS)
        return response

    async def callback(self, request: Request) -> Response:
        keys = set(request.query_params)
        if keys != {"code", "state"}:
            response = _error(
                400,
                code="invalid_authorization_response",
                message="The authorization response is invalid.",
            )
            self._clear_flow_cookie(response)
            return response
        try:
            code = _query_value(request, "code")
            state = _query_value(request, "state")
            if code is None or state is None:
                raise BrowserAuthError("missing authorization response")
            flow_values = browser_flow_cookie_values(
                request.headers.getlist("cookie")
            )
            if (
                len(flow_values) != 1
                or not valid_session_token(state)
                or not valid_session_token(flow_values[0])
                or not hmac.compare_digest(flow_values[0], state)
            ):
                raise BrowserAuthError(
                    "authorization response is not bound to this browser"
                )
            session_token, return_to = await self.service.complete_login(
                code=code,
                state=state,
            )
        except BrowserAuthError:
            response = _error(
                400,
                code="invalid_authorization_response",
                message=(
                    "The authorization response is invalid or has expired."
                ),
            )
            self._clear_flow_cookie(response)
            return response
        except BrowserSessionUnavailable:
            response = _error(
                503,
                code="browser_auth_unavailable",
                message="Browser authentication is temporarily unavailable.",
            )
            self._clear_flow_cookie(response)
            return response

        response = RedirectResponse(return_to, status_code=302)
        self._clear_flow_cookie(response)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_token,
            max_age=self.service.config.session_max_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.headers.update(_NO_STORE_HEADERS)
        return response

    async def logout(self, request: Request) -> Response:
        values = browser_session_cookie_values(
            request.headers.getlist("cookie")
        )
        try:
            await self.service.logout(
                values[0] if len(values) == 1 else None
            )
        except BrowserSessionUnavailable:
            return _error(
                503,
                code="browser_auth_unavailable",
                message="Browser authentication is temporarily unavailable.",
            )
        response = JSONResponse(
            {"logout_url": self.service.hosted_logout_url()},
            headers=_NO_STORE_HEADERS,
        )
        self._clear_browser_cookies(response)
        return response

    async def signed_out(self, _request: Request) -> Response:
        response = HTMLResponse(
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>Signed out</title></head><body><main>"
            "<h1>Signed out</h1><p><a href=\"/auth/login\">Sign in</a></p>"
            "</main></body></html>",
            headers=_NO_STORE_HEADERS,
        )
        self._clear_browser_cookies(response)
        return response

    async def config(self, _request: Request) -> Response:
        return JSONResponse(
            {
                "browser_auth": {
                    "enabled": True,
                    "login_url": LOGIN_PATH,
                    "logout_url": LOGOUT_PATH,
                    "session_max_seconds": (
                        self.service.config.session_max_seconds
                    ),
                }
            },
            headers=_NO_STORE_HEADERS,
        )

    @staticmethod
    def _clear_browser_cookies(response: Response) -> None:
        BrowserAuthAPI._clear_flow_cookie(response)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.delete_cookie(
            CSRF_COOKIE_NAME,
            path="/",
            secure=True,
            samesite="strict",
        )

    @staticmethod
    def _clear_flow_cookie(response: Response) -> None:
        response.delete_cookie(
            FLOW_COOKIE_NAME,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )


def create_browser_auth_routes(api: BrowserAuthAPI) -> list[Route]:
    return [
        Route(LOGIN_PATH, api.login, methods=["GET"]),
        Route(CALLBACK_PATH, api.callback, methods=["GET"]),
        Route(LOGOUT_PATH, api.logout, methods=["POST"]),
        Route(SIGNED_OUT_PATH, api.signed_out, methods=["GET"]),
        Route(CONFIG_PATH, api.config, methods=["GET"]),
    ]
