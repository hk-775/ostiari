"""MCP Server management API."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway, McpServer
from control_plane.models.schemas import McpServerCreate, McpServerResponse
from control_plane.models.scoping import get_scoped, scoped, stamp
from control_plane.services.audit_service import actor_of, audit

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


@router.get("", response_model=list[McpServerResponse])
async def list_mcp_servers(gateway_id: str | None = None, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    query = scoped(select(McpServer), McpServer, org)
    if gateway_id:
        query = query.where(McpServer.gateway_id == gateway_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/{gateway_id}", response_model=McpServerResponse)
async def add_mcp_server(gateway_id: str, body: McpServerCreate, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    if body.mode not in ("embedded", "remote", "stdio"):
        raise HTTPException(status_code=400, detail="Mode must be: embedded, remote, or stdio")

    if body.mode == "embedded" and not body.package and not body.module:
        raise HTTPException(status_code=400, detail="Embedded mode requires 'package' or 'module'")
    if body.mode == "remote" and not body.url:
        raise HTTPException(status_code=400, detail="Remote mode requires 'url'")
    if body.mode == "stdio" and not body.command:
        raise HTTPException(status_code=400, detail="Stdio mode requires 'command'")

    mcp = McpServer(
        name=body.name,
        mode=body.mode,
        package=body.package,
        module=body.module,
        url=body.url,
        command=body.command,
        config=body.config,
        allowed_tools=body.allowed_tools,
        blocked_tools=body.blocked_tools,
        prefix=body.prefix or body.name,
        gateway_id=gateway_id,
    )
    stamp(mcp, gateway.org_id)
    db.add(mcp)
    await db.flush()
    # An MCP server is a whole toolset arriving from outside — the allow/block
    # lists here decide what an agent can reach, so record who set them.
    await audit.log(db, actor_of(request), "create", "mcp_server", str(mcp.id),
                    {"name": body.name, "mode": body.mode, "gateway_id": gateway_id,
                     "url": body.url, "package": body.package, "command": body.command,
                     "allowed_tools": body.allowed_tools, "blocked_tools": body.blocked_tools},
                    org=gateway.org_id or org)
    await db.commit()
    await db.refresh(mcp)
    return mcp


@router.get("/{mcp_id}", response_model=McpServerResponse)
async def get_mcp_server(mcp_id: int, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    mcp = await get_scoped(db, McpServer, mcp_id, org)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp


@router.delete("/{mcp_id}")
async def delete_mcp_server(mcp_id: int, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    mcp = await get_scoped(db, McpServer, mcp_id, org)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")
    # Capture before the delete — the response also reads mcp.name.
    name, gw = mcp.name, mcp.gateway_id
    await db.delete(mcp)
    await audit.log(db, actor_of(request), "delete", "mcp_server", str(mcp_id),
                    {"name": name, "gateway_id": gw}, org=org)
    await db.commit()
    return {"deleted": mcp_id, "name": name}


@router.post("/{mcp_id}/discover")
async def discover_tools(mcp_id: int, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Ask the gateway to discover/refresh tools from this MCP server."""
    import httpx

    mcp = await get_scoped(db, McpServer, mcp_id, org)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")

    gateway = await db.get(Gateway, mcp.gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{gateway.endpoint}/config/mcp-servers/{mcp.name}/refresh")
            if resp.status_code == 200:
                # A refresh can change the tool surface an agent can call, so it
                # belongs in the trail even though no CP row changed.
                await audit.log(db, actor_of(request), "discover", "mcp_server", str(mcp_id),
                                {"name": mcp.name, "gateway_id": mcp.gateway_id}, org=org)
                await db.commit()
                return resp.json()
            return {"error": f"Gateway returned {resp.status_code}", "detail": resp.text[:200]}
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot reach gateway at {gateway.endpoint}")


@router.get("/{mcp_id}/tools")
async def get_discovered_tools(mcp_id: int, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Get the list of tools currently discovered from this MCP server on the gateway."""
    import httpx

    mcp = await get_scoped(db, McpServer, mcp_id, org)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")

    gateway = await db.get(Gateway, mcp.gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{gateway.endpoint}/tools")
            if resp.status_code == 200:
                data = resp.json()
                mcp_tools = data.get("mcp_tools", [])
                prefix = mcp.prefix or mcp.name
                server_tools = [t for t in mcp_tools if t.get("server") == mcp.name or t.get("name", "").startswith(f"{prefix}.")]
                return {"server": mcp.name, "tools": server_tools, "total": len(server_tools)}
            return {"server": mcp.name, "tools": [], "total": 0}
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot reach gateway at {gateway.endpoint}")
