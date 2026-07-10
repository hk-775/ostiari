"""Token authentication middleware for the dashboard."""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

EXEMPT_PREFIXES = ("/api/health", "/static", "/favicon.ico")


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer token authentication with route exemptions."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)

        if request.scope.get("type") == "websocket":
            token = request.query_params.get("token", "")
        else:
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ").strip()

        if not hmac.compare_digest(token.encode(), self._token.encode()):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
