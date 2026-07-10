"""Audit service — records who changed what config, when."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models.database import AuditLog


class AuditService:
    """Records audit entries for all config-changing operations."""

    async def log(
        self,
        db: AsyncSession,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
        db.add(entry)
        await db.flush()
        return entry


audit = AuditService()
