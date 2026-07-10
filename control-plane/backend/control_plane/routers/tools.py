"""Tool management API."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.database import get_db
from control_plane.models.database import Gateway, Tool
from control_plane.models.schemas import ToolCreate, ToolResponse

router = APIRouter(prefix="/api/tools", tags=["tools"])


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
