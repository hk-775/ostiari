"""Absolute-deadline and response-size tests for outbound HTTP helpers."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ostiari.bounded_http import (
    ResponseTooLargeError,
    max_response_bytes,
    request_limited,
    timeout_seconds,
)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self):
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk


@pytest.mark.asyncio
async def test_response_is_bounded_while_streaming():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_Stream([b"a" * 700, b"b" * 700]),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ResponseTooLargeError):
            await request_limited(
                client,
                "GET",
                "https://downstream.example/data",
                deadline_seconds=1,
                max_bytes=1024,
            )


@pytest.mark.asyncio
async def test_declared_oversize_is_rejected_before_stream_read():
    read = False

    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal read
            read = True
            yield b"x"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "4096"},
            stream=NeverRead(),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ResponseTooLargeError):
            await request_limited(
                client,
                "GET",
                "https://downstream.example/data",
                deadline_seconds=1,
                max_bytes=1024,
            )
    assert read is False


@pytest.mark.asyncio
async def test_drip_feed_cannot_extend_absolute_deadline():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_Stream([b"x"] * 20, delay=0.01),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await request_limited(
                client,
                "GET",
                "https://downstream.example/data",
                deadline_seconds=0.035,
                max_bytes=1024,
            )


def test_environment_values_are_server_bounded(monkeypatch):
    monkeypatch.setenv("LIMIT", "999999999")
    monkeypatch.setenv("TIMEOUT", "999999999")
    assert max_response_bytes("LIMIT") == 16 * 1024 * 1024
    assert timeout_seconds("TIMEOUT") == 120.0
