"""Tool management API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.database import get_db
from control_plane.models.database import Gateway, Tool
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
async def list_tools(gateway_id: str | None = None, db: AsyncSession = Depends(get_db)):
    query = select(Tool)
    if gateway_id:
        query = query.where(Tool.gateway_id == gateway_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/{gateway_id}", response_model=ToolResponse)
async def add_tool(gateway_id: str, body: ToolCreate, db: AsyncSession = Depends(get_db)):
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    tool = Tool(
        name=body.name, endpoint=body.endpoint, method=body.method,
        description=body.description, timeout_seconds=body.timeout_seconds,
        schema_json=body.schema_json, gateway_id=gateway_id,
    )
    db.add(tool)
    await db.commit()
    await db.refresh(tool)
    return tool


@router.delete("/{tool_id}")
async def delete_tool(tool_id: int, db: AsyncSession = Depends(get_db)):
    tool = await db.get(Tool, tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    await db.delete(tool)
    await db.commit()
    return {"deleted": tool_id}


@router.post("/{gateway_id}/import-openapi")
async def import_openapi(gateway_id: str, body: OpenAPIImport, db: AsyncSession = Depends(get_db)):
    """Generate tools from an OpenAPI spec and persist them to a gateway.

    Parsing is done by the shared gateway importer (single source of truth).
    With preview=true, the generated tools are returned without persisting.
    With replace=true, the gateway's existing tools are deleted first.
    """
    from ostiari_gateway.openapi_import import OpenAPIError, import_openapi as _gen

    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    source = body.spec if body.spec is not None else body.source
    if source is None:
        raise HTTPException(status_code=400, detail="provide 'source' (url/json/yaml) or 'spec' (object)")

    try:
        tool_defs = _gen(source, server_url=body.server_url, name_prefix=body.name_prefix)
    except OpenAPIError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 — fetch/parse errors surface as 400
        raise HTTPException(status_code=400, detail=f"could not import spec: {e}") from e

    preview = [
        {"name": t.name, "method": t.method, "endpoint": t.endpoint,
         "description": t.description, "path_params": t.path_params,
         "query_params": t.query_params}
        for t in tool_defs
    ]
    if body.preview:
        return {"status": "preview", "count": len(preview), "tools": preview}

    if body.replace:
        existing = await db.execute(select(Tool).where(Tool.gateway_id == gateway_id))
        for t in existing.scalars().all():
            await db.delete(t)

    for td in tool_defs:
        # Upsert by (gateway_id, name) so re-importing updates rather than duplicates.
        found = await db.execute(
            select(Tool).where(Tool.gateway_id == gateway_id, Tool.name == td.name)
        )
        row = found.scalar_one_or_none()
        if row is None:
            row = Tool(name=td.name, gateway_id=gateway_id)
            db.add(row)
        row.endpoint = td.endpoint
        row.method = td.method
        row.description = td.description
        row.timeout_seconds = td.timeout_seconds
        row.schema_json = td.schema_
        row.path_params = td.path_params
        row.query_params = td.query_params

    await db.commit()
    return {"status": "imported", "gateway_id": gateway_id, "count": len(preview), "tools": preview}
