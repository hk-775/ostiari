"""Fleet-wide admission and durable lifecycle coordination for queries."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from src.gateway.rate_limiter import consume_shared_rate_limit


QUERY_RATE_LIMIT_NAMESPACE = "athena-query"
_DENIAL_STATUS = {
    "duplicate_request": 409,
    "project_concurrency": 429,
    "principal_concurrency": 429,
    "project_scan_budget": 429,
    "principal_scan_budget": 429,
}


class QueryAdmissionError(RuntimeError):
    """Safe admission failure carrying an HTTP-compatible status."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class QueryAdmissionLimits:
    """Deployment-owned fleet limits applied before Athena starts."""

    project_rpm: int = 30
    principal_rpm: int = 10
    project_concurrency: int = 5
    principal_concurrency: int = 2
    project_scan_bytes_per_minute: int = 5 * 1024 * 1024 * 1024
    principal_scan_bytes_per_minute: int = 2 * 1024 * 1024 * 1024
    window_seconds: int = 60
    lease_seconds: int = 360

    def __post_init__(self) -> None:
        integer_fields = (
            "project_rpm",
            "principal_rpm",
            "project_concurrency",
            "principal_concurrency",
            "project_scan_bytes_per_minute",
            "principal_scan_bytes_per_minute",
            "window_seconds",
            "lease_seconds",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer")
        if self.principal_rpm > self.project_rpm:
            raise ValueError("principal_rpm must not exceed project_rpm")
        if self.principal_concurrency > self.project_concurrency:
            raise ValueError(
                "principal_concurrency must not exceed project_concurrency"
            )
        if (
            self.principal_scan_bytes_per_minute
            > self.project_scan_bytes_per_minute
        ):
            raise ValueError(
                "principal scan budget must not exceed project scan budget"
            )
        if not 1 <= self.window_seconds <= 3600:
            raise ValueError("window_seconds must be between 1 and 3600")
        if not 30 <= self.lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 30 and 900")


@dataclass(frozen=True)
class QueryAdmissionLease:
    """One durable reservation that must be finalized exactly once."""

    tenant_id: str
    project_id: str
    principal_id: str
    request_id: str
    datasource_id: str
    query_sha256: str
    lease_token: str
    window_start: int
    lease_expires_at: int
    project_slot: int
    principal_slot: int
    reserved_scan_bytes: int


class QueryAdmissionPersistence(Protocol):
    enabled: bool

    async def reserve_query_capacity(self, **kwargs: Any) -> object: ...

    async def mark_query_started(self, **kwargs: Any) -> object: ...

    async def finalize_query_capacity(self, **kwargs: Any) -> object: ...


class QueryAdmissionController:
    """Fail-closed coordinator for distributed query admission."""

    def __init__(
        self,
        persistence: QueryAdmissionPersistence,
        *,
        limits: QueryAdmissionLimits | None = None,
        max_scan_bytes_per_query: int,
    ) -> None:
        self.persistence = persistence
        self.limits = limits or QueryAdmissionLimits()
        if (
            isinstance(max_scan_bytes_per_query, bool)
            or not isinstance(max_scan_bytes_per_query, int)
            or max_scan_bytes_per_query < 1
        ):
            raise ValueError(
                "max_scan_bytes_per_query must be a positive integer"
            )
        if (
            max_scan_bytes_per_query
            > self.limits.principal_scan_bytes_per_minute
            or max_scan_bytes_per_query
            > self.limits.project_scan_bytes_per_minute
        ):
            raise ValueError(
                "per-query scan limit must fit within aggregate scan budgets"
            )
        self.max_scan_bytes_per_query = max_scan_bytes_per_query

    async def acquire(
        self,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
        datasource_id: str,
        query_sha256: str,
    ) -> QueryAdmissionLease:
        """Reserve rate, concurrency, and worst-case scan capacity."""
        if not getattr(self.persistence, "enabled", False):
            raise QueryAdmissionError(
                503,
                "query_admission_unavailable",
                "Distributed query admission is unavailable.",
            )
        now = datetime.now(timezone.utc)
        rate = await consume_shared_rate_limit(
            self.persistence,
            namespace=QUERY_RATE_LIMIT_NAMESPACE,
            tenant_id=tenant_id,
            user_id=principal_id,
            project_id=project_id,
            user_limit=self.limits.principal_rpm,
            project_limit=self.limits.project_rpm,
            window_seconds=self.limits.window_seconds,
            now=now,
        )
        if not rate.allowed:
            raise QueryAdmissionError(
                429,
                "query_rate_limit_exceeded",
                "Query rate limit exceeded.",
                retry_after_seconds=rate.retry_after_seconds,
            )

        reserve = getattr(
            self.persistence,
            "reserve_query_capacity",
            None,
        )
        if not callable(reserve):
            raise QueryAdmissionError(
                503,
                "query_admission_unavailable",
                "Distributed query admission is unavailable.",
            )
        try:
            decision = await reserve(
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                request_id=request_id,
                datasource_id=datasource_id,
                query_sha256=query_sha256,
                reserved_scan_bytes=self.max_scan_bytes_per_query,
                project_concurrency=self.limits.project_concurrency,
                principal_concurrency=self.limits.principal_concurrency,
                project_scan_limit=(
                    self.limits.project_scan_bytes_per_minute
                ),
                principal_scan_limit=(
                    self.limits.principal_scan_bytes_per_minute
                ),
                window_seconds=self.limits.window_seconds,
                lease_seconds=self.limits.lease_seconds,
                now=now,
            )
        except Exception as exc:
            raise QueryAdmissionError(
                503,
                "query_admission_unavailable",
                "Distributed query admission is unavailable.",
            ) from exc
        if not isinstance(decision, dict):
            raise QueryAdmissionError(
                503,
                "query_admission_unavailable",
                "Distributed query admission is unavailable.",
            )
        if decision.get("allowed") is not True:
            reason = decision.get("reason")
            if not isinstance(reason, str) or reason not in _DENIAL_STATUS:
                raise QueryAdmissionError(
                    503,
                    "query_admission_unavailable",
                    "Distributed query admission is unavailable.",
                )
            retry_after = decision.get("retry_after_seconds")
            if (
                retry_after is not None
                and (
                    isinstance(retry_after, bool)
                    or not isinstance(retry_after, int)
                    or retry_after < 1
                )
            ):
                retry_after = None
            code = (
                "duplicate_query_request"
                if reason == "duplicate_request"
                else "query_admission_exceeded"
            )
            message = (
                "request_id has already been used for this project."
                if reason == "duplicate_request"
                else "Query concurrency or scan budget is exhausted."
            )
            raise QueryAdmissionError(
                _DENIAL_STATUS[reason],
                code,
                message,
                retry_after_seconds=retry_after,
            )
        try:
            lease = QueryAdmissionLease(
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                request_id=request_id,
                datasource_id=datasource_id,
                query_sha256=query_sha256,
                lease_token=str(decision["lease_token"]),
                window_start=int(decision["window_start"]),
                lease_expires_at=int(decision["lease_expires_at"]),
                project_slot=int(decision["project_slot"]),
                principal_slot=int(decision["principal_slot"]),
                reserved_scan_bytes=self.max_scan_bytes_per_query,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QueryAdmissionError(
                503,
                "query_admission_unavailable",
                "Distributed query admission returned invalid state.",
            ) from exc
        if (
            not lease.lease_token
            or lease.window_start < 0
            or lease.lease_expires_at <= int(now.timestamp())
            or not 0
            <= lease.project_slot
            < self.limits.project_concurrency
            or not 0
            <= lease.principal_slot
            < self.limits.principal_concurrency
        ):
            raise QueryAdmissionError(
                503,
                "query_admission_unavailable",
                "Distributed query admission returned invalid state.",
            )
        return lease

    async def mark_started(
        self,
        lease: QueryAdmissionLease,
        execution_id: str,
    ) -> None:
        if not isinstance(execution_id, str) or not execution_id:
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            )
        update = getattr(self.persistence, "mark_query_started", None)
        if not callable(update):
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            )
        try:
            updated = await update(
                tenant_id=lease.tenant_id,
                project_id=lease.project_id,
                request_id=lease.request_id,
                lease_token=lease.lease_token,
                execution_id=execution_id,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            ) from exc
        if updated is not True:
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            )

    async def finalize(
        self,
        lease: QueryAdmissionLease,
        *,
        status: str,
        actual_scan_bytes: int,
        execution_id: str | None,
        failure_code: str | None = None,
        terminal_audit: object,
    ) -> object:
        from src.gateway.query.reconciliation import (
            QueryLifecycleClaim,
            QueryTerminalAudit,
        )

        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("query terminal status is invalid")
        if (
            isinstance(actual_scan_bytes, bool)
            or not isinstance(actual_scan_bytes, int)
            or not 0 <= actual_scan_bytes <= lease.reserved_scan_bytes
        ):
            raise ValueError("actual_scan_bytes is outside the reservation")
        if execution_id is not None and (
            not isinstance(execution_id, str) or not execution_id
        ):
            raise ValueError("execution_id must be None or non-empty")
        if (
            not isinstance(terminal_audit, QueryTerminalAudit)
            or terminal_audit.status != status
            or terminal_audit.execution_id != execution_id
            or terminal_audit.failure_code != failure_code
            or terminal_audit.accounted_scan_bytes != actual_scan_bytes
        ):
            raise ValueError(
                "query terminal audit does not match finalization"
            )
        audit_claim_token = f"query-service-{uuid.uuid4().hex}"
        finalize = getattr(
            self.persistence,
            "finalize_query_capacity",
            None,
        )
        if not callable(finalize):
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            )
        try:
            finalized = await finalize(
                tenant_id=lease.tenant_id,
                project_id=lease.project_id,
                principal_id=lease.principal_id,
                request_id=lease.request_id,
                lease_token=lease.lease_token,
                window_start=lease.window_start,
                project_slot=lease.project_slot,
                principal_slot=lease.principal_slot,
                reserved_scan_bytes=lease.reserved_scan_bytes,
                actual_scan_bytes=actual_scan_bytes,
                status=status,
                execution_id=execution_id,
                failure_code=failure_code,
                terminal_audit=terminal_audit,
                audit_claim_token=audit_claim_token,
                audit_claim_seconds=60,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            ) from exc
        if finalized is not True:
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            )
        return QueryLifecycleClaim(
            lease=lease,
            claim_token=audit_claim_token,
            status=status,
            execution_id=execution_id,
            terminal_audit=terminal_audit,
        )

    async def ack_audit(self, claim: object) -> None:
        from src.gateway.query.reconciliation import QueryLifecycleClaim

        if not isinstance(claim, QueryLifecycleClaim):
            raise ValueError("query terminal audit claim is invalid")
        acknowledge = getattr(
            self.persistence,
            "ack_query_reconciliation_audit",
            None,
        )
        if not callable(acknowledge):
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            )
        try:
            acknowledged = await acknowledge(
                claim=claim,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            ) from exc
        if acknowledged is not True:
            raise QueryAdmissionError(
                503,
                "query_lifecycle_unavailable",
                "Query lifecycle persistence is unavailable.",
            )


def retry_after_for_window(window_start: int, window_seconds: int) -> int:
    """Return a bounded retry delay for a fixed admission window."""
    remaining = (
        window_start
        + window_seconds
        - datetime.now(timezone.utc).timestamp()
    )
    return max(1, math.ceil(remaining))
