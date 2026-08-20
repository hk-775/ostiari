"""Bounded JSON request-body handling for chat ingress."""

from __future__ import annotations

import json
import math
from typing import Any

from starlette.requests import ClientDisconnect, Request

DEFAULT_CHAT_REQUEST_MAX_BYTES = 1024 * 1024


class JSONBodyError(ValueError):
    """A client-visible request body error with a stable HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _content_length(request: Request) -> int | None:
    values = request.headers.getlist("content-length")
    if not values:
        return None
    if len(values) != 1 or request.headers.get("transfer-encoding"):
        raise JSONBodyError(400, "Invalid Content-Length header")

    raw_value = values[0].strip()
    if not raw_value or not raw_value.isascii() or not raw_value.isdecimal():
        raise JSONBodyError(400, "Invalid Content-Length header")
    try:
        return int(raw_value)
    except ValueError as exc:
        raise JSONBodyError(400, "Invalid Content-Length header") from exc


def _validate_content_headers(request: Request) -> None:
    content_type = request.headers.get("content-type")
    if content_type:
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            raise JSONBodyError(400, "Content-Type must be application/json")

    content_encoding = request.headers.get("content-encoding")
    if content_encoding and content_encoding.strip().lower() != "identity":
        raise JSONBodyError(400, "Compressed request bodies are not supported")


def _reject_nonstandard_number(value: str) -> None:
    raise ValueError(f"Non-standard JSON number: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON number is outside the finite range: {value}")
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON object key: {key}")
        value[key] = item
    return value


async def read_json_object(
    request: Request,
    *,
    max_bytes: int = DEFAULT_CHAT_REQUEST_MAX_BYTES,
) -> dict[str, Any]:
    """Read one bounded JSON object without trusting ``Content-Length`` alone."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    _validate_content_headers(request)
    declared_length = _content_length(request)
    if declared_length is not None and declared_length > max_bytes:
        raise JSONBodyError(
            413,
            f"Request body exceeds the {max_bytes}-byte limit",
        )

    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > max_bytes:
                raise JSONBodyError(
                    413,
                    f"Request body exceeds the {max_bytes}-byte limit",
                )
            body.extend(chunk)
    except ClientDisconnect as exc:
        raise JSONBodyError(400, "Request body was not received completely") from exc

    if declared_length is not None and len(body) != declared_length:
        raise JSONBodyError(400, "Content-Length does not match request body")

    try:
        decoded = bytes(body).decode("utf-8-sig")
        value = json.loads(
            decoded,
            parse_constant=_reject_nonstandard_number,
            parse_float=_parse_finite_float,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise JSONBodyError(400, "Invalid JSON in request body") from exc

    if not isinstance(value, dict):
        raise JSONBodyError(400, "Request body must be a JSON object")
    return value
