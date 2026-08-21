"""HTTP and LLM-request security middleware.

``SecurityMiddleware`` retains the lightweight LLM endpoint marker used by the
gateway. ``ControlPlaneHTTPMiddleware`` owns browser-facing HTTP controls that
must run outside authentication so they also cover authentication failures:
bounded request bodies, browser-session CSRF protection, and production
response headers.
"""

from __future__ import annotations

import hmac
import secrets

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.gateway.auth.browser_session import (
    BROWSER_AUTH_PATHS,
    CSRF_COOKIE_NAME,
    browser_session_cookie_values,
    valid_session_token,
)

DEFAULT_REQUEST_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_ADMIN_MAX_BODY_BYTES = 64 * 1024
CSRF_HEADER_NAME = "x-axon-csrf-token"
_CSRF_TOKEN_BYTES = 32
_CSRF_TOKEN_LENGTH = 43
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CSRF_EXEMPT_UNSAFE_PATHS = frozenset({"/saml/acs"})
_TOKEN_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

_BASE_CONTENT_SECURITY_POLICY = (
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'self'; "
    "form-action 'self'"
)
_ADMIN_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'self'; "
    "form-action 'self'; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "font-src 'self' data:; "
    "style-src 'unsafe-inline'; "
    "script-src 'self'; "
    "worker-src 'none'"
)


