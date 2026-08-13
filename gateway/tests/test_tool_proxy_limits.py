"""Bounded response and deadline behavior for the HTTP tool proxy."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from ostiari_gateway.models import ToolDefinition
from ostiari_gateway.tool_proxy import ToolProxy


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self):
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk


async def _proxy_with_response(response_factory) -> ToolProxy:
    async def handler(request: httpx.Request) -> httpx.Response:
        return response_factory(request)

    proxy = ToolProxy()
    await proxy._client.aclose()
    proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return proxy


@pytest.mark.asyncio
async def test_tool_response_is_bounded(monkeypatch):
    monkeypatch.setenv("OSTIARI_MAX_TOOL_RESPONSE_BYTES", "1024")
    proxy = await _proxy_with_response(
        lambda request: httpx.Response(
            200,
            stream=_Stream([b"a" * 700, b"b" * 700]),
            request=request,
        )
    )
    proxy.register(ToolDefinition(name="large", endpoint="https://tool.example/large"))
    try:
        result = await proxy.execute("large", {})
    finally:
        await proxy.close()

    assert result["status_code"] == 502
    assert "1024 byte limit" in result["error"]

@pytest.mark.asyncio
async def test_tool_uses_absolute_deadline():
    proxy = await _proxy_with_response(
        lambda request: httpx.Response(
            200,
            stream=_Stream([b"x"] * 20, delay=0.01),
            request=request,
        )
    )
    proxy.register(
        ToolDefinition(
            name="drip",
            endpoint="https://tool.example/drip",
            timeout_seconds=0.035,
        )
    )
    try:
        result = await proxy.execute("drip", {})
    finally:
        await proxy.close()

    assert result["status_code"] == 504
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_paid_adapter_response_is_still_size_checked(monkeypatch):
    monkeypatch.setenv("OSTIARI_MAX_TOOL_RESPONSE_BYTES", "1024")
    proxy = ToolProxy()
    proxy.register(ToolDefinition(name="paid", endpoint="https://tool.example/paid"))

    class PaidClient:
        async def request(self, **kwargs):
            request = httpx.Request("POST", kwargs["url"])
            return httpx.Response(200, content=b"x" * 2048, request=request)

    try:
        result = await proxy.execute(
            "paid",
            {},
            payment_client=PaidClient(),
            payment_quote=object(),
        )
    finally:
        await proxy.close()

    assert result["status_code"] == 502
    assert "1024 byte limit" in result["error"]
