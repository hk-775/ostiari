"""ClientAgent — thin translation layer between HTTP-oriented dicts and GatewayAgent."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class ClientAgent:
    """Translates between the Chat API's HTTP format and GatewayAgent's internal API."""

    def __init__(
        self,
        gateway_agent: Any,
        default_user_id: str = "chat-user",
        default_project_id: str = "chat-project",
    ) -> None:
        self.gateway_agent = gateway_agent
        self.default_user_id = default_user_id
        self.default_project_id = default_project_id

    async def list_models(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        authorized_project: Any | None = None,
        tenant_id: str | None = None,
        allow_legacy_project_lookup: bool = False,
    ) -> list[dict]:
        """Return available models filtered by access context.

        Passes project_id and user_id through to GatewayAgent.handle_list_models
        so that only models the caller is permitted to use are returned.
        """
        pid, resolved_tenant_id = self._resolve_scope(
            project_id=project_id,
            tenant_id=tenant_id,
            authorized_project=authorized_project,
        )
        uid = user_id or self.default_user_id
        kwargs: dict[str, Any] = {"project_id": pid, "user_id": uid}
        if resolved_tenant_id is not None:
            kwargs["tenant_id"] = resolved_tenant_id
        if authorized_project is not None:
            kwargs["authorized_project"] = authorized_project
        if allow_legacy_project_lookup:
            kwargs["allow_legacy_project_lookup"] = True
        result = await self.gateway_agent.handle_list_models(**kwargs)
        return result.get("models", [])

    async def embeddings(
        self,
        model: str,
        input_value: str | list[str],
        *,
        encoding_format: str = "float",
        dimensions: int | None = None,
        provider: str | None = None,
        user: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        authorized_project: Any | None = None,
        tenant_id: str | None = None,
        allow_legacy_project_lookup: bool = False,
    ) -> dict:
        """Create embeddings through the governed gateway data plane."""
        request_data: dict[str, Any] = {
            "model": model,
            "input": input_value,
            "encoding_format": encoding_format,
        }
        if dimensions is not None:
            request_data["dimensions"] = dimensions
        if user is not None:
            request_data["user"] = user
        context = self._build_context(
            user_id=user_id,
            project_id=project_id,
            provider=provider,
            authorized_project=authorized_project,
            tenant_id=tenant_id,
            allow_legacy_project_lookup=allow_legacy_project_lookup,
        )
        return await self.gateway_agent.handle_embeddings(
            request_data,
            context,
        )

    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        provider: str | None = None,
        smart_routing: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        system: str | None = None,
        authorized_project: Any | None = None,
        tenant_id: str | None = None,
        allow_legacy_project_lookup: bool = False,
    ) -> dict:
        """Non-streaming chat completion. Returns simplified response dict."""
        request_data = self._build_request_data(
            model, messages, stream=False,
            temperature=temperature, max_tokens=max_tokens,
            top_p=top_p, stop=stop, system=system,
            tools=tools, tool_choice=tool_choice,
        )
        context = self._build_context(
            user_id=user_id,
            project_id=project_id,
            provider=provider,
            smart_routing=smart_routing,
            authorized_project=authorized_project,
            tenant_id=tenant_id,
            allow_legacy_project_lookup=allow_legacy_project_lookup,
        )
        response = await self.gateway_agent.handle_chat_completion(request_data, context)

        # Extract rate limit headers to pass through
        rate_limit_headers = response.pop("_rate_limit_headers", None)

        if "error" in response:
            result: dict[str, Any] = {
                "error": response["error"],
                "status_code": response.get("status_code", 500),
            }
            if rate_limit_headers:
                result["_rate_limit_headers"] = rate_limit_headers
            return result

        choice = response["choices"][0]
        message = choice.get("message", {})
        result = {
            "id": response["id"],
            "model": response["model"],
            "provider": response["provider"],
            "content": message.get("content"),
            "usage": response["usage"],
        }
        # A tool call carries no text, so summarizing the response as `content`
        # alone loses it entirely — the caller gets a fluent 200 and never learns
        # a tool was requested. finish_reason travels with it because that is
        # what a tool loop branches on; defaulting it to "stop" downstream ends
        # the loop before the tool ever runs.
        if message.get("tool_calls"):
            result["tool_calls"] = message["tool_calls"]
        if choice.get("finish_reason") is not None:
            result["finish_reason"] = choice["finish_reason"]
        # Include smart_routing metadata if present
        if "smart_routing" in response:
            result["smart_routing"] = response["smart_routing"]
        # Whether the answer came from the cache, and which cache. This dict is
        # a whitelist rebuild, so a key not named here is dropped — and both
        # routes above build their responses from this result, not from the
        # pipeline's. Unreachable until the cache started being written to.
        if response.get("is_cached"):
            result["is_cached"] = True
            if "cache_type" in response:
                result["cache_type"] = response["cache_type"]
        # Include ensemble metadata if present
        if "ensemble" in response:
            result["ensemble"] = response["ensemble"]
        # Include backward-compat note when ensemble was requested but unavailable
        if "ensemble_unavailable" in response:
            result["ensemble_unavailable"] = response["ensemble_unavailable"]
        if rate_limit_headers:
            result["_rate_limit_headers"] = rate_limit_headers
        return result

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        provider: str | None = None,
        smart_routing: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        system: str | None = None,
        authorized_project: Any | None = None,
        tenant_id: str | None = None,
        allow_legacy_project_lookup: bool = False,
    ) -> AsyncIterator[dict]:
        """Streaming chat completion. Yields chunk dicts.

        Tools are forwarded on the request. Native SSE routes can relay text
        incrementally; buffered policy/fallback paths emit a normalized,
        complete ``tool_calls`` delta rather than partial argument fragments.
        """
        request_data = self._build_request_data(
            model, messages, stream=True,
            temperature=temperature, max_tokens=max_tokens,
            top_p=top_p, stop=stop, system=system,
            tools=tools, tool_choice=tool_choice,
        )
        context = self._build_context(
            user_id=user_id, project_id=project_id, provider=provider,
            smart_routing=smart_routing,
            authorized_project=authorized_project,
            tenant_id=tenant_id,
            allow_legacy_project_lookup=allow_legacy_project_lookup,
        )
        result = await self.gateway_agent.handle_chat_completion(request_data, context)

        # If the gateway returned an error dict directly (e.g. rate limit)
        if isinstance(result, dict) and "error" in result:
            chunk: dict[str, Any] = {
                "error": result["error"],
                "status_code": result.get("status_code", 500),
            }
            # Pass through rate limit headers
            if "_rate_limit_headers" in result:
                chunk["_rate_limit_headers"] = result["_rate_limit_headers"]
            yield chunk
            return

        # result is an async iterator of chunks
        async for chunk in result:
            # Pass through rate limit headers metadata chunk
            if "_rate_limit_headers" in chunk:
                yield {"_rate_limit_headers": chunk["_rate_limit_headers"]}
                continue

            data = chunk.get("data")
            if data is None:
                continue

            # "[DONE]" sentinel
            if data == "[DONE]":
                yield {"done": True}
                return

            # Error during streaming
            if isinstance(data, dict) and "error" in data:
                yield {"error": data["error"]}
                continue

            # Normal content chunk
            content = ""
            delta: dict = {}
            finish_reason = None
            choices = data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {}) or {}
                # `.get("content", "")` returns None on a tool-call delta, where
                # OpenAI sends content: null — the default never applies.
                content = delta.get("content") or ""
                finish_reason = choices[0].get("finish_reason")

            out: dict[str, Any] = {
                "id": data.get("id", ""),
                "model": data.get("model", ""),
                "provider": data.get("provider", ""),
                "content": content,
                "is_final": data.get("is_final", False),
            }
            # A streamed tool call arrives as delta.tool_calls with empty
            # content, so a chunk summarized by `content` alone is
            # indistinguishable from silence — the caller sees an empty stream
            # and no tool.
            if delta.get("tool_calls"):
                out["tool_calls"] = delta["tool_calls"]
            if finish_reason is not None:
                out["finish_reason"] = finish_reason

            yield out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_request_data(
        self,
        model: str,
        messages: list[dict],
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        system: str | None = None,
    ) -> dict:
        request_data: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if temperature is not None:
            request_data["temperature"] = temperature
        if max_tokens is not None:
            request_data["max_tokens"] = max_tokens
        if top_p is not None:
            request_data["top_p"] = top_p
        if stop is not None:
            request_data["stop"] = stop
        if system is not None:
            request_data["system"] = system
        # Truthiness, not `is not None`: an empty tools list is not the same
        # request as no tools, and some providers reject `tools: []` outright.
        if tools:
            request_data["tools"] = tools
            if tool_choice is not None:
                request_data["tool_choice"] = tool_choice
        return request_data

    def _build_context(
        self,
        user_id: str | None = None,
        project_id: str | None = None,
        provider: str | None = None,
        smart_routing: bool = False,
        authorized_project: Any | None = None,
        tenant_id: str | None = None,
        allow_legacy_project_lookup: bool = False,
    ) -> dict:
        resolved_project_id, resolved_tenant_id = self._resolve_scope(
            project_id=project_id,
            tenant_id=tenant_id,
            authorized_project=authorized_project,
        )
        ctx: dict[str, Any] = {
            "user_id": user_id or self.default_user_id,
            "project_id": resolved_project_id,
        }
        if provider:
            ctx["provider"] = provider
        if smart_routing:
            ctx["smart_routing"] = True
        if resolved_tenant_id is not None:
            ctx["tenant_id"] = resolved_tenant_id
        if authorized_project is not None:
            ctx["authorized_project"] = authorized_project
        if allow_legacy_project_lookup:
            if authorized_project is not None:
                raise ValueError(
                    "legacy project lookup cannot accompany an authorized project"
                )
            ctx["allow_legacy_project_lookup"] = True
        return ctx

    def _resolve_scope(
        self,
        *,
        project_id: str | None,
        tenant_id: str | None,
        authorized_project: Any | None,
    ) -> tuple[str, str | None]:
        """Resolve tenant/project hints without weakening canonical authority."""
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or not tenant_id.strip()
        ):
            raise ValueError("tenant_id must be None or a non-empty string")

        if authorized_project is None:
            return project_id or self.default_project_id, tenant_id

        authoritative_project_id = getattr(
            authorized_project,
            "project_id",
            None,
        )
        authoritative_tenant_id = getattr(
            authorized_project,
            "tenant_id",
            None,
        )
        if (
            not isinstance(authoritative_project_id, str)
            or not authoritative_project_id.strip()
            or not isinstance(authoritative_tenant_id, str)
            or not authoritative_tenant_id.strip()
        ):
            raise ValueError(
                "authorized_project must have non-empty project_id and tenant_id"
            )
        if project_id and project_id != authoritative_project_id:
            raise ValueError(
                "project_id does not match the authorized project"
            )
        if tenant_id is not None and tenant_id != authoritative_tenant_id:
            raise ValueError(
                "tenant_id does not match the authorized project"
            )
        return authoritative_project_id, authoritative_tenant_id

    async def get_available_users(self) -> list[str]:
        """Return known user IDs, covering the fleet rather than this task only.

        ``cost_tracker._records`` holds what *this* process served plus whatever
        was hydrated at startup, so on a multi-instance deployment the user
        selector listed a different set depending on which task answered:

            store holds: ['alice', 'bob']
            GET /api/users on task A: ['alice', 'chat-user']
            GET /api/users on task B: ['bob', 'chat-user']

        Async now because the refresh is: the same rate-limited fleet sync the
        admin aggregates use, so a dashboard and the chat selector cannot
        disagree about who exists. Falls back to the local records when
        persistence is off or the scan fails, which is the answer this returned
        before any of this existed.
        """
        synced = getattr(self.gateway_agent.cost_tracker, "synced_records", None)
        if synced is not None:
            try:
                await synced()
            except Exception:
                logger.warning(
                    "Fleet-wide user list refresh failed; listing this instance's users",
                    exc_info=True,
                )
        records = self.gateway_agent.cost_tracker._records
        user_ids = sorted({r.user_id for r in records})
        # Also include users from user_configs that may not have records yet
        for uid in self.gateway_agent._user_configs:
            if uid not in user_ids:
                user_ids.append(uid)
        # Always include the default user
        if self.default_user_id not in user_ids:
            user_ids.append(self.default_user_id)
        return sorted(set(user_ids))
