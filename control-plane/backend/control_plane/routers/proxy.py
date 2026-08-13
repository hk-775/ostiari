"""Gateway proxy — bounded, credential-isolated calls from the dashboard."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway
from control_plane.models.scoping import get_scoped
from control_plane.services.gateway_credentials import proxy_headers
from ostiari.bounded_http import (
    ResponseTooLargeError,
    max_response_bytes,
    request_limited,
    timeout_seconds,
)

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

_PASSTHROUGH_RESPONSE_HEADERS = frozenset(
    {"content-type", "retry-after", "x-request-id"}
)


async def _proxy(
    gateway_id: str,
    path: str,
    request: Request,
    db: AsyncSession,
    org: str,
) -> Response:
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail=f"Gateway '{gateway_id}' not found")

    body = await request.body()
    headers = proxy_headers(path, request.headers)
    query = request.url.query
    url = f"{gateway.endpoint.rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"

    deadline = timeout_seconds("OSTIARI_PROXY_TIMEOUT_SECONDS")
    response_limit = max_response_bytes("OSTIARI_PROXY_MAX_RESPONSE_BYTES")
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            downstream = await request_limited(
                client,
                request.method,
                url,
                content=body or None,
                headers=headers,
                deadline_seconds=deadline,
                max_bytes=response_limit,
            )
    except ResponseTooLargeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Gateway response exceeds {response_limit} byte limit",
        ) from exc
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=504, detail="Gateway request timed out") from exc
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach gateway at {gateway.endpoint}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Gateway request failed") from exc

    if downstream.status_code == 401:
        return JSONResponse(
            status_code=502,
            content={
                "detail": "Gateway rejected control-plane credentials",
                "gateway_status": 401,
            },
        )

    response_headers = {
        name: value
        for name, value in downstream.headers.items()
        if name in _PASSTHROUGH_RESPONSE_HEADERS
    }
    return Response(
        content=downstream.content,
        status_code=downstream.status_code,
        headers=response_headers,
    )


@router.post("/gateway/{gateway_id}/{path:path}")
async def proxy_to_gateway(
    gateway_id: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> Response:
    return await _proxy(gateway_id, path, request, db, org)


@router.get("/gateway/{gateway_id}/{path:path}")
async def proxy_get_to_gateway(
    gateway_id: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> Response:
    return await _proxy(gateway_id, path, request, db, org)
