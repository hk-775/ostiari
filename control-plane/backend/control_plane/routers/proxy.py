"""Gateway proxy — forwards requests from the UI to gateways."""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway
from control_plane.models.scoping import get_scoped

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

# Both routes fetch the gateway with `get_scoped`, not `db.get`. This is the
# widest of the broker-pilot scoping holes: an unscoped lookup here let any
# caller who knew a gateway id reach *through* the control plane to that
# gateway's own /config and /tool endpoints — a cross-tenant path to another
# org's runtime, not just to its records. A cross-org id now 404s, which is the
# same answer as a nonexistent one: probing must not distinguish them.


@router.post("/gateway/{gateway_id}/{path:path}")
async def proxy_to_gateway(gateway_id: str, path: str, request: Request,
                           db: AsyncSession = Depends(get_db),
                           org: str = Depends(get_current_org)):
    """Proxy a request from the UI to a gateway.

    Used by the Sandbox and other UI features that need to call gateways
    without CORS issues (browser → control plane → gateway).
    """
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail=f"Gateway '{gateway_id}' not found")

    body = await request.body()
    headers = dict(request.headers)
    # Remove host/content-length (will be set by httpx)
    headers.pop("host", None)
    headers.pop("content-length", None)

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=f"{gateway.endpoint}/{path}",
                content=body,
                headers=headers,
            )
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot reach gateway at {gateway.endpoint}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Gateway request timed out")


@router.get("/gateway/{gateway_id}/{path:path}")
async def proxy_get_to_gateway(gateway_id: str, path: str, request: Request,
                               db: AsyncSession = Depends(get_db),
                               org: str = Depends(get_current_org)):
    """Proxy GET requests to a gateway."""
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail=f"Gateway '{gateway_id}' not found")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{gateway.endpoint}/{path}")
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot reach gateway at {gateway.endpoint}")
