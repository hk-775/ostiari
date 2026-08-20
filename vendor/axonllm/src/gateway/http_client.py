"""HTTP client for communicating with LLM provider APIs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from urllib.parse import urlparse

import aiohttp

from src.gateway.adapters.base import ProviderAdapter
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    ProviderModelMapping,
    StreamChunk,
)
from src.gateway.provider_config import (
    ProviderConfig,
    build_provider_embedding_url,
    build_provider_url,
    build_provider_stream_url,
    get_auth_headers,
)
from src.gateway.router import ProviderError

# Provider-specific headers added automatically.
_PROVIDER_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {"anthropic-version": "2023-06-01"},
}
_MAX_PENDING_STREAM_METADATA = 32
_MAX_PROVIDER_BODY_BYTES = 10 * 1024 * 1024
_MAX_PROVIDER_STREAM_BYTES = 32 * 1024 * 1024
_MAX_PROVIDER_STREAM_LINE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


async def _content_chunks(content: object) -> AsyncIterator[bytes]:
    iterator = getattr(content, "iter_chunked", None)
    source = iterator(_READ_CHUNK_BYTES) if callable(iterator) else content
    async for chunk in source:  # type: ignore[union-attr]
        if not isinstance(chunk, bytes):
            raise TypeError("provider response chunk is not bytes")
        yield chunk


async def _read_bounded_body(
    content: object,
    *,
    provider: str,
) -> bytes:
    body = bytearray()
    async for chunk in _content_chunks(content):
        if len(body) + len(chunk) > _MAX_PROVIDER_BODY_BYTES:
            raise ProviderError(
                status_code=502,
                provider=provider,
                message="Provider response exceeded the maximum size",
            )
        body.extend(chunk)
    return bytes(body)


async def _bounded_stream_lines(
    content: object,
    *,
    provider: str,
) -> AsyncIterator[bytes]:
    pending = bytearray()
    total = 0
    async for chunk in _content_chunks(content):
        total += len(chunk)
        if total > _MAX_PROVIDER_STREAM_BYTES:
            raise ProviderError(
                status_code=502,
                provider=provider,
                message="Provider stream exceeded the maximum size",
            )
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                if len(pending) > _MAX_PROVIDER_STREAM_LINE_BYTES:
                    raise ProviderError(
                        status_code=502,
                        provider=provider,
                        message="Provider stream event exceeded the maximum size",
                    )
                break
            if newline > _MAX_PROVIDER_STREAM_LINE_BYTES:
                raise ProviderError(
                    status_code=502,
                    provider=provider,
                    message="Provider stream event exceeded the maximum size",
                )
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            yield line.removesuffix(b"\r")
    if pending:
        if len(pending) > _MAX_PROVIDER_STREAM_LINE_BYTES:
            raise ProviderError(
                status_code=502,
                provider=provider,
                message="Provider stream event exceeded the maximum size",
            )
        yield bytes(pending).removesuffix(b"\r")


def _transport_payload(payload: dict) -> tuple[dict, list[str]]:
    """Remove adapter-only metadata before serializing a provider request."""
    warnings = payload.get("_warnings", [])
    safe_warnings = (
        [str(warning) for warning in warnings]
        if isinstance(warnings, list)
        else []
    )
    return (
        {
            key: value
            for key, value in payload.items()
            if not str(key).startswith("_")
        },
        safe_warnings,
    )


def _stream_chunk_has_signal(chunk: StreamChunk) -> bool:
    """Return whether a chunk proves the provider produced a stream result."""
    if chunk.is_final:
        return True
    for choice in chunk.choices:
        if choice.get("finish_reason") is not None:
            return True
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            return True
        if delta.get("tool_calls") or delta.get("function_call"):
            return True
    return False


def _stream_chunk_has_metadata(chunk: StreamChunk) -> bool:
    return bool(
        chunk.id
        or chunk.model
        or chunk.usage is not None
        or chunk.choices
    )


def _stream_chunk_is_terminal(chunk: StreamChunk) -> bool:
    return chunk.is_final or any(
        choice.get("finish_reason") is not None
        for choice in chunk.choices
    )


def _is_stream_error_event(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type") or event.get("event_type") or "")
    if event_type in {"error", "response.failed"}:
        return True
    error = event.get("error")
    return isinstance(error, (dict, str)) and bool(error)


class HttpClient:
    """Async HTTP client that sends requests to LLM provider endpoints.

    Maintains lazy sessions keyed by transport identity: endpoint authority,
    proxy/TLS identity, timeout policy, and pool limits. Credentials are applied
    per request, so two keys for the same endpoint share connections safely.
    """

    def __init__(self) -> None:
        self._sessions: dict[object, aiohttp.ClientSession] = {}
        self._retired_sessions: list[aiohttp.ClientSession] = []

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @staticmethod
    def transport_key(config: ProviderConfig) -> tuple:
        """Return the connection-pool identity for a concrete route."""
        parsed = urlparse(config.base_url)
        authority = (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            parsed.port,
        )
        return (
            authority,
            config.extra_params.get("proxy_url", ""),
            config.extra_params.get("tls_identity", ""),
            config.connect_timeout,
            config.read_timeout,
            config.max_connections,
            config.max_connections_per_host,
            config.keepalive_timeout,
        )

    def _get_or_create_session(self, config: ProviderConfig) -> aiohttp.ClientSession:
        """Return an existing session for the route transport, or create one."""
        key = self.transport_key(config)
        session = self._sessions.get(key)
        # Keep compatibility with callers/tests that injected a provider-keyed
        # session before pools became transport-keyed.
        if session is None:
            session = self._sessions.get(config.provider_name)
        if session is None or session.closed:
            timeout = aiohttp.ClientTimeout(
                connect=config.connect_timeout,
                sock_read=config.read_timeout,
                total=config.connect_timeout + config.read_timeout,
            )
            connector = aiohttp.TCPConnector(
                limit=config.max_connections,
                limit_per_host=config.max_connections_per_host,
                keepalive_timeout=config.keepalive_timeout,
                ttl_dns_cache=300,
            )
            session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
            self._sessions[key] = session
        return session

    def retain_configs(self, configs: list[ProviderConfig]) -> None:
        """Retire pools absent from the catalog without interrupting requests.

        Catalog replacement can happen while a response is streaming. Stale
        sessions are removed from future selection immediately but remain open
        until factory shutdown so their in-flight requests can finish.
        """
        active = {self.transport_key(config) for config in configs}
        stale = [
            self._sessions.pop(key)
            for key in list(self._sessions)
            if key not in active
        ]
        for session in stale:
            if not any(retired is session for retired in self._retired_sessions):
                self._retired_sessions.append(session)

    async def close(self) -> None:
        """Close all open provider sessions."""
        for session in self._sessions.values():
            if not session.closed:
                await session.close()
        for session in self._retired_sessions:
            if not session.closed:
                await session.close()
        self._sessions.clear()
        self._retired_sessions.clear()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Non-streaming execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        request: ChatCompletionRequest,
        mapping: ProviderModelMapping,
        adapter: ProviderAdapter,
        config: ProviderConfig,
        prompt_caching_enabled: bool = False,
    ) -> ChatCompletionResponse:
        """Send a non-streaming request and return the translated response.

        Steps:
        1. Translate the unified request via the adapter.
        2. Build auth + provider-specific headers.
        3. POST JSON to the provider endpoint.
        4. On 2xx – parse JSON and translate through the adapter.
        5. On non-2xx – raise ``ProviderError`` with the matching status code.
        6. On network error – raise ``ProviderError(502)``.
        """
        # 1. Translate request
        provider_request = replace(request, model=mapping.model_id)
        adapter.validate_request(provider_request)
        translated = await adapter.translate_request(
            provider_request,
            prompt_caching_enabled=prompt_caching_enabled,
        )
        payload, adapter_warnings = _transport_payload(translated)

        # Override model with the actual provider model ID (not the gateway model name)
        if "model" in payload:
            payload["model"] = mapping.model_id

        # Always use non-streaming for the execute path (streaming uses execute_streaming)
        payload.pop("stream", None)

        # 2. Build URL
        url = build_provider_url(config, mapping)

        # 3. Assemble headers
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_auth_headers(config))
        headers.update(_PROVIDER_HEADERS.get(config.provider_name, {}))
        if prompt_caching_enabled and config.provider_name == "anthropic":
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        headers.update(config.extra_headers)

        # 4. Send request
        session = self._get_or_create_session(config)
        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=config.extra_params.get("proxy_url") or None,
            ) as resp:
                raw_body = await _read_bounded_body(
                    resp.content,
                    provider=mapping.provider,
                )
                if 200 <= resp.status < 300:
                    try:
                        body = json.loads(raw_body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ProviderError(
                            status_code=502,
                            provider=mapping.provider,
                            message="Provider returned malformed JSON",
                        ) from exc
                    if not isinstance(body, dict):
                        raise ProviderError(
                            status_code=502,
                            provider=mapping.provider,
                            message="Provider returned a non-object response",
                        )
                    response = adapter.translate_response(body)
                    response.warnings.extend(adapter_warnings)
                    return response
                raise ProviderError(
                    status_code=resp.status,
                    provider=mapping.provider,
                    message=(
                        f"Provider request failed with status {resp.status}"
                    ),
                )
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderError(
                status_code=504,
                provider=mapping.provider,
                message="Provider request timed out",
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(
                status_code=502,
                provider=mapping.provider,
                message="Provider network request failed",
            ) from exc

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    async def execute_embeddings(
        self,
        request: EmbeddingRequest,
        mapping: ProviderModelMapping,
        adapter: ProviderAdapter,
        config: ProviderConfig,
    ) -> EmbeddingResponse:
        """Send a routed embeddings request through a supported HTTP adapter."""
        if not adapter.supports_embeddings:
            raise ProviderError(
                status_code=501,
                provider=mapping.provider,
                message=(
                    f"Embeddings are not supported for provider "
                    f"'{mapping.provider}'"
                ),
                retryable=False,
                provider_unavailable=False,
            )

        provider_request = replace(request, model=mapping.model_id)
        try:
            payload = await adapter.translate_embedding_request(
                provider_request
            )
        except NotImplementedError as exc:
            raise ProviderError(
                status_code=501,
                provider=mapping.provider,
                message=(
                    f"Embeddings are not supported for provider "
                    f"'{mapping.provider}'"
                ),
                retryable=False,
                provider_unavailable=False,
            ) from exc
        if "model" in payload:
            payload["model"] = mapping.model_id

        url = build_provider_embedding_url(config, mapping)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_auth_headers(config))
        headers.update(config.extra_headers)

        session = self._get_or_create_session(config)
        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=config.extra_params.get("proxy_url") or None,
            ) as resp:
                raw_body = await _read_bounded_body(
                    resp.content,
                    provider=mapping.provider,
                )
                if 200 <= resp.status < 300:
                    try:
                        body = json.loads(raw_body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ProviderError(
                            status_code=502,
                            provider=mapping.provider,
                            message="Provider returned malformed JSON",
                        ) from exc
                    if not isinstance(body, dict):
                        raise ProviderError(
                            status_code=502,
                            provider=mapping.provider,
                            message="Provider returned a non-object response",
                        )
                    try:
                        return adapter.translate_embedding_response(body)
                    except (TypeError, ValueError) as exc:
                        raise ProviderError(
                            status_code=502,
                            provider=mapping.provider,
                            message=(
                                "Provider returned a malformed embeddings "
                                "response"
                            ),
                        ) from exc
                raise ProviderError(
                    status_code=resp.status,
                    provider=mapping.provider,
                    message=(
                        "Provider embeddings request failed with status "
                        f"{resp.status}"
                    ),
                )
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderError(
                status_code=504,
                provider=mapping.provider,
                message="Provider embeddings request timed out",
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(
                status_code=502,
                provider=mapping.provider,
                message="Provider embeddings network request failed",
            ) from exc

    # ------------------------------------------------------------------
    # Streaming (SSE) execution
    # ------------------------------------------------------------------

    async def execute_streaming(
        self,
        request: ChatCompletionRequest,
        mapping: ProviderModelMapping,
        adapter: ProviderAdapter,
        config: ProviderConfig,
        prompt_caching_enabled: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        """Send a streaming request and yield translated ``StreamChunk`` objects.

        Steps:
        1. Translate the unified request via the adapter.
        2. Build auth + provider-specific headers.
        3. POST to the provider endpoint and read the response as an SSE stream.
        4. On non-2xx before streaming begins – raise ``ProviderError``.
        5. Parse ``data:`` lines, skip ``[DONE]``, translate chunks via adapter.
        6. Yield each ``StreamChunk``; stop on ``is_final=True``.
        7. On network error during streaming – raise ``ProviderError(502)``.
        """
        # 1. Translate request
        stream_request = replace(
            request,
            model=mapping.model_id,
            stream=True,
        )
        adapter.validate_request(stream_request)
        translated = await adapter.translate_request(
            stream_request,
            prompt_caching_enabled=prompt_caching_enabled,
        )
        payload, _ = _transport_payload(translated)

        # Override model with the actual provider model ID
        if "model" in payload:
            payload["model"] = mapping.model_id

        # 2. Build URL (streaming variant)
        url = build_provider_stream_url(config, mapping)

        # 3. Assemble headers
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(get_auth_headers(config))
        headers.update(_PROVIDER_HEADERS.get(config.provider_name, {}))
        if prompt_caching_enabled and config.provider_name == "anthropic":
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        headers.update(config.extra_headers)

        # 4. Send request and stream response
        session = self._get_or_create_session(config)
        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=config.extra_params.get("proxy_url") or None,
            ) as resp:
                # Pre-flight check: non-2xx before streaming begins
                if not (200 <= resp.status < 300):
                    await _read_bounded_body(
                        resp.content,
                        provider=mapping.provider,
                    )
                    raise ProviderError(
                        status_code=resp.status,
                        provider=mapping.provider,
                        message=(
                            "Provider streaming request failed with status "
                            f"{resp.status}"
                        ),
                    )

                # Hold metadata-only events until an output/final event arrives.
                # This lets a clean HTTP 200 with only pings/headers fail before
                # the route is treated as having produced its first byte.
                pending: list[StreamChunk] = []
                saw_signal = False
                saw_terminal = False
                translate_stream_chunk = adapter.stream_translator()

                # Read SSE or newline-delimited JSON events from the response.
                async for raw_line in _bounded_stream_lines(
                    resp.content,
                    provider=mapping.provider,
                ):
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

                    if line.startswith("data:"):
                        data = line[len("data:"):].lstrip(" ")
                    elif line.lstrip().startswith(("{", "[")):
                        # Cohere v1 streams one JSON object per line rather than
                        # using the SSE data prefix.
                        data = line.lstrip()
                    else:
                        continue

                    if data == "[DONE]":
                        saw_terminal = True
                        break

                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            status_code=502,
                            provider=mapping.provider,
                            message="Provider returned malformed streaming JSON",
                        ) from exc

                    if _is_stream_error_event(parsed):
                        raise ProviderError(
                            status_code=502,
                            provider=mapping.provider,
                            message="Provider reported a streaming error",
                        )

                    chunk = translate_stream_chunk(parsed)
                    if _stream_chunk_is_terminal(chunk):
                        saw_terminal = True
                    if _stream_chunk_has_signal(chunk):
                        saw_signal = True
                        for metadata in pending:
                            yield metadata
                        pending.clear()
                        yield chunk
                    elif _stream_chunk_has_metadata(chunk):
                        if saw_signal:
                            yield chunk
                        elif len(pending) < _MAX_PENDING_STREAM_METADATA:
                            pending.append(chunk)

                    # NB: do NOT stop on chunk.is_final. Providers that report
                    # usage in-stream (OpenAI stream_options.include_usage) send
                    # the usage chunk AFTER the finish_reason chunk, so returning
                    # on is_final would drop it. Read until the SSE [DONE]
                    # sentinel (above) or the connection closes.
                if not saw_signal:
                    raise ProviderError(
                        status_code=502,
                        provider=mapping.provider,
                        message="Provider returned an empty streaming response",
                    )
                if not saw_terminal:
                    raise ProviderError(
                        status_code=502,
                        provider=mapping.provider,
                        message=(
                            "Provider streaming response ended without a "
                            "terminal event"
                        ),
                    )
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderError(
                status_code=504,
                provider=mapping.provider,
                message="Provider streaming request timed out",
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderError(
                status_code=502,
                provider=mapping.provider,
                message="Provider streaming network request failed",
            ) from exc
