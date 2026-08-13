"""Bounded outbound HTTP response helpers.

HTTPX read timeouts are inactivity timeouts: a peer can keep a response alive
indefinitely by sending one byte before each timeout expires. These helpers add
an absolute wall-clock deadline and cap the decompressed bytes retained in
memory.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_CONFIGURED_RESPONSE_BYTES = 16 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 120.0


class ResponseTooLargeError(Exception):
    """Raised before a downstream response can exceed the configured cap."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"downstream response exceeds {limit} byte limit")


@dataclass(frozen=True)
class BoundedResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes


def max_response_bytes(
    env_name: str = "OSTIARI_MAX_OUTBOUND_RESPONSE_BYTES",
    *,
    default: int = _DEFAULT_MAX_RESPONSE_BYTES,
) -> int:
    """Return a positive response cap, constrained to a server-owned maximum."""
    raw = os.environ.get(env_name, "").strip()
    try:
        configured = int(raw) if raw else default
    except ValueError:
        configured = default
    return max(1024, min(configured, _MAX_CONFIGURED_RESPONSE_BYTES))


def timeout_seconds(
    env_name: str = "OSTIARI_OUTBOUND_TIMEOUT_SECONDS",
    *,
    default: float = _DEFAULT_TIMEOUT_SECONDS,
) -> float:
    """Return a bounded absolute timeout in seconds."""
    raw = os.environ.get(env_name, "").strip()
    try:
        configured = float(raw) if raw else default
    except ValueError:
        configured = default
    return max(0.1, min(configured, _MAX_TIMEOUT_SECONDS))


def _declared_length(headers: Any) -> int | None:
    raw = headers.get("content-length") if headers is not None else None
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def read_limited(response: httpx.Response, limit: int) -> bytes:
    """Read a streaming HTTPX response without retaining more than ``limit``."""
    declared = _declared_length(response.headers)
    if declared is not None and declared > limit:
        raise ResponseTooLargeError(limit)

    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > limit:
            raise ResponseTooLargeError(limit)
        content.extend(chunk)
    return bytes(content)


def read_buffered_limited(response: httpx.Response, limit: int) -> bytes:
    """Validate a response already buffered by a third-party HTTP adapter."""
    declared = _declared_length(response.headers)
    if declared is not None and declared > limit:
        raise ResponseTooLargeError(limit)
    content = response.content
    if len(content) > limit:
        raise ResponseTooLargeError(limit)
    return content


async def request_limited(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    deadline_seconds: float,
    max_bytes: int,
    **kwargs: Any,
) -> BoundedResponse:
    """Execute one HTTPX request under an absolute deadline and response cap."""
    timeout = max(0.1, deadline_seconds)
    async with asyncio.timeout(timeout):
        async with client.stream(
            method,
            url,
            timeout=httpx.Timeout(timeout),
            **kwargs,
        ) as response:
            content = await read_limited(response, max_bytes)
            return BoundedResponse(
                status_code=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                content=content,
            )


def decode_json_or_text(content: bytes) -> Any:
    """Decode a bounded response as JSON, falling back to replacement text."""
    if not content:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")
