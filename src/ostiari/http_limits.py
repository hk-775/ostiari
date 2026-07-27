"""HTTP DoS guards shared by the gateway and control-plane apps.

A request body-size limit: unbounded ``await request.json()`` on every handler
means a multi-GB POST can OOM the process. This Starlette-compatible ASGI
middleware rejects oversized bodies with 413 before they are buffered.

Size is configurable via OSTIARI_MAX_BODY_BYTES (default 10 MiB). Clients like
Claude Code legitimately send large bodies (system prompt + tools, ~260 KB),
so the default is generous; set it per deployment.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Protocol

# Minimal ASGI signatures — spelled out here rather than depending on asgiref or
# starlette.types, since this module is shared by both apps and must not pull in
# a web framework of its own.
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class _RateStore(Protocol):
    """The one method RateLimitMiddleware needs from a shared store."""

    def rate_allow(self, key: str, limit: int, window_s: float) -> bool: ...


_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB


def max_body_bytes() -> int:
    raw = os.environ.get("OSTIARI_MAX_BODY_BYTES", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_MAX_BODY_BYTES


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds the configured cap with HTTP 413.

    Pure-ASGI (not BaseHTTPMiddleware) so it never interferes with streaming
    RESPONSES. It caps the incoming request body: fast-rejects on an oversized
    Content-Length, and enforces the limit while reading the body stream
    (handles missing/chunked length) by counting bytes as they pass through.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self._max = max_bytes or max_body_bytes()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast reject on a declared oversized length.
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        cl = headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > self._max:
            await _send_413(send, self._max)
            return

        # Read the full request body up front, enforcing the cap as we go
        # (handles missing/chunked Content-Length). Buffering the REQUEST body
        # does not affect RESPONSE streaming, so this is safe for SSE handlers.
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                # Nothing downstream can run; hand the disconnect through.
                async def _disconnected() -> Message:
                    return {"type": "http.disconnect"}
                await self.app(scope, _disconnected, send)
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self._max:
                await _send_413(send, self._max)
                return

        # Replay the buffered body to the downstream app. After the body is
        # delivered, defer to the REAL receive channel for any further messages
        # (e.g. http.disconnect) — returning a synthetic disconnect here would
        # make a StreamingResponse think the client vanished and abort the stream.
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


async def _send_413(send: Send, max_bytes: int) -> None:
    import json as _json
    payload = _json.dumps({"detail": f"request body exceeds limit ({max_bytes} bytes)"}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode())]})
    await send({"type": "http.response.body", "body": payload})


def rate_limit_rpm() -> int:
    """Gateway-wide per-caller requests/minute cap. 0/unset = disabled (dev)."""
    raw = os.environ.get("OSTIARI_GATEWAY_RATE_LIMIT_RPM", "").strip()
    return int(raw) if raw.isdigit() else 0


class RateLimitMiddleware:
    """Per-caller sliding-window rate limit (DoS guard) — off unless configured.

    Pure-ASGI (safe for streaming responses). Keyed by X-Agent-Id when present,
    else client IP. No-op unless OSTIARI_GATEWAY_RATE_LIMIT_RPM is set. Returns
    429 with Retry-After.

    In-process per gateway instance by default. Pass a `store` exposing
    `rate_allow(key, limit, window_s) -> bool` (the gateway's Redis-backed shared
    store) to make the limit hold **fleet-wide** across replicas; without it the
    limit is per-process (so N replicas ⇒ N× the effective rate).
    """

    def __init__(
        self, app: ASGIApp, rpm: int | None = None, store: _RateStore | None = None
    ) -> None:
        self.app = app
        self._rpm = rpm if rpm is not None else rate_limit_rpm()
        self._hits: dict[str, deque[float]] = {}
        self._store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._rpm <= 0:
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        key = headers.get("x-agent-id") or (scope.get("client") or ("unknown",))[0]

        if self._store is not None:
            allowed = self._store.rate_allow(key, self._rpm, 60.0)
        else:
            now = time.monotonic()
            window = self._hits.setdefault(key, deque())
            cutoff = now - 60.0
            while window and window[0] < cutoff:
                window.popleft()
            allowed = len(window) < self._rpm
            if allowed:
                window.append(now)

        if not allowed:
            import json as _json
            payload = _json.dumps({"detail": f"rate limit exceeded ({self._rpm}/min)"}).encode()
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(payload)).encode()),
                                    (b"retry-after", b"60")]})
            await send({"type": "http.response.body", "body": payload})
            return
        await self.app(scope, receive, send)