class _BodyProblem(Exception):
    """A sanitized client-visible request framing or size failure."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", ())
        if key.lower() == name
    ]


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _valid_csrf_token(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _CSRF_TOKEN_LENGTH
        and all(character in _TOKEN_CHARACTERS for character in value)
    )


def _csrf_cookie(scope: Scope) -> str | None:
    matches: list[str] = []
    for raw_cookie in _header_values(scope, b"cookie"):
        for part in raw_cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == CSRF_COOKIE_NAME:
                matches.append(value.strip())
    if len(matches) != 1 or not _valid_csrf_token(matches[0]):
        return None
    return matches[0]


def _is_alb_browser_request(scope: Scope) -> bool:
    """Identify cookie-backed ALB OIDC without classifying bearer APIs as CSRF."""
    oidc_tokens = _header_values(scope, b"x-amzn-oidc-data")
    oidc_identities = _header_values(scope, b"x-amzn-oidc-identity")
    explicit_credentials = (
        _header_values(scope, b"authorization")
        or _header_values(scope, b"x-api-key")
    )
    return bool(
        not explicit_credentials
        and len(oidc_tokens) == 1
        and len(oidc_identities) == 1
        and oidc_tokens[0]
        and oidc_identities[0]
    )


def _is_app_browser_request(scope: Scope) -> bool:
    """Identify an opaque application session even when credentials conflict."""
    values = browser_session_cookie_values(
        _header_values(scope, b"cookie")
    )
    return bool(
        len(values) == 1
        and valid_session_token(values[0])
    )


def _csrf_request_is_valid(scope: Scope) -> bool:
    cookie_token = _csrf_cookie(scope)
    header_tokens = _header_values(scope, CSRF_HEADER_NAME.encode("ascii"))
    return bool(
        cookie_token
        and len(header_tokens) == 1
        and _valid_csrf_token(header_tokens[0])
        and hmac.compare_digest(cookie_token, header_tokens[0])
    )


def _declared_content_length(scope: Scope, max_bytes: int) -> int | None:
    values = _header_values(scope, b"content-length")
    transfer_encodings = _header_values(scope, b"transfer-encoding")
    if len(values) > 1 or len(transfer_encodings) > 1:
        raise _BodyProblem(
            400,
            "invalid_request_framing",
            "Ambiguous request body framing.",
        )
    if values and transfer_encodings:
        raise _BodyProblem(
            400,
            "invalid_request_framing",
            "Content-Length and Transfer-Encoding cannot be combined.",
        )
    if transfer_encodings and transfer_encodings[0].strip().lower() != "chunked":
        raise _BodyProblem(
            400,
            "invalid_request_framing",
            "Unsupported Transfer-Encoding.",
        )
    if not values:
        return None

    raw_value = values[0].strip()
    if not raw_value or not raw_value.isascii() or not raw_value.isdecimal():
        raise _BodyProblem(
            400,
            "invalid_content_length",
            "Content-Length must be a non-negative decimal integer.",
        )
    normalized = raw_value.lstrip("0") or "0"
    if len(normalized) > len(str(max_bytes)):
        raise _BodyProblem(
            413,
            "request_body_too_large",
            f"Request body exceeds the {max_bytes}-byte limit.",
        )
    declared = int(normalized)
    if declared > max_bytes:
        raise _BodyProblem(
            413,
            "request_body_too_large",
            f"Request body exceeds the {max_bytes}-byte limit.",
        )
    return declared


async def _read_bounded_body(
    receive: Receive,
    *,
    declared_length: int | None,
    max_bytes: int,
) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            raise _BodyProblem(
                400,
                "incomplete_request_body",
                "Request body was not received completely.",
            )
        if message_type != "http.request":
            raise _BodyProblem(
                400,
                "invalid_request_body",
                "Invalid request body event.",
            )

        chunk = message.get("body", b"")
        if len(chunk) > max_bytes - len(body):
            raise _BodyProblem(
                413,
                "request_body_too_large",
                f"Request body exceeds the {max_bytes}-byte limit.",
            )
        body.extend(chunk)
        if not message.get("more_body", False):
            break

    if declared_length is not None and declared_length != len(body):
        raise _BodyProblem(
            400,
            "content_length_mismatch",
            "Content-Length does not match the received request body.",
        )
    return bytes(body)


def _problem_response(problem: _BodyProblem) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status_code,
        content={
            "error": {
                "type": "request_error",
                "code": problem.code,
                "message": problem.message,
            }
        },
    )


class ControlPlaneHTTPMiddleware:
    """Apply browser and ingress controls without changing bearer API semantics."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        production: bool = False,
        request_max_body_bytes: int = DEFAULT_REQUEST_MAX_BODY_BYTES,
        admin_max_body_bytes: int = DEFAULT_ADMIN_MAX_BODY_BYTES,
    ) -> None:
        for name, value in (
            ("request_max_body_bytes", request_max_body_bytes),
            ("admin_max_body_bytes", admin_max_body_bytes),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        self.app = app
        self.production = production
        self.request_max_body_bytes = request_max_body_bytes
        self.admin_max_body_bytes = admin_max_body_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        admin_path = _is_admin_path(path)
        browser_session = (
            _is_alb_browser_request(scope)
            or _is_app_browser_request(scope)
        )
        auth_path = path in BROWSER_AUTH_PATHS
        static_asset = (
            path.startswith("/admin/static/")
            or path.startswith("/chat/static/")
        )
        csrf_cookie = _csrf_cookie(scope)
        csrf_to_set = (
            secrets.token_urlsafe(_CSRF_TOKEN_BYTES)
            if method == "GET"
            and browser_session
            and not static_asset
            and csrf_cookie is None
            else None
        )

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if self.production:
                    self._apply_production_headers(headers, admin_path=admin_path)
                if (
                    self.production
                    and admin_path
                    and not path.startswith("/admin/static/")
                ) or csrf_to_set is not None or auth_path:
                    headers["cache-control"] = "no-store"
                if auth_path:
                    headers["pragma"] = "no-cache"
                if csrf_to_set is not None:
                    headers.append(
                        "set-cookie",
                        f"{CSRF_COOKIE_NAME}={csrf_to_set}; "
                        "Path=/; Secure; SameSite=Strict",
                    )
            await send(message)

        body_limit = (
            None
            if method in _SAFE_METHODS
            else (
                self.admin_max_body_bytes
                if admin_path
                else self.request_max_body_bytes
            )
        )
        replay_body: bytes | None = None
        if body_limit is not None:
            try:
                declared_length = _declared_content_length(
                    scope,
                    body_limit,
                )
                replay_body = await _read_bounded_body(
                    receive,
                    declared_length=declared_length,
                    max_bytes=body_limit,
                )
            except _BodyProblem as problem:
                await _problem_response(problem)(
                    scope,
                    receive,
                    send_with_security_headers,
                )
                return

            if (
                browser_session
                and path not in _CSRF_EXEMPT_UNSAFE_PATHS
                and not _csrf_request_is_valid(scope)
            ):
                await JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "type": "authorization_error",
                            "code": "csrf_validation_failed",
                            "message": "A valid CSRF token is required.",
                        }
                    },
                )(scope, receive, send_with_security_headers)
                return

        if replay_body is None:
            await self.app(scope, receive, send_with_security_headers)
            return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": replay_body,
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay_receive, send_with_security_headers)

    @staticmethod
    def _apply_production_headers(
        headers: MutableHeaders,
        *,
        admin_path: bool,
    ) -> None:
        headers["strict-transport-security"] = "max-age=31536000"
        headers["x-content-type-options"] = "nosniff"
        headers["x-frame-options"] = "SAMEORIGIN"
        headers["referrer-policy"] = "no-referrer"
        headers["permissions-policy"] = (
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        )
        headers["x-permitted-cross-domain-policies"] = "none"
        headers["x-xss-protection"] = "0"
        headers["content-security-policy"] = (
            _ADMIN_CONTENT_SECURITY_POLICY
            if admin_path
            else _BASE_CONTENT_SECURITY_POLICY
        )


class SecurityMiddleware(BaseHTTPMiddleware):
    """Marks LLM endpoints on request.state for downstream awareness.

    All security enforcement (injection blocking, PII redaction, audit)
    is handled in GatewayAgent to avoid double policy resolution and
    to operate on the parsed message body rather than raw HTTP.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        request.state.is_llm_endpoint = self._is_llm_endpoint(path)
        return await call_next(request)

    def _is_llm_endpoint(self, path: str) -> bool:
        return path.startswith("/v1/") or path.startswith("/chat/completions") or path == "/chat/send"
