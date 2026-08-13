"""Tool proxy — executes tools by forwarding HTTP requests to remote endpoints."""

import asyncio
import logging
import time
from typing import Any

import httpx

from ostiari.bounded_http import (
    ResponseTooLargeError,
    decode_json_or_text,
    max_response_bytes,
    read_buffered_limited,
    request_limited,
)
from ostiari_gateway.models import ToolDefinition

log = logging.getLogger("ostiari.sidecar")


class ToolProxy:
    """Manages tool definitions and proxies calls to their endpoints."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._client = httpx.AsyncClient()

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        log.info("Registered tool: %s → %s %s", tool.name, tool.method, tool.endpoint)

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            log.info("Unregistered tool: %s", name)
            return True
        return False

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "endpoint": t.endpoint,
                "method": t.method,
                "description": t.description,
                "timeout_seconds": t.timeout_seconds,
                # The registered parameter schema. Omitting it left the agentic
                # loop advertising every tool with empty properties, so the model
                # could never supply arguments.
                "schema": t.schema_,
            }
            for t in self._tools.values()
        ]

    def clear(self) -> None:
        self._tools.clear()

    @staticmethod
    def _place_params(
        tool: ToolDefinition, params: dict[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """Distribute a flat params dict into (url, query, body).

        Path params are substituted into the ``{var}`` placeholders in the
        endpoint template; query params go to the query string; everything else
        is the JSON body. When the tool declares neither path nor query params
        (hand-registered tools), all params become the body — unchanged behavior.
        """
        if not tool.path_params and not tool.query_params:
            return tool.endpoint, {}, params

        params = dict(params or {})
        url = tool.endpoint
        for name in tool.path_params:
            if name in params:
                # URL-encode the path segment value.
                from urllib.parse import quote

                url = url.replace("{" + name + "}", quote(str(params.pop(name)), safe=""))

        query = {name: params.pop(name) for name in tool.query_params if name in params}
        body = params if params else None
        return url, query, body

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        propagate_headers: dict[str, str] | None = None,
        payment_client: Any = None,
        payment_quote: Any = None,
    ) -> dict[str, Any]:
        """Proxy a tool call to its remote endpoint.

        Args:
            name: Tool name to execute.
            params: Parameters to send as JSON body.
            propagate_headers: Extra headers to forward (e.g., traceparent for OTel).
                              Passed through regardless of whether the tool supports them.
            payment_client: Optional live x402 client used for a paid retry.
            payment_quote: The previously approved x402 challenge.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {
                "error": f"Unknown tool: {name}",
                "available": [t.name for t in self._tools.values()],
                "status_code": 404,
            }

        headers = {"Content-Type": "application/json", **tool.headers}
        if propagate_headers:
            headers.update(propagate_headers)

        # Split the flat params dict across URL path, query string, and body
        # according to the tool's REST param placement. Tools with no
        # path/query params (the default) send everything as a JSON body,
        # exactly as before.
        url, query, body = self._place_params(tool, params)

        start = time.monotonic()
        response_limit = max_response_bytes("OSTIARI_MAX_TOOL_RESPONSE_BYTES")
        timeout = max(0.1, tool.timeout_seconds)

        try:
            if payment_client is None:
                bounded = await request_limited(
                    self._client,
                    method=tool.method,
                    url=url,
                    params=query or None,
                    json=body,
                    headers=headers,
                    deadline_seconds=timeout,
                    max_bytes=response_limit,
                )
                status_code = bounded.status_code
                response_headers = bounded.headers
                content = bounded.content
            else:
                async with asyncio.timeout(timeout):
                    response = await payment_client.request(
                        quote=payment_quote,
                        method=tool.method,
                        url=url,
                        params=query or None,
                        json_body=body,
                        headers=headers,
                        timeout=timeout,
                    )
                status_code = response.status_code
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                content = read_buffered_limited(response, response_limit)
            duration_ms = (time.monotonic() - start) * 1000

            response_body = decode_json_or_text(content)

            payment_headers = {
                name.lower(): value
                for name in (
                    "PAYMENT-REQUIRED",
                    "X-PAYMENT-REQUIRED",
                    "PAYMENT-RESPONSE",
                    "X-PAYMENT-RESPONSE",
                )
                if (value := response_headers.get(name.lower()))
            }
            return {
                "result": response_body,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
                "payment_headers": payment_headers,
            }
        except (TimeoutError, httpx.TimeoutException):
            duration_ms = (time.monotonic() - start) * 1000
            return {
                "error": f"Tool {name} timed out after {timeout}s",
                "status_code": 504,
                "duration_ms": round(duration_ms, 2),
            }
        except ResponseTooLargeError:
            duration_ms = (time.monotonic() - start) * 1000
            return {
                "error": f"Tool {name} response exceeds {response_limit} byte limit",
                "status_code": 502,
                "duration_ms": round(duration_ms, 2),
            }
        except httpx.ConnectError as e:
            duration_ms = (time.monotonic() - start) * 1000
            return {
                "error": f"Cannot reach tool endpoint: {e}",
                "status_code": 502,
                "duration_ms": round(duration_ms, 2),
            }
        except Exception as e:
            if payment_client is None:
                raise
            duration_ms = (time.monotonic() - start) * 1000
            log.warning("Live x402 request failed for %s: %s", name, e)
            return {
                "error": f"x402 payment failed: {e}",
                "status_code": 502,
                "duration_ms": round(duration_ms, 2),
                "payment_headers": {},
            }

    async def close(self) -> None:
        await self._client.aclose()
