"""Chat API endpoints for the LLM-Router service.

Provides Starlette routes for:
- Model listing (GET /api/models)
- Non-streaming chat completion (POST /api/chat)
- Streaming chat completion via SSE (POST /api/chat/stream)
- Chat UI page (GET /chat)
"""

from __future__ import annotations

import json
import logging
import pathlib
import traceback
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from src.gateway.chat.request_body import (
    DEFAULT_CHAT_REQUEST_MAX_BYTES,
    JSONBodyError,
    read_json_object,
)
from src.gateway.request_validator import RequestValidator

if TYPE_CHECKING:
    from src.gateway.chat.client_agent import ClientAgent

_STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
_PUBLIC_STATIC_ASSETS = frozenset(
    {
        "chat.js",
        "playground.js",
        "routing.js",
        "vendor/react.production.min.js",
        "vendor/react-dom.production.min.js",
    }
)


async def chat_static_asset(request: Request) -> Response:
    """Serve only the immutable browser assets required by the chat UIs."""
    relative_path = request.path_params.get("path", "")
    if relative_path not in _PUBLIC_STATIC_ASSETS:
        return PlainTextResponse("Not found", status_code=404)

    target = _STATIC_DIR / relative_path
    if not target.is_file():
        return PlainTextResponse("Not found", status_code=404)
    return Response(
        target.read_bytes(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _identity_from_context(
    request: Request,
) -> tuple[str | None, str | None, str | None]:
    """Resolve trustworthy user, project, and tenant attribution.

    Identity comes from the authenticated request context established by
    AuthMiddleware — never from the request body, which a caller can forge.
    For an authenticated request (API key or OIDC) we use the context's
    user_id/project_id/tenant_id. In an ANONYMOUS context (local dev / LOG_ONLY
    mode) we return three ``None`` values so ClientAgent falls back to its
    configured defaults and the dev chat UI keeps working without credentials.
    """
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        return None, None, None
    # AuthMethod.ANONYMOUS => unauthenticated (dev/LOG_ONLY); don't trust it for attribution.
    if getattr(ctx.auth_method, "value", None) == "anonymous":
        return None, None, None
    user_id = ctx.user_id or None
    project_id = ctx.project_id or None
    tenant_id = getattr(ctx, "tenant_id", None) or None
    return user_id, project_id, tenant_id


def _authorized_project(request: Request):
    """Return the project object resolved by tenant authorization, if any."""
    context = getattr(request.state, "context", None)
    return getattr(context, "authorized_project", None)


def _allow_legacy_project_lookup(request: Request) -> bool:
    """Allow the global project map only outside canonical principal mode."""
    return (
        getattr(request.state, "principal", None) is None
        and _authorized_project(request) is None
    )


class ChatAPI:
    """Route handlers for the client-facing chat interface."""

    def __init__(
        self,
        client_agent: ClientAgent,
        *,
        max_request_bytes: int = DEFAULT_CHAT_REQUEST_MAX_BYTES,
        request_validator: RequestValidator | None = None,
    ) -> None:
        self.client_agent = client_agent
        self.max_request_bytes = max_request_bytes
        self.request_validator = (
            request_validator
            if request_validator is not None
            else _resolve_request_validator(client_agent)
        )

    # ------------------------------------------------------------------
    # GET /api/models
    # ------------------------------------------------------------------

    async def list_models(self, request: Request) -> JSONResponse:
        """Return available models as a JSON array."""
        try:
            # Access-filtered model list is scoped to the authenticated identity,
            # not query params (which a caller can set to any project/user).
            user_id, project_id, tenant_id = _identity_from_context(request)
            kwargs = {"project_id": project_id, "user_id": user_id}
            if tenant_id is not None:
                kwargs["tenant_id"] = tenant_id
            project = _authorized_project(request)
            if project is not None:
                kwargs["authorized_project"] = project
            elif _allow_legacy_project_lookup(request):
                kwargs["allow_legacy_project_lookup"] = True
            models = await self.client_agent.list_models(**kwargs)
            return JSONResponse(models)
        except Exception:
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

    # ------------------------------------------------------------------
    # GET /api/users
    # ------------------------------------------------------------------

    async def list_users(self, request: Request) -> JSONResponse:
        """Return available user IDs for the user selector."""
        try:
            users = await self.client_agent.get_available_users()
            return JSONResponse(users)
        except Exception:
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

    # ------------------------------------------------------------------
    # POST /api/chat
    # ------------------------------------------------------------------

    async def chat(self, request: Request) -> JSONResponse:
        """Non-streaming chat completion."""
        # Parse and validate request body
        body, error_response = await _parse_chat_body(
            request,
            request_validator=self.request_validator,
            max_bytes=self.max_request_bytes,
        )
        if error_response is not None:
            return error_response

        model = body.get("model", "")
        messages = body["messages"]
        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        top_p = body.get("top_p")
        stop = body.get("stop")
        system = body.get("system")
        provider = body.get("provider")
        tools = body.get("tools")
        tool_choice = body.get("tool_choice")
        tool_options: dict[str, Any] = {}
        if tools is not None:
            tool_options["tools"] = tools
            tool_options["tool_choice"] = tool_choice
        # Identity for attribution comes from the authenticated context, NOT the
        # request body — the body's user_id/project_id are ignored so a caller
        # cannot impersonate another tenant's quota/budget/model-access context.
        user_id, project_id, tenant_id = _identity_from_context(request)
        if tenant_id is not None:
            tool_options["tenant_id"] = tenant_id
        if _allow_legacy_project_lookup(request):
            tool_options["allow_legacy_project_lookup"] = True
        # Extract smart_routing flag from context
        context = body.get("context", {})
        smart_routing = context.get("smart_routing", False) if isinstance(context, dict) else False

        try:
            if (project := _authorized_project(request)) is not None:
                tool_options["authorized_project"] = project
            response = await self.client_agent.chat(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                top_p=top_p, stop=stop, system=system,
                user_id=user_id, project_id=project_id, provider=provider, smart_routing=smart_routing,
                **tool_options,
            )
        except Exception:
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

        # Extract rate limit headers from the response (if present)
        rate_limit_headers = response.pop("_rate_limit_headers", None)

        # Error response from the gateway
        if "error" in response:
            status_code = response.get("status_code", 500)
            json_response = JSONResponse(
                {"error": response["error"]},
                status_code=status_code,
            )
            if rate_limit_headers:
                for header_name, header_value in rate_limit_headers.items():
                    json_response.headers[header_name] = str(header_value)
            return json_response

        json_response = JSONResponse(response)
        if rate_limit_headers:
            for header_name, header_value in rate_limit_headers.items():
                json_response.headers[header_name] = str(header_value)
        return json_response

    # ------------------------------------------------------------------
    # POST /api/chat/stream
    # ------------------------------------------------------------------

    async def chat_stream(self, request: Request) -> StreamingResponse:
        """Streaming chat completion via SSE."""
        # Parse and validate request body (return 400 JSON, not SSE)
        body, error_response = await _parse_chat_body(
            request,
            request_validator=self.request_validator,
            max_bytes=self.max_request_bytes,
        )
        if error_response is not None:
            return error_response

        model = body.get("model", "")
        messages = body["messages"]
        temperature = body.get("temperature")
        max_tokens = body.get("max_tokens")
        top_p = body.get("top_p")
        stop = body.get("stop")
        system = body.get("system")
        provider = body.get("provider")
        tools = body.get("tools")
        tool_choice = body.get("tool_choice")
        tool_options: dict[str, Any] = {}
        if tools is not None:
            tool_options["tools"] = tools
            tool_options["tool_choice"] = tool_choice
        # Smart-routing flag (Auto mode): empty model + context.smart_routing.
        ctx = body.get("context", {})
        smart_routing = ctx.get("smart_routing", False) if isinstance(ctx, dict) else False
        # Identity for attribution comes from the authenticated context, not the body.
        user_id, project_id, tenant_id = _identity_from_context(request)
        if tenant_id is not None:
            tool_options["tenant_id"] = tenant_id
        if _allow_legacy_project_lookup(request):
            tool_options["allow_legacy_project_lookup"] = True

        # Collect the first response to check for errors / rate limit headers
        # before starting the SSE stream
        rate_limit_headers: dict[str, str] = {}

        # Try to get the stream result
        try:
            if (project := _authorized_project(request)) is not None:
                tool_options["authorized_project"] = project
            result = self.client_agent.chat_stream(
                model, messages, temperature=temperature, max_tokens=max_tokens,
                top_p=top_p, stop=stop, system=system,
                user_id=user_id, project_id=project_id, provider=provider,
                smart_routing=smart_routing,
                **tool_options,
            )
        except Exception as exc:
            logging.getLogger("gateway.chat").error("Stream error: %s\n%s", exc, traceback.format_exc())
            return JSONResponse(
                {"error": {"type": "server_error", "message": "Internal server error"}},
                status_code=500,
            )

        async def event_generator():
            try:
                async for chunk in result:
                    # Extract rate limit headers from metadata chunk
                    if "_rate_limit_headers" in chunk:
                        rate_limit_headers.update(chunk["_rate_limit_headers"])
                        # If this chunk ONLY has rate limit headers, skip it
                        if "error" not in chunk and "done" not in chunk and "content" not in chunk and "id" not in chunk:
                            continue

                    # Error chunk
                    if "error" in chunk:
                        yield f"data: {json.dumps({'error': chunk['error']})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    # Done sentinel
                    if chunk.get("done"):
                        yield "data: [DONE]\n\n"
                        return

                    # Normal content chunk
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as exc:
                logging.getLogger("gateway.chat").error("Stream error: %s\n%s", exc, traceback.format_exc())
                yield f"data: {json.dumps({'error': {'type': 'server_error', 'message': 'Internal server error'}})}\n\n"
                yield "data: [DONE]\n\n"

        streaming_response = StreamingResponse(event_generator(), media_type="text/event-stream")
        # Rate limit headers will be set on the response if available
        # For streaming, we set them eagerly from any pre-stream error response
        if rate_limit_headers:
            for header_name, header_value in rate_limit_headers.items():
                streaming_response.headers[header_name] = str(header_value)
        return streaming_response

    # ------------------------------------------------------------------
    # GET /chat
    # ------------------------------------------------------------------

    async def chat_page(self, request: Request) -> HTMLResponse:
        """Serve the chat UI SPA."""
        index_path = _STATIC_DIR / "index.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def playground_page(self, request: Request) -> HTMLResponse:
        """Serve the customer-facing playground SPA."""
        index_path = _STATIC_DIR / "playground.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def routing_page(self, request: Request) -> HTMLResponse:
        """Serve the routing explorer SPA."""
        index_path = _STATIC_DIR / "routing.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)


# ------------------------------------------------------------------
# Validation helper
# ------------------------------------------------------------------


def _resolve_request_validator(client_agent: ClientAgent) -> RequestValidator:
    gateway_agent = getattr(client_agent, "gateway_agent", None)
    validator = getattr(gateway_agent, "request_validator", None)
    if isinstance(validator, RequestValidator):
        return validator
    return RequestValidator()


def _body_error(error: JSONBodyError) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": "invalid_request", "message": error.message}},
        status_code=error.status_code,
    )


async def _parse_chat_body(
    request: Request,
    *,
    request_validator: RequestValidator | None = None,
    max_bytes: int = DEFAULT_CHAT_REQUEST_MAX_BYTES,
) -> tuple[dict[str, Any], JSONResponse | None]:
    """Parse and validate the chat request body.

    Returns (body_dict, None) on success, or (empty_dict, error_response) on failure.
    """
    try:
        body = await read_json_object(request, max_bytes=max_bytes)
    except JSONBodyError as exc:
        return {}, _body_error(exc)

    # Allow empty model when smart_routing context is present (auto-select mode)
    context = body.get("context", {})
    smart_routing = (
        isinstance(context, dict) and context.get("smart_routing") is True
    )
    validator = request_validator or RequestValidator()
    errors = validator.validate_payload(
        body,
        allow_empty_model=smart_routing,
        check_model=False,
    )
    if errors:
        return {}, JSONResponse(
            {
                "error": {
                    "type": "invalid_request",
                    "message": errors[0].message,
                }
            },
            status_code=400,
        )

    return body, None


# ------------------------------------------------------------------
# Route factory
# ------------------------------------------------------------------


def create_chat_routes(chat_api: ChatAPI) -> list[Route]:
    """Return Starlette Route objects for the chat API."""
    return [
        Route("/api/models", chat_api.list_models, methods=["GET"]),
        Route("/api/users", chat_api.list_users, methods=["GET"]),
        Route("/api/chat", chat_api.chat, methods=["POST"]),
        Route("/api/chat/stream", chat_api.chat_stream, methods=["POST"]),
        Route("/chat", chat_api.chat_page, methods=["GET"]),
        Route("/playground", chat_api.playground_page, methods=["GET"]),
        Route("/routing", chat_api.routing_page, methods=["GET"]),
        Route(
            "/chat/static/{path:path}",
            chat_static_asset,
            methods=["GET"],
        ),
    ]
