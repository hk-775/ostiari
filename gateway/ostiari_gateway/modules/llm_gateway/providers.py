"""LLM providers — wraps AxonLLM's router and provider adapters.

Instead of reimplementing multi-provider routing, we import AxonLLM's
battle-tested gateway engine and run it in-process. This gives us:
- 6 provider adapters (Bedrock, Anthropic, OpenAI, Azure, Vertex, Cohere)
- 5 routing strategies (round-robin, weighted, least-latency, cost-optimized, smart)
- Ensemble routing (scatter-gather-synthesize)
- Provider health tracking with circuit breaking
- Cost tracking and budget enforcement
"""

import asyncio
import json
import logging
from typing import Any

from ostiari_gateway.modules.llm_gateway.models import LLMCredentials

log = logging.getLogger("ostiari.sidecar.llm")


class ToolCall:
    """Represents a tool call returned by an LLM."""

    def __init__(self, id: str, name: str, arguments: dict[str, Any]) -> None:
        self.id = id
        self.name = name
        self.arguments = arguments

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


class LLMResponse:
    """Unified response from any LLM provider."""

    def __init__(
        self,
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        tokens_used: int = 0,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        # Real input/output split when the provider reports it; else fall back to
        # a half/half estimate of tokens_used so callers always have both.
        self.input_tokens = input_tokens or (tokens_used - tokens_used // 2)
        self.output_tokens = output_tokens or (tokens_used // 2)
        self.tokens_used = tokens_used or (self.input_tokens + self.output_tokens)
        self.model = model

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMProvider:
    """Wraps AxonLLM's routing engine for in-process LLM calls.

    Uses AxonLLM's Router, provider adapters, health tracking,
    and cost tracking — all running in the same process as the sidecar.
    """

    def __init__(self, credentials: LLMCredentials) -> None:
        self._credentials = credentials
        self._router: Any = None
        self._cost_tracker: Any = None
        self._health_tracker: Any = None
        self._initialized = False

    def update_credentials(self, credentials: LLMCredentials) -> None:
        self._credentials = credentials
        self._initialized = False
        self._router = None

    def _ensure_initialized(self) -> None:
        """Lazy-initialize AxonLLM components on first call."""
        if self._initialized:
            return

        try:
            from src.gateway.config import DEFAULT_CONFIG
            from src.gateway.cost_tracker import CostTracker
            from src.gateway.health_tracker import ProviderHealthTracker
            from src.gateway.router import Router

            self._health_tracker = ProviderHealthTracker()
            self._cost_tracker = CostTracker(
                pricing_config=DEFAULT_CONFIG.get("pricing", {})
            )

            # Build provider configurations from credentials
            provider_configs = self._build_provider_configs()

            self._router = Router(
                provider_fn=self._make_provider_fn(provider_configs),
                health_tracker=self._health_tracker,
                cost_tracker=self._cost_tracker,
            )
            self._initialized = True
            log.info("AxonLLM engine initialized with %d providers", len(provider_configs))

        except ImportError as e:
            log.warning("AxonLLM not available, falling back to direct provider calls: %s", e)
            self._initialized = True

    def _build_provider_configs(self) -> dict[str, dict[str, Any]]:
        """Build provider configs from credentials."""
        configs: dict[str, dict[str, Any]] = {}
        if self._credentials.anthropic:
            configs["anthropic"] = {"api_key": self._credentials.anthropic}
        if self._credentials.openai:
            configs["openai"] = {"api_key": self._credentials.openai}
        if self._credentials.azure_api_key:
            configs["azure"] = {
                "endpoint": self._credentials.azure_endpoint,
                "api_key": self._credentials.azure_api_key,
                "api_version": self._credentials.azure_api_version,
            }
        if self._credentials.bedrock_region:
            configs["bedrock"] = {"region": self._credentials.bedrock_region}
        if self._credentials.cohere_api_key:
            configs["cohere"] = {"api_key": self._credentials.cohere_api_key}
        if self._credentials.vertex_project:
            configs["vertex"] = {
                "project": self._credentials.vertex_project,
                "location": self._credentials.vertex_location,
            }
        return configs

    def _make_provider_fn(self, configs: dict[str, dict[str, Any]]) -> Any:
        """Create a provider function for the Router."""
        try:
            from src.gateway.multi_provider_factory import MultiProviderFactory

            factory = MultiProviderFactory(configs)
            return factory.get_provider_fn()
        except (ImportError, Exception) as e:
            log.debug("MultiProviderFactory not available: %s", e)
            return self._fallback_provider_fn

    async def _fallback_provider_fn(self, model: str, request: Any) -> Any:
        """Fallback if AxonLLM factory isn't available."""
        raise RuntimeError(f"No provider configured for model: {model}")

    def call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Call an LLM using AxonLLM's routing engine, with direct-call fallback."""
        self._ensure_initialized()

        if self._router is not None:
            return self._call_via_axon(model, messages, tools, max_tokens, temperature)
        return self._call_direct(model, messages, tools, max_tokens, temperature)

    def _call_via_axon(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        """Route through AxonLLM's engine."""
        try:
            from src.gateway.models import ChatCompletionRequest

            request = ChatCompletionRequest(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=self._convert_tools_to_openai_format(tools) if tools else None,
            )

            # AxonLLM's router is async. _call_via_axon is sync and may be called
            # from within a running event loop (the /invoke path), where we can't
            # await. Run the coroutine to completion in a worker thread and BLOCK
            # on its result — previously this used loop.run_in_executor and never
            # awaited the returned Future, so `response` was the Future itself and
            # the result was silently dropped (empty response). See B4.
            try:
                asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    response = pool.submit(
                        lambda: asyncio.run(self._router.route(request))
                    ).result()
            except RuntimeError:
                # No running loop — safe to run directly.
                response = asyncio.run(self._router.route(request))

            return self._convert_axon_response(response, model)

        except Exception as e:
            log.warning("AxonLLM routing failed, falling back to direct call: %s", e)
            return self._call_direct(model, messages, tools, max_tokens, temperature)

    def _call_direct(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        """Direct provider calls as fallback when AxonLLM router isn't available."""
        provider = self._detect_provider(model)

        if provider == "anthropic":
            return self._call_anthropic(model, messages, tools, max_tokens, temperature)
        elif provider == "openai":
            return self._call_openai(model, messages, tools, max_tokens, temperature)
        elif provider == "azure":
            return self._call_azure(model, messages, tools, max_tokens, temperature)
        elif provider == "bedrock":
            return self._call_bedrock(model, messages, tools, max_tokens, temperature)
        elif provider == "cohere":
            return self._call_cohere(model, messages, tools, max_tokens, temperature)
        elif provider == "vertex":
            return self._call_vertex(model, messages, tools, max_tokens, temperature)
        else:
            raise ValueError(f"Unknown model provider for: {model}")

    def _detect_provider(self, model: str) -> str:
        if model.startswith("bedrock/"):
            return "bedrock"
        if model.startswith("azure/"):
            return "azure"
        if model.startswith("vertex/"):
            return "vertex"
        if "claude" in model or "anthropic" in model:
            return "anthropic"
        if "gpt" in model or "o1" in model or "o3" in model or "openai" in model:
            return "openai"
        if "command" in model or "cohere" in model:
            return "cohere"
        return "anthropic"

    def _convert_axon_response(self, response: Any, model: str) -> LLMResponse:
        """Convert AxonLLM ChatCompletionResponse to our LLMResponse."""
        content = ""
        tool_calls = []

        if hasattr(response, "choices") and response.choices:
            choice = response.choices[0]
            msg = choice.get("message", {}) if isinstance(choice, dict) else getattr(choice, "message", {})
            if isinstance(msg, dict):
                content = msg.get("content", "") or ""
                for tc in msg.get("tool_calls", []):
                    if isinstance(tc, dict):
                        func = tc.get("function", {})
                        args = func.get("arguments", "{}")
                        if isinstance(args, str):
                            args = json.loads(args)
                        tool_calls.append(ToolCall(
                            id=tc.get("id", ""),
                            name=func.get("name", ""),
                            arguments=args,
                        ))

        tokens = 0
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            if isinstance(usage, dict):
                tokens = usage.get("total_tokens", 0)

        return LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            tokens_used=tokens,
            model=model,
        )

    def _convert_tools_to_openai_format(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    # ─── Direct provider fallbacks (same as before) ───────────────────────

    def _call_anthropic(self, model, messages, tools, max_tokens, temperature) -> LLMResponse:
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("anthropic package required: pip install anthropic") from e

        client = anthropic.Anthropic(api_key=self._credentials.anthropic)

        system = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                api_messages.append(msg)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            # Anthropic requires tool names match ^[a-zA-Z0-9_-]{1,128}$ — replace dots
            name_map = {}
            anthropic_tools = []
            for t in tools:
                safe_name = t["name"].replace(".", "_")
                name_map[safe_name] = t["name"]
                anthropic_tools.append({
                    "name": safe_name,
                    "description": t.get("description", ""),
                    "input_schema": t.get("schema", {"type": "object", "properties": {}}),
                })
            kwargs["tools"] = anthropic_tools
        else:
            name_map = {}

        response = client.messages.create(**kwargs)

        tool_calls = []
        content = ""
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                original_name = name_map.get(block.name, block.name)
                tool_calls.append(ToolCall(id=block.id, name=original_name, arguments=block.input))

        in_tok = response.usage.input_tokens or 0
        out_tok = response.usage.output_tokens or 0
        return LLMResponse(content=content or None, tool_calls=tool_calls, model=model,
                           input_tokens=in_tok, output_tokens=out_tok)

    def _call_openai(self, model, messages, tools, max_tokens, temperature) -> LLMResponse:
        try:
            import openai
        except ImportError as e:
            raise ImportError("openai package required: pip install openai") from e

        client = openai.OpenAI(api_key=self._credentials.openai)

        kwargs: dict[str, Any] = {
            "model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        # OpenAI requires tool names match ^[a-zA-Z0-9_-]+$ — replace dots
        name_map: dict[str, str] = {}
        if tools:
            openai_tools = []
            for t in tools:
                safe_name = t["name"].replace(".", "_")
                name_map[safe_name] = t["name"]
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": safe_name,
                        "description": t.get("description", ""),
                        "parameters": t.get("schema", {"type": "object", "properties": {}}),
                    },
                })
            kwargs["tools"] = openai_tools

        response = client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                original_name = name_map.get(tc.function.name, tc.function.name)
                tool_calls.append(ToolCall(
                    id=tc.id, name=original_name,
                    arguments=json.loads(tc.function.arguments),
                ))

        in_tok = getattr(response.usage, "prompt_tokens", 0) or 0 if response.usage else 0
        out_tok = getattr(response.usage, "completion_tokens", 0) or 0 if response.usage else 0
        return LLMResponse(content=msg.content, tool_calls=tool_calls, model=model,
                           input_tokens=in_tok, output_tokens=out_tok)

    def _call_bedrock(self, model, messages, tools, max_tokens, temperature) -> LLMResponse:
        try:
            import boto3
        except ImportError as e:
            raise ImportError("boto3 required: pip install boto3") from e

        client = boto3.client("bedrock-runtime", region_name=self._credentials.bedrock_region)
        model_id = model.removeprefix("bedrock/")

        bedrock_messages = []
        system_prompts = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompts.append({"text": msg["content"]})
            else:
                bedrock_messages.append({
                    "role": msg["role"],
                    "content": [{"text": msg.get("content", "")}],
                })

        kwargs: dict[str, Any] = {
            "modelId": model_id, "messages": bedrock_messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system_prompts:
            kwargs["system"] = system_prompts

        response = client.converse(**kwargs)

        content = ""
        tool_calls = []
        for block in response.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                content += block["text"]
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(ToolCall(id=tu["toolUseId"], name=tu["name"], arguments=tu["input"]))

        usage = response.get("usage", {})
        return LLMResponse(content=content or None, tool_calls=tool_calls, model=model,
                           input_tokens=usage.get("inputTokens", 0),
                           output_tokens=usage.get("outputTokens", 0))

    def _call_azure(self, model, messages, tools, max_tokens, temperature) -> LLMResponse:
        """Call Azure OpenAI — same format as OpenAI but different endpoint."""
        try:
            import openai
        except ImportError as e:
            raise ImportError("openai package required: pip install openai") from e

        client = openai.AzureOpenAI(
            azure_endpoint=self._credentials.azure_endpoint,
            api_key=self._credentials.azure_api_key,
            api_version=self._credentials.azure_api_version,
        )
        deployment = model.removeprefix("azure/")

        kwargs: dict[str, Any] = {
            "model": deployment, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = self._convert_tools_to_openai_format(tools)

        response = client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id, name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
                ))

        in_tok = getattr(response.usage, "prompt_tokens", 0) or 0 if response.usage else 0
        out_tok = getattr(response.usage, "completion_tokens", 0) or 0 if response.usage else 0
        return LLMResponse(content=msg.content, tool_calls=tool_calls, model=model,
                           input_tokens=in_tok, output_tokens=out_tok)

    def _call_cohere(self, model, messages, tools, max_tokens, temperature) -> LLMResponse:
        """Call Cohere's Command model."""
        try:
            import cohere
        except ImportError as e:
            raise ImportError("cohere package required: pip install cohere") from e

        client = cohere.ClientV2(api_key=self._credentials.cohere_api_key)
        model_name = model.removeprefix("cohere/")

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": m["role"], "content": m.get("content", "")} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("schema", {})}}
                for t in tools
            ]

        response = client.chat(**kwargs)

        content = response.message.content[0].text if response.message.content else ""
        tool_calls = []
        if response.message.tool_calls:
            for tc in response.message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id, name=tc.function.name,
                    arguments=json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments,
                ))

        in_tok = (response.usage.input_tokens or 0) if response.usage else 0
        out_tok = (response.usage.output_tokens or 0) if response.usage else 0
        return LLMResponse(content=content, tool_calls=tool_calls, model=model,
                           input_tokens=in_tok, output_tokens=out_tok)

    def _call_vertex(self, model, messages, tools, max_tokens, temperature) -> LLMResponse:
        """Call Google Vertex AI (Gemini models)."""
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ImportError("google-generativeai required: pip install google-generativeai") from e

        model_name = model.removeprefix("vertex/")
        genai_model = genai.GenerativeModel(model_name)

        # Convert messages to Gemini format
        history = []
        latest_msg = ""
        for msg in messages:
            if msg["role"] == "user":
                latest_msg = msg.get("content", "")
            elif msg["role"] == "assistant":
                history.append({"role": "model", "parts": [msg.get("content", "")]})
            elif msg["role"] == "system":
                history.insert(0, {"role": "user", "parts": [msg.get("content", "")]})
                history.insert(1, {"role": "model", "parts": ["Understood."]})

        chat = genai_model.start_chat(history=history)
        response = chat.send_message(latest_msg, generation_config={"max_output_tokens": max_tokens, "temperature": temperature})

        content = response.text if response.text else ""
        tokens = response.usage_metadata.total_token_count if hasattr(response, "usage_metadata") else 0
        return LLMResponse(content=content, tool_calls=[], tokens_used=tokens, model=model)
