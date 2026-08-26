"""MCP Server management API."""

import json
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org, require_role
from control_plane.database import get_db
from control_plane.models.database import Gateway, McpServer
from control_plane.models.schemas import McpServerCreate, McpServerResponse
from control_plane.models.scoping import get_gateway, get_scoped, scoped, stamp
from control_plane.routers.providers import _decrypt, _encrypt
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.push_service import gateway_config_headers

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


def _encrypt_config(config: dict) -> str:
    """Encrypt the complete MCP configuration document at rest."""
    if not config:
        return ""
    return _encrypt(
        json.dumps(config, sort_keys=True, separators=(",", ":"))
    )


def _validate_remote_url(url: str) -> None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="Remote mode requires an absolute HTTP(S) URL",
        )
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail="Remote MCP URL must not contain credentials",
        )
    if parsed.query or parsed.fragment:
        raise HTTPException(
            status_code=400,
            detail=(
                "Remote MCP URL must not contain a query or fragment; "
                "use private config for credentials"
            ),
        )


def decrypt_mcp_config(record: McpServer) -> dict:
    """Return runtime MCP config, preferring the encrypted representation."""
    if record.config_encrypted:
        value = json.loads(_decrypt(record.config_encrypted))
        if not isinstance(value, dict):
            raise ValueError("Decrypted MCP config must be a JSON object")
        return value
    # Compatibility for a row created before the encryption migration. The
    # migration clears this field after encrypting it.
    return record.config if isinstance(record.config, dict) else {}


def _to_response(record: McpServer) -> McpServerResponse:
    """Build a secret-free API response."""
    return McpServerResponse(
        id=record.id,
        name=record.name,
        mode=record.mode,
        package=record.package,
        module=record.module,
        url=record.url,
        command=record.command,
        config={},
        has_config=bool(record.config_encrypted or record.config),
        allowed_tools=record.allowed_tools,
        blocked_tools=record.blocked_tools,
        prefix=record.prefix,
        gateway_id=record.gateway_id,
        created_at=record.created_at,
    )


@router.get("", response_model=list[McpServerResponse])
async def list_mcp_servers(gateway_id: str | None = None, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    query = scoped(select(McpServer), McpServer, org)
    if gateway_id:
        query = query.where(McpServer.gateway_id == gateway_id)
    result = await db.execute(query)
    return [_to_response(record) for record in result.scalars().all()]


@router.post("/{gateway_id}", response_model=McpServerResponse)
async def add_mcp_server(
    gateway_id: str,
    body: McpServerCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
    _admin=Depends(require_role("admin")),
):
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    if body.mode not in ("embedded", "remote", "stdio"):
        raise HTTPException(status_code=400, detail="Mode must be: embedded, remote, or stdio")

    if body.mode == "embedded" and not body.package and not body.module:
        raise HTTPException(status_code=400, detail="Embedded mode requires 'package' or 'module'")
    if body.mode == "remote" and not body.url:
        raise HTTPException(status_code=400, detail="Remote mode requires 'url'")
    if body.mode == "remote":
        _validate_remote_url(body.url)
    if body.mode == "stdio" and not body.command:
        raise HTTPException(status_code=400, detail="Stdio mode requires 'command'")

    mcp = McpServer(
        name=body.name,
        mode=body.mode,
        package=body.package,
        module=body.module,
        url=body.url,
        command=body.command,
        config={},
        config_encrypted=_encrypt_config(body.config),
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
                     "url": body.url, "package": body.package,
                     "command_executable": body.command[0] if body.command else "",
                     "command_arg_count": max(0, len(body.command) - 1),
                     "allowed_tools": body.allowed_tools, "blocked_tools": body.blocked_tools},
                    org=gateway.org_id or org)
    await db.commit()
    await db.refresh(mcp)
    return _to_response(mcp)


@router.get("/{mcp_id}", response_model=McpServerResponse)
async def get_mcp_server(mcp_id: int, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    mcp = await get_scoped(db, McpServer, mcp_id, org)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return _to_response(mcp)


@router.delete("/{mcp_id}")
async def delete_mcp_server(
    mcp_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
    _admin=Depends(require_role("admin")),
):
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
async def discover_tools(
    mcp_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
    _admin=Depends(require_role("admin")),
):
    """Ask the gateway to discover/refresh tools from this MCP server."""
    import httpx

    mcp = await get_scoped(db, McpServer, mcp_id, org)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")

    gateway = await get_gateway(db, mcp.gateway_id, mcp.org_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    async with httpx.AsyncClient(
        timeout=10.0, headers=gateway_config_headers()
    ) as client:
        try:
            resp = await client.post(
                f"{gateway.endpoint}/config/mcp-servers/{mcp.name}/refresh",
            )
            if resp.status_code == 200:
                # A refresh can change the tool surface an agent can call, so it
                # belongs in the trail even though no CP row changed.
                await audit.log(db, actor_of(request), "discover", "mcp_server", str(mcp_id),
                                {"name": mcp.name, "gateway_id": mcp.gateway_id}, org=org)
                await db.commit()
                return resp.json()
            return {"error": f"Gateway returned {resp.status_code}", "detail": resp.text[:200]}
        except httpx.ConnectError:
            raise HTTPException(
                status_code=502,
                detail=f"Cannot reach gateway at {gateway.endpoint}",
            ) from None


@router.get("/{mcp_id}/tools")
async def get_discovered_tools(mcp_id: int, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Get the list of tools currently discovered from this MCP server on the gateway."""
    import httpx

    mcp = await get_scoped(db, McpServer, mcp_id, org)
    if not mcp:
        raise HTTPException(status_code=404, detail="MCP server not found")

    gateway = await get_gateway(db, mcp.gateway_id, mcp.org_id)
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
            raise HTTPException(
                status_code=502,
                detail=f"Cannot reach gateway at {gateway.endpoint}",
            ) from None
