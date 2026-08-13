"""Audit service — records who changed what config, when, tamper-evidently.

Each entry is hash-chained: entry_hash = SHA-256(prev_hash + canonical(content)).
Any alteration or deletion of a row breaks every subsequent hash, so tampering
is detectable via verify_chain(). The genesis row chains from an empty prev_hash.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models.database import AuditChainHead, AuditLog

_GENESIS = ""  # prev_hash of the first entry
_GLOBAL_HEAD = "global"


def _canonical(actor: str, action: str, resource_type: str, resource_id: str,
               details: dict[str, Any], timestamp: str) -> str:
    """Stable serialization of an entry's content for hashing."""
    return json.dumps({
        "actor": actor, "action": action, "resource_type": resource_type,
        "resource_id": resource_id, "details": details, "timestamp": timestamp,
    }, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev_hash: str, content: str) -> str:
    return hashlib.sha256((prev_hash + "|" + content).encode()).hexdigest()


def _ts_str(ts: datetime) -> str:
    """Canonical UTC second-precision timestamp, stable across SQLite round-trips.

    SQLite stores datetimes tz-naive, so a tz-aware write and a tz-naive re-read
    would hash differently. Normalize to naive-UTC seconds so both sides match:
    convert aware -> UTC, drop tzinfo, drop microseconds.
    """
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.replace(microsecond=0).isoformat()


class AuditService:
    """Records tamper-evident audit entries for config-changing operations."""

    async def _last_hash_from_entries(self, db: AsyncSession) -> str:
        """Most recent entry hash, used to initialize an upgraded database."""
        result = await db.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        )
        row = result.scalar_one_or_none()
        return row.entry_hash if row and row.entry_hash else _GENESIS

    async def _lock_head(self, db: AsyncSession) -> AuditChainHead:
        """Lock the chain head so concurrent appenders cannot fork the chain."""
        initial_hash = await self._last_hash_from_entries(db)
        dialect = db.get_bind().dialect.name
        insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
        await db.execute(
            insert_fn(AuditChainHead)
            .values(name=_GLOBAL_HEAD, entry_hash=initial_hash)
            .on_conflict_do_nothing(index_elements=[AuditChainHead.name])
        )
        result = await db.execute(
            select(AuditChainHead)
            .where(AuditChainHead.name == _GLOBAL_HEAD)
            .with_for_update()
        )
        return result.scalar_one()

    async def log(
        self,
        db: AsyncSession,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
        org: str = "default",
    ) -> AuditLog:
        details = details or {}
        head = await self._lock_head(db)
        prev = head.entry_hash or _GENESIS
        # Set the timestamp explicitly (don't rely on the DB default) so the exact
        # value we hash is the value that's stored — SQLite datetime round-tripping
        # can otherwise lose microseconds and falsely break the chain on re-read.
        ts = datetime.now(timezone.utc)
        # org_id is NOT part of the hashed content — the audit chain stays a single
        # global chain (verify_chain checks integrity across all orgs); org only
        # scopes which rows a tenant can READ.
        content = _canonical(actor, action, resource_type, resource_id, details,
                             _ts_str(ts))
        entry = AuditLog(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            timestamp=ts,
            org_id=org,
            prev_hash=prev,
            entry_hash=_hash(prev, content),
        )
        db.add(entry)
        await db.flush()
        head.entry_hash = entry.entry_hash or _GENESIS
        await db.flush()
        return entry

    async def verify_chain(self, db: AsyncSession) -> dict[str, Any]:
        """Recompute the hash chain and report the first break, if any."""
        result = await db.execute(select(AuditLog).order_by(AuditLog.id.asc()))
        rows = result.scalars().all()
        prev = _GENESIS
        checked = 0
        for row in rows:
            if row.entry_hash is None:
                # Legacy pre-chain row: can't verify; reset expectation.
                prev = _GENESIS
                continue
            content = _canonical(row.actor, row.action, row.resource_type,
                                row.resource_id, row.details or {},
                                _ts_str(row.timestamp))
            expected = _hash(row.prev_hash or _GENESIS, content)
            if prev != _GENESIS and row.prev_hash != prev:
                return {"valid": False, "broken_at_id": row.id,
                        "reason": "prev_hash does not match preceding entry_hash",
                        "checked": checked}
            if expected != row.entry_hash:
                return {"valid": False, "broken_at_id": row.id,
                        "reason": "entry_hash mismatch (content altered)",
                        "checked": checked}
            prev = row.entry_hash
            checked += 1
        head = await db.get(AuditChainHead, _GLOBAL_HEAD)
        if head is not None and (head.entry_hash or _GENESIS) != prev:
            return {
                "valid": False,
                "reason": "chain head does not match final entry",
                "checked": checked,
            }
        return {"valid": True, "checked": checked}


audit = AuditService()


def actor_of(request: Any) -> str:
    """Return the identity established by auth middleware, never a caller header."""
    if request is None:
        return "system"
    return getattr(request.state, "audit_actor", "system")
