"""LLM Gateway module — registers /invoke and /models endpoints."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ostiari_gateway.modules.llm_gateway.executor import AgenticExecutor
from ostiari_gateway.modules.llm_gateway.messages_proxy import MessagesProxy
from ostiari_gateway.modules.llm_gateway.models import InvokeRequest, LLMConfig

log = logging.getLogger("ostiari.sidecar.llm")


class LLMGatewayModule:
    """Pluggable module that adds LLM routing and agentic execution."""

    def __init__(self) -> None:
        self._executor: AgenticExecutor | None = None
        self._config: LLMConfig = LLMConfig()
        self._messages_proxy: MessagesProxy | None = None

    @property
    def name(self) -> str:
        return "llm_gateway"

    @property
    def description(self) -> str:
        return "LLM routing, fallback chains, and full agentic loop execution"

    def register(self, app: FastAPI, context: dict[str, Any]) -> None:
        """Register LLM Gateway routes on the app."""
        manager = context["manager"]
        mcp_manager = context.get("mcp_manager")
        trace_reporter = context.get("trace_reporter")
        quota_enforcer = context.get("quota_enforcer")
        agent_auth = context.get("agent_auth")
        llm_config = context.get("llm_config", LLMConfig())
        self._config = llm_config
        self._executor = AgenticExecutor(config=llm_config, manager=manager, mcp_manager=mcp_manager, trace_reporter=trace_reporter, quota_enforcer=quota_enforcer, agent_auth=agent_auth)

        # Claude Code shim: intercept the Anthropic /v1/messages API, govern +
        # route across providers, and stream back. Reuses the executor's router,
        # provider, and security so config hot-reloads apply to both paths.
        self._messages_proxy = MessagesProxy(
            config=llm_config,
            provider=self._executor._provider,
            router=self._executor._router,
            security=self._executor._security,
            quota_enforcer=quota_enforcer,
            trace_reporter=trace_reporter,
            agent_auth=agent_auth,
        )

        @app.post("/v1/messages")
        async def messages(request: Request) -> Any:
            """Anthropic Messages API shim — intercept, govern, route, stream."""
            if self._messages_proxy is None:
                return JSONResponse(status_code=503,
                                    content={"type": "error",
                                             "error": {"type": "api_error",
                                                       "message": "LLM Gateway not initialized"}})
            return await self._messages_proxy.handle(request)

        @app.post("/invoke")
        async def invoke(request: Request) -> Any:
            """Run the full agentic loop: LLM → validate → execute tools → respond."""
            if self._executor is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": "LLM Gateway not initialized"},
                )

            body = await request.json()
            agent_id = request.headers.get("X-Agent-Id", "unknown")
            framework = request.headers.get("X-Framework", "unknown")
            session_id = request.headers.get("X-Session-Id", "")
            plan = request.headers.get("X-Plan", "")
            step = request.headers.get("X-Step", "")

            # Agent Auth: check if this agent is allowed to use /invoke
            if agent_auth:
                auth_allowed, auth_reason = agent_auth.check(agent_id, "/invoke")
                if not auth_allowed:
                    return JSONResponse(
                        status_code=403,
                        content={"blocked": True, "reason": auth_reason, "limit_type": "agent_authorization"},
                    )

            req = InvokeRequest(**body)
            req.context.update({
                "agent_id": agent_id, "framework": framework,
                "session_id": session_id, "plan": plan, "step": step,
            })

            try:
                result = await self._executor.invoke(req)
                return result.model_dump()
            except Exception as e:
                log.error("Invoke failed: %s", e)
                return JSONResponse(
                    status_code=500,
                    content={"error": str(e)},
                )

        @app.get("/models")
        async def list_models() -> Any:
            """List available models and current routing config."""
            return {
                "default_model": self._config.default_model,
                "fallback_chain": self._config.fallback_chain,
                "routing_rules": [r.model_dump() for r in self._config.routing_rules],
            }

        @app.get("/cache/stats")
        async def cache_stats() -> Any:
            """Get intent cache statistics."""
            if self._executor:
                return self._executor._intent_cache.get_stats()
            return {"entries": 0, "hits": 0, "misses": 0, "hit_rate": 0}

        @app.post("/cache/clear")
        async def clear_cache() -> Any:
            """Clear the intent cache."""
            if self._executor:
                self._executor._intent_cache.clear()
            return {"status": "cleared"}

        @app.post("/config/llm")
        async def update_llm_config(request: Request) -> Any:
            """Hot-reload LLM configuration."""
            body = await request.json()
            self._config = LLMConfig(**body)
            if self._executor:
                self._executor.update_config(self._config)
            if self._messages_proxy:
                # Keep the shim on the same live config/router/provider/security.
                self._messages_proxy._config = self._config
                self._messages_proxy._router = self._executor._router
                self._messages_proxy._provider = self._executor._provider
                self._messages_proxy._security = self._executor._security
            return {"status": "applied", "default_model": self._config.default_model}

        log.info("LLM Gateway module registered: POST /v1/messages, POST /invoke, "
                 "GET /models, POST /config/llm")

    def shutdown(self) -> None:
        self._executor = None
        self._messages_proxy = None
