"""Gateway proxy — forwards requests from the UI to gateways."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.database import get_db
from control_plane.models.database import Gateway

router = APIRouter(prefix="/api/proxy", tags=["proxy"])


@router.post("/gateway/{gateway_id}/{path:path}")
async def proxy_to_gateway(gateway_id: str, path: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Proxy a request from the UI to a gateway.

    Used by the Sandbox and other UI features that need to call gateways
    without CORS issues (browser → control plane → gateway).
    """
    gateway = await db.get(Gateway, gateway_id)
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
async def proxy_get_to_gateway(gateway_id: str, path: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Proxy GET requests to a gateway."""
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail=f"Gateway '{gateway_id}' not found")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{gateway.endpoint}/{path}")
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot reach gateway at {gateway.endpoint}")
