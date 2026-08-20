"""Immutable, tenant-isolated audit trail for compliance recording.

Records are append-only and include a per-tenant hash chain. Production
persistence is expected to append the record and conditionally advance that
tenant's chain head in one atomic operation.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)

GENESIS_HASH = "genesis"
LEGACY_TENANT_ID = "__legacy__"
MAX_APPEND_ATTEMPTS = 5


class AuditStoreUnavailable(RuntimeError):
    """The authoritative audit store cannot safely answer the operation."""


class TenantAuditPersistence(Protocol):
    """Optional durable contract used for canonical tenant audit records.

    Implementations must raise on read/write outages. In particular, a failed
    load must never be converted to ``[]`` because that would make an outage
    indistinguishable from a new tenant with an intact empty chain.
    """

    enabled: bool

    async def append_tenant_audit_record(
        self,
        tenant_id: str,
        record: dict,
        expected_prev_hash: str,
    ) -> bool:
        """Atomically put ``record`` and advance the tenant head.

        Return ``True`` only when the transaction commits. Return ``False`` for
        an expected-head conflict so the caller can reload and retry. Raise for
        every other failure.
        """
        ...

    async def get_latest_tenant_audit_hash(
        self,
        tenant_id: str,
    ) -> str | None:
        """Return the tenant chain head, or ``None`` for a genuinely empty chain."""
        ...

    async def load_tenant_audit_records(
        self,
        tenant_id: str,
        project_id: str | None = None,
    ) -> list[dict]:
        """Return tenant records in append order, optionally project-filtered."""
        ...


class AuditEventType(Enum):
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    PII_REDACTION = "pii_redaction"
    INJECTION_DETECTED = "injection_detected"
    INJECTION_BLOCKED = "injection_blocked"
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"
    POLICY_DENY = "policy_deny"
    KEY_ISSUED = "key_issued"
    KEY_LISTED = "key_listed"
    KEY_REVOKED = "key_revoked"
    KEY_ROTATED = "key_rotated"
    BREAK_GLASS_ACCESS = "break_glass_access"
    QUERY_REQUEST = "query_request"
    QUERY_RESULT = "query_result"
    QUERY_REJECTED = "query_rejected"
    DATASOURCE_MUTATION_REQUEST = "datasource_mutation_request"
    DATASOURCE_MUTATION_RESULT = "datasource_mutation_result"
    TENANT_CONFIG_MUTATION_REQUEST = "tenant_config_mutation_request"
    TENANT_CONFIG_MUTATION_RESULT = "tenant_config_mutation_result"


def _normalize_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    return tenant_id


@dataclass
class AuditRecord:
    """A single immutable audit record."""

    record_id: str
    event_type: AuditEventType
    timestamp: datetime
    user_id: str
    project_id: str
    request_id: str
    data: dict = field(default_factory=dict)
    prev_hash: str = ""
    record_hash: str = ""
    tenant_id: str = LEGACY_TENANT_ID

    def __post_init__(self) -> None:
        self.tenant_id = _normalize_tenant_id(self.tenant_id)

    def compute_hash(self) -> str:
        payload = {
            "record_id": self.record_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "user_id": self.user_id,
            "project_id": self.project_id,
            "request_id": self.request_id,
            "data": self.data,
            "prev_hash": self.prev_hash,
        }
        # Existing rows were hashed before tenant identity was introduced.
        # Keeping their payload byte-for-byte compatible makes the migration
        # explicit instead of silently invalidating every legacy chain.
        if self.tenant_id != LEGACY_TENANT_ID:
            payload["tenant_id"] = self.tenant_id
        serialized = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()


class AuditTrail:
    """Append-only audit trail with one independent chain per tenant."""

    def __init__(
        self,
        persistence: DynamoPersistence | TenantAuditPersistence | None = None,
        buffer_size: int = 10000,
    ) -> None:
        self._persistence = persistence
        self._buffer_size = buffer_size
        self._buffers: dict[str, deque[AuditRecord]] = {}
        self._last_hashes: dict[str, str] = {}
        self._initialized_tenants: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        # Compatibility for code that intentionally inspects the legacy lock.
        self._lock = self._lock_for(LEGACY_TENANT_ID)
        if persistence is not None:
            setattr(persistence, "_audit_trail", self)

    @property
    def _buffer(self) -> deque[AuditRecord]:
        """Legacy buffer compatibility; canonical code must name a tenant."""
        return self._buffer_for(LEGACY_TENANT_ID)

    @property
    def _last_hash(self) -> str:
        """Legacy chain-head compatibility."""
        return self._last_hashes.get(LEGACY_TENANT_ID, GENESIS_HASH)

    @_last_hash.setter
    def _last_hash(self, value: str) -> None:
        self._last_hashes[LEGACY_TENANT_ID] = value

    @property
    def _initialized(self) -> bool:
        """Legacy initialization compatibility."""
        return LEGACY_TENANT_ID in self._initialized_tenants

    @_initialized.setter
    def _initialized(self, value: bool) -> None:
        if value:
            self._initialized_tenants.add(LEGACY_TENANT_ID)
        else:
            self._initialized_tenants.discard(LEGACY_TENANT_ID)

    def _buffer_for(self, tenant_id: str) -> deque[AuditRecord]:
        return self._buffers.setdefault(
            tenant_id,
            deque(maxlen=self._buffer_size),
        )

    def _lock_for(self, tenant_id: str) -> asyncio.Lock:
        return self._locks.setdefault(tenant_id, asyncio.Lock())

    @property
    def _persistence_enabled(self) -> bool:
        return bool(self._persistence is not None and getattr(self._persistence, "enabled", False))

    @property
    def durable_enabled(self) -> bool:
        """Whether canonical audit appends have an authoritative store."""
        return self._persistence_enabled

    async def initialize(
        self,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> None:
        """Load one tenant's durable chain head.

        Tenant heads are hydrated lazily because a process should not scan every
        tenant at startup. A failed durable read leaves the tenant uninitialized
        and raises, which prevents a subsequent append from starting at genesis.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        async with self._lock_for(tenant_id):
            await self._initialize_tenant_locked(tenant_id)

    async def _initialize_tenant_locked(self, tenant_id: str) -> None:
        if tenant_id in self._initialized_tenants:
            return
        if not self._persistence_enabled:
            self._last_hashes[tenant_id] = GENESIS_HASH
            self._initialized_tenants.add(tenant_id)
            return

        try:
            head = await self._load_tenant_head(tenant_id)
        except AuditStoreUnavailable:
            raise
        except Exception as exc:
            raise AuditStoreUnavailable(f"Audit chain head unavailable for tenant {tenant_id!r}") from exc

        self._last_hashes[tenant_id] = head or GENESIS_HASH
        self._initialized_tenants.add(tenant_id)
        logger.info("Audit chain head loaded for tenant %s", tenant_id)

    async def _load_tenant_head(self, tenant_id: str) -> str | None:
        if self._persistence is None:
            return None
        if tenant_id == LEGACY_TENANT_ID:
            loader = getattr(self._persistence, "get_latest_audit_hash", None)
        else:
            loader = getattr(
                self._persistence,
                "get_latest_tenant_audit_hash",
                None,
            )
        if loader is None:
            raise AuditStoreUnavailable("Tenant-qualified audit head persistence is not configured")
        head = await loader() if tenant_id == LEGACY_TENANT_ID else await loader(tenant_id)
        if head is not None and (not isinstance(head, str) or not head):
            raise AuditStoreUnavailable("Audit persistence returned an invalid head")
        return head

    def initialize_sync(
        self,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> None:
        """Loop-safe startup initialization for one tenant."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if tenant_id in self._initialized_tenants or not self._persistence_enabled:
            if not self._persistence_enabled:
                self._last_hashes.setdefault(tenant_id, GENESIS_HASH)
                self._initialized_tenants.add(tenant_id)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            task = loop.create_task(self.initialize(tenant_id))
            task.add_done_callback(self._log_initialization_failure)
        else:
            asyncio.run(self.initialize(tenant_id))

    @staticmethod
    def _log_initialization_failure(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception:
            logger.error("Audit chain initialization failed", exc_info=True)

    async def record(
        self,
        event_type: AuditEventType,
        user_id: str,
        project_id: str,
        request_id: str,
        data: dict | None = None,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> AuditRecord:
        """Append a record to exactly one tenant chain."""
        tenant_id = _normalize_tenant_id(tenant_id)
        async with self._lock_for(tenant_id):
            await self._initialize_tenant_locked(tenant_id)
            record = AuditRecord(
                record_id=f"aud_{uuid.uuid4().hex[:16]}",
                event_type=event_type,
                timestamp=datetime.now(timezone.utc),
                user_id=user_id,
                project_id=project_id,
                request_id=request_id,
                data=copy.deepcopy(data or {}),
                tenant_id=tenant_id,
            )

            if self._persistence_enabled and tenant_id != LEGACY_TENANT_ID:
                await self._append_tenant_record(record)
            else:
                record.prev_hash = self._last_hashes.get(
                    tenant_id,
                    GENESIS_HASH,
                )
                record.record_hash = record.compute_hash()
                if self._persistence_enabled:
                    await self._persist_legacy(record)

            self._last_hashes[tenant_id] = record.record_hash
            self._buffer_for(tenant_id).append(record)
            return record

    async def _append_tenant_record(self, record: AuditRecord) -> None:
        if self._persistence is None:
            raise AuditStoreUnavailable("Audit persistence is not configured")
        append = getattr(
            self._persistence,
            "append_tenant_audit_record",
            None,
        )
        if append is None:
            raise AuditStoreUnavailable("Atomic tenant audit append persistence is not configured")

        for attempt in range(MAX_APPEND_ATTEMPTS):
            if attempt:
                record.timestamp = datetime.now(timezone.utc)
            expected = self._last_hashes.get(record.tenant_id, GENESIS_HASH)
            record.prev_hash = expected
            record.record_hash = record.compute_hash()
            try:
                committed = await append(
                    record.tenant_id,
                    self._serialize_record(record),
                    expected,
                )
            except Exception as exc:
                raise AuditStoreUnavailable(f"Audit append unavailable for tenant {record.tenant_id!r}") from exc

            if committed is True:
                return
            if committed is not False:
                raise AuditStoreUnavailable("Atomic audit append returned an invalid result")
            try:
                durable_head = await self._load_tenant_head(record.tenant_id)
            except Exception as exc:
                raise AuditStoreUnavailable(
                    f"Audit conflict recovery unavailable for tenant {record.tenant_id!r}"
                ) from exc
            self._last_hashes[record.tenant_id] = durable_head or GENESIS_HASH

        raise AuditStoreUnavailable(f"Audit chain remained contended after {MAX_APPEND_ATTEMPTS} attempts")

    async def record_llm_request(
        self,
        user_id: str,
        project_id: str,
        request_id: str,
        model: str,
        provider: str,
        message_count: int,
        pii_redacted_count: int = 0,
        injection_score: float = 0.0,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> AuditRecord:
        """Record an LLM request with security metadata."""
        return await self.record(
            event_type=AuditEventType.LLM_REQUEST,
            user_id=user_id,
            project_id=project_id,
            request_id=request_id,
            data={
                "model": model,
                "provider": provider,
                "message_count": message_count,
                "pii_redacted_count": pii_redacted_count,
                "injection_score": injection_score,
            },
            tenant_id=tenant_id,
        )

    async def record_injection_event(
        self,
        user_id: str,
        project_id: str,
        request_id: str,
        threat_level: str,
        patterns: list[str],
        blocked: bool,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> AuditRecord:
        event_type = AuditEventType.INJECTION_BLOCKED if blocked else AuditEventType.INJECTION_DETECTED
        return await self.record(
            event_type=event_type,
            user_id=user_id,
            project_id=project_id,
            request_id=request_id,
            data={
                "threat_level": threat_level,
                "patterns": patterns,
                "blocked": blocked,
            },
            tenant_id=tenant_id,
        )

    async def record_pii_redaction(
        self,
        user_id: str,
        project_id: str,
        request_id: str,
        redacted_types: list[str],
        count: int,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> AuditRecord:
        return await self.record(
            event_type=AuditEventType.PII_REDACTION,
            user_id=user_id,
            project_id=project_id,
            request_id=request_id,
            data={"redacted_types": redacted_types, "count": count},
            tenant_id=tenant_id,
        )

    async def record_break_glass_access(
        self,
        *,
        user_id: str,
        principal_id: str,
        tenant_id: str,
        project_id: str,
        request_id: str,
        route: str,
        method: str,
        reason: str,
        result: str,
        access: str,
    ) -> AuditRecord:
        """Durably record one platform-admin tenant elevation decision."""
        if not self.durable_enabled:
            raise AuditStoreUnavailable(
                "Durable audit persistence is required for break-glass access"
            )
        return await self.record(
            event_type=AuditEventType.BREAK_GLASS_ACCESS,
            user_id=user_id,
            project_id=project_id,
            request_id=request_id,
            data={
                "principal_id": principal_id,
                "tenant_id": tenant_id,
                "route": route,
                "method": method,
                "reason": reason,
                "result": result,
                "access": access,
            },
            tenant_id=tenant_id,
        )

    def query_recent(
        self,
        project_id: str | None = None,
        event_type: AuditEventType | None = None,
        limit: int = 100,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> list[AuditRecord]:
        """Query one tenant's recent in-memory records."""
        tenant_id = _normalize_tenant_id(tenant_id)
        results = list(self._buffer_for(tenant_id))
        if project_id:
            results = [r for r in results if r.project_id == project_id]
        if event_type:
            results = [r for r in results if r.event_type == event_type]
        return results[-limit:]

    def buffered_records(
        self,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> list[AuditRecord]:
        """Return a snapshot of one tenant's local recent buffer."""
        tenant_id = _normalize_tenant_id(tenant_id)
        return list(self._buffer_for(tenant_id))

    def verify_chain(
        self,
        records: list[AuditRecord] | None = None,
        expected_prev_hash: str = GENESIS_HASH,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> bool:
        """Verify one tenant's in-memory hash chain."""
        tenant_id = _normalize_tenant_id(tenant_id)
        records = records if records is not None else list(self._buffer_for(tenant_id))
        if not records:
            return True

        prev = expected_prev_hash
        for record in records:
            if record.tenant_id != tenant_id:
                return False
            if record.record_hash != record.compute_hash():
                return False
            if record.prev_hash != prev:
                return False
            prev = record.record_hash
        return True

    async def verify_persisted_chain(
        self,
        project_id: str | None = None,
        expected_prev_hash: str = GENESIS_HASH,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> dict:
        """Verify the complete durable tenant chain.

        ``project_id`` is retained for legacy API compatibility, but integrity is
        always checked over the complete tenant chain. A project-filtered subset
        cannot prove links through records belonging to other projects.
        """
        tenant_id = _normalize_tenant_id(tenant_id)
        if not self._persistence_enabled:
            if tenant_id != LEGACY_TENANT_ID:
                return {
                    "available": False,
                    "valid": False,
                    "checked": 0,
                    "tenant_id": tenant_id,
                    "reason": "durable audit store is not configured",
                }
            return {
                "available": True,
                "valid": True,
                "checked": 0,
                "tenant_id": tenant_id,
                "note": "persistence disabled",
            }

        try:
            rows = await self._load_audit_records(tenant_id, project_id=None)
            durable_head = await self._load_tenant_head(tenant_id)
        except Exception as exc:
            logger.error(
                "Durable audit verification unavailable for tenant %s",
                tenant_id,
            )
            return {
                "available": False,
                "valid": False,
                "checked": 0,
                "tenant_id": tenant_id,
                "reason": "durable audit store unavailable",
                "error_type": type(exc).__name__,
            }

        prev = expected_prev_hash
        checked = 0
        project_matches = 0
        for row in rows:
            try:
                rec = self._deserialize_record(row, tenant_id)
            except Exception:
                return {
                    "available": True,
                    "valid": False,
                    "broken_at": row.get("record_id"),
                    "reason": "unreadable row",
                    "checked": checked,
                    "tenant_id": tenant_id,
                }
            if rec.compute_hash() != rec.record_hash:
                return {
                    "available": True,
                    "valid": False,
                    "broken_at": rec.record_id,
                    "reason": "record_hash mismatch (content altered)",
                    "checked": checked,
                    "tenant_id": tenant_id,
                }
            if rec.prev_hash != prev:
                reason = (
                    "first record does not link to expected genesis hash"
                    if checked == 0 and expected_prev_hash == GENESIS_HASH
                    else "prev_hash link broken (row removed/reordered)"
                )
                return {
                    "available": True,
                    "valid": False,
                    "broken_at": rec.record_id,
                    "reason": reason,
                    "checked": checked,
                    "tenant_id": tenant_id,
                }
            prev = rec.record_hash
            checked += 1
            if project_id is None or rec.project_id == project_id:
                project_matches += 1

        if (rows and durable_head != prev) or (not rows and durable_head):
            return {
                "available": True,
                "valid": False,
                "checked": checked,
                "tenant_id": tenant_id,
                "reason": "durable audit head does not match loaded records",
            }
        return {
            "available": True,
            "valid": True,
            "checked": checked,
            "matched": project_matches,
            "tenant_id": tenant_id,
        }

    async def export_records(
        self,
        project_id: str | None = None,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> list[dict]:
        """Return one tenant's audit rows for external verification."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if self._persistence_enabled:
            try:
                rows = await self._load_audit_records(tenant_id, project_id)
                return [self._normalize_export_row(row, tenant_id) for row in rows]
            except Exception as exc:
                raise AuditStoreUnavailable(f"Audit export unavailable for tenant {tenant_id!r}") from exc

        return [
            self._serialize_record(record)
            for record in self._buffer_for(tenant_id)
            if project_id is None or record.project_id == project_id
        ]

    async def _load_audit_records(
        self,
        tenant_id: str,
        project_id: str | None,
    ) -> list[dict]:
        if self._persistence is None:
            return []
        if tenant_id == LEGACY_TENANT_ID:
            loader = getattr(self._persistence, "load_audit_records", None)
        else:
            loader = getattr(
                self._persistence,
                "load_tenant_audit_records",
                None,
            )
        if loader is None:
            raise AuditStoreUnavailable("Tenant-qualified audit record loading is not configured")
        rows = await loader(project_id) if tenant_id == LEGACY_TENANT_ID else await loader(tenant_id, project_id)
        if not isinstance(rows, list):
            raise AuditStoreUnavailable("Audit persistence returned an invalid record set")
        return rows

    @staticmethod
    def _deserialize_record(row: dict, tenant_id: str) -> AuditRecord:
        row_tenant_id = row.get("tenant_id")
        if tenant_id == LEGACY_TENANT_ID:
            row_tenant_id = row_tenant_id or LEGACY_TENANT_ID
        elif row_tenant_id != tenant_id:
            raise ValueError("audit row belongs to a different tenant")
        raw_data = row.get("data")
        data = json.loads(raw_data) if isinstance(raw_data, str) else (raw_data or {})
        return AuditRecord(
            record_id=row["record_id"],
            event_type=AuditEventType(row["event_type"]),
            timestamp=datetime.fromisoformat(row["timestamp"]),
            user_id=row.get("user_id", ""),
            project_id=row.get("project_id", ""),
            request_id=row.get("request_id", ""),
            data=data,
            prev_hash=row.get("prev_hash", ""),
            record_hash=row.get("record_hash", ""),
            tenant_id=row_tenant_id,
        )

    @staticmethod
    def _serialize_record(record: AuditRecord) -> dict:
        return {
            "record_id": record.record_id,
            "event_type": record.event_type.value,
            "timestamp": record.timestamp.isoformat(),
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "project_id": record.project_id,
            "request_id": record.request_id,
            "data": json.dumps(record.data),
            "prev_hash": record.prev_hash,
            "record_hash": record.record_hash,
        }

    @staticmethod
    def _normalize_export_row(row: dict, tenant_id: str) -> dict:
        row_tenant_id = row.get("tenant_id")
        if tenant_id == LEGACY_TENANT_ID:
            row_tenant_id = row_tenant_id or LEGACY_TENANT_ID
        elif row_tenant_id != tenant_id:
            raise AuditStoreUnavailable("Audit persistence returned a cross-tenant row")
        normalized = dict(row)
        normalized["tenant_id"] = row_tenant_id
        return normalized

    async def _persist_legacy(self, record: AuditRecord) -> None:
        """Persist a legacy record through the pre-tenant compatibility API."""
        if self._persistence is None:
            return
        item = self._serialize_record(record)
        item.update(
            {
                "PK": f"AUDIT#{record.project_id}",
                "SK": f"AUDIT#{record.timestamp.isoformat()}#{record.record_id}",
            }
        )
        try:
            await self._persistence.put_item(item)
        except Exception:
            logger.error(
                "Failed to persist legacy audit record %s",
                record.record_id,
                exc_info=True,
            )
            raise
