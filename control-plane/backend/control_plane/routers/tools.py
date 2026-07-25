"""Tool management API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway, Tool
from control_plane.models.scoping import get_scoped, scoped, stamp
from control_plane.models.schemas import ToolCreate, ToolResponse

router = APIRouter(prefix="/api/tools", tags=["tools"])


class OpenAPIImport(BaseModel):
    """Request to import tools from an OpenAPI spec into a gateway."""

    source: str | None = None          # URL, JSON, or YAML text
    spec: dict | None = None           # or an inline spec object
    server_url: str | None = None      # override base URL
    name_prefix: str = ""              # namespace the generated tool names
    replace: bool = False              # replace all this gateway's tools vs. merge
    preview: bool = False              # generate + return without persisting


@router.get("", response_model=list[ToolResponse])
async def list_tools(gateway_id: str | None = None, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    query = scoped(select(Tool), Tool, org)
    if gateway_id:
        query = query.where(Tool.gateway_id == gateway_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/{gateway_id}", response_model=ToolResponse)
async def add_tool(gateway_id: str, body: ToolCreate, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    tool = Tool(
        name=body.name, endpoint=body.endpoint, method=body.method,
        description=body.description, timeout_seconds=body.timeout_seconds,
        schema_json=body.schema_json, gateway_id=gateway_id,
    )
    stamp(tool, org)
    db.add(tool)
    await db.commit()
    await db.refresh(tool)
    return tool


@router.delete("/{tool_id}")
async def delete_tool(tool_id: int, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    tool = await get_scoped(db, Tool, tool_id, org)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    await db.delete(tool)
    await db.commit()
    return {"deleted": tool_id}


@router.post("/{gateway_id}/import-openapi")
async def import_openapi(gateway_id: str, body: OpenAPIImport, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Generate tools from an OpenAPI spec and persist them to a gateway.

    Parsing is done by the shared gateway importer (single source of truth).
    With preview=true, the generated tools are returned without persisting.
    With replace=true, the gateway's existing tools are deleted first.
    """
    # Uses the shared root-package parser — the control plane must not depend on
    # the gateway package (they are separately deployed services).
    from ostiari.openapi_import import OpenAPIError, fetch_spec_text, is_url, parse_spec

    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    source = body.spec if body.spec is not None else body.source
    if source is None:
        raise HTTPException(status_code=400, detail="provide 'source' (url/json/yaml) or 'spec' (object)")

    if isinstance(source, str) and is_url(source):
        try:
            source = fetch_spec_text(source)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not fetch spec: {e}") from e

    try:
        specs = parse_spec(source, server_url=body.server_url, name_prefix=body.name_prefix)
    except OpenAPIError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 — parse errors surface as 400
        raise HTTPException(status_code=400, detail=f"could not import spec: {e}") from e

    preview = [
        {"name": s["name"], "method": s["method"], "endpoint": s["endpoint"],
         "description": s["description"], "path_params": s["path_params"],
         "query_params": s["query_params"]}
        for s in specs
    ]
    if body.preview:
        return {"status": "preview", "count": len(preview), "tools": preview}

    if body.replace:
        existing = await db.execute(scoped(select(Tool).where(Tool.gateway_id == gateway_id), Tool, org))
        for t in existing.scalars().all():
            await db.delete(t)

    for s in specs:
        # Upsert by (gateway_id, name) so re-importing updates rather than duplicates.
        found = await db.execute(
            scoped(select(Tool).where(Tool.gateway_id == gateway_id, Tool.name == s["name"]), Tool, org)
        )
        row = found.scalar_one_or_none()
        if row is None:
            row = Tool(name=s["name"], gateway_id=gateway_id)
            stamp(row, org)
            db.add(row)
        row.endpoint = s["endpoint"]
        row.method = s["method"]
        row.description = s["description"]
        row.timeout_seconds = s["timeout_seconds"]
        row.schema_json = s["schema"]
        row.path_params = s["path_params"]
        row.query_params = s["query_params"]

    await db.commit()
    return {"status": "imported", "gateway_id": gateway_id, "count": len(preview), "tools": preview}
