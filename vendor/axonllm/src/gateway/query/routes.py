"""Canonical HTTP endpoint for bounded, read-only queries."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .service import QueryService, QueryServiceError


_MAX_BODY_BYTES = 64 * 1024
_FIELDS = frozenset(
    {
        "project_id",
        "datasource_id",
        "sql",
        "max_rows",
        "request_id",
    }
)


class _RequestBodyLimitExceeded(Exception):
    """Signal a bounded read failure without masking stream errors."""


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "type": "query_error",
                "code": code,
                "message": message,
            }
        },
    )


async def _read_request_body(request: Request) -> bytes:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > _MAX_BODY_BYTES - len(body):
                raise _RequestBodyLimitExceeded
            body.extend(chunk)
    except _RequestBodyLimitExceeded as exc:
        raise ValueError("query request exceeds 64 KiB") from exc
    except Exception as exc:
        raise ValueError(
            "query request body could not be read"
        ) from exc
    return bytes(body)


async def _request_object(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise ValueError("Content-Length is invalid") from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ValueError("query request exceeds 64 KiB")
    body = await _read_request_body(request)

    def _reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(
                    f"query request contains duplicate field {key!r}"
                )
            value[key] = item
        return value

    def _reject_constant(value: str) -> None:
        raise ValueError(
            f"query request contains non-finite number {value}"
        )

    try:
        value = json.loads(
            body,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("query request must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("query request must be a JSON object")
    unknown = set(value).difference(_FIELDS)
    if unknown:
        raise ValueError(
                "query request contains unsupported fields: "
                + ", ".join(sorted(unknown))
        )
    return value


class QueryAPI:
    """Adapt an authenticated Starlette request to the query service."""

    def __init__(self, service: QueryService) -> None:
        self.service = service

    async def query(self, request: Request) -> JSONResponse:
        principal = getattr(request.state, "principal", None)
        context = getattr(request.state, "context", None)
        tenant_id = getattr(context, "tenant_id", None)
        context_project_id = getattr(context, "project_id", None)
        if (
            principal is None
            or not isinstance(tenant_id, str)
            or not tenant_id
            or not isinstance(context_project_id, str)
            or not context_project_id
        ):
            return _error(
                401,
                "canonical_identity_required",
                "Canonical tenant and project identity is required.",
            )
        try:
            body = await _request_object(request)
        except ValueError as exc:
            return _error(400, "invalid_query_request", str(exc))

        project_id = body.get("project_id", context_project_id)
        if project_id != context_project_id:
            return _error(
                400,
                "project_context_mismatch",
                "project_id must match the authenticated project context.",
            )
        datasource_id = body.get("datasource_id")
        if (
            not isinstance(datasource_id, str)
            or not datasource_id
            or datasource_id != datasource_id.strip()
        ):
            return _error(
                400,
                "invalid_query_request",
                "datasource_id must be a non-empty string.",
            )
        try:
            result = await self.service.execute(
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                datasource_id=datasource_id,
                sql=body.get("sql"),
                max_rows=body.get("max_rows"),
                request_id=body.get("request_id"),
            )
        except QueryServiceError as exc:
            return _error(exc.status_code, exc.code, exc.message)
        return JSONResponse(result)


def create_query_routes(api: QueryAPI) -> list[Route]:
    return [Route("/v1/query", api.query, methods=["POST"])]
