"""Lease-safe recovery of interrupted Athena query lifecycles."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from src.gateway.security.audit_trail import AuditEventType, AuditTrail

from .admission import QueryAdmissionLease
from .athena import AthenaExecutor, AthenaQueryTermination
from .models import AthenaRoleBindings
from .repository import DatasourceRepository


logger = logging.getLogger(__name__)
_ACTIVE_LIFECYCLE_STATES = frozenset({"accepted", "running"})
_TERMINAL_LIFECYCLE_STATES = frozenset(
    {"succeeded", "failed", "cancelled"}
)


class QueryReconciliationError(RuntimeError):
    """A durable reconciliation dependency returned unsafe state."""


@dataclass(frozen=True)
class QueryTerminalAudit:
    """Durable terminal evidence retained until its audit append is acknowledged."""

    status: str
    failure_code: str | None
    execution_id: str | None
    athena_state: str | None
    observed_scan_bytes: int | None
    accounted_scan_bytes: int
    engine_execution_ms: int | None
    cancellation_requested: bool
    scan_accounting: str
    row_count: int | None = None
    truncated: bool | None = None
    result_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL_LIFECYCLE_STATES:
            raise ValueError("query terminal audit status is invalid")
        for name, value in (
            ("observed_scan_bytes", self.observed_scan_bytes),
            ("engine_execution_ms", self.engine_execution_ms),
            ("row_count", self.row_count),
            ("result_bytes", self.result_bytes),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"{name} must be None or non-negative")
        if (
            isinstance(self.accounted_scan_bytes, bool)
            or not isinstance(self.accounted_scan_bytes, int)
            or self.accounted_scan_bytes < 0
        ):
            raise ValueError(
                "accounted_scan_bytes must be a non-negative integer"
            )
        if not isinstance(self.cancellation_requested, bool):
            raise ValueError("cancellation_requested must be boolean")
        if self.truncated is not None and not isinstance(
            self.truncated,
            bool,
        ):
            raise ValueError("truncated must be None or boolean")
        if not isinstance(self.scan_accounting, str) or not self.scan_accounting:
            raise ValueError("scan_accounting must be non-empty")
        if self.scan_accounting not in {
            "actual",
            "reserved_fallback",
            "reservation_ceiling",
            "zero_before_start",
        }:
            raise ValueError("scan_accounting is invalid")
        for name, value in (
            ("failure_code", self.failure_code),
            ("execution_id", self.execution_id),
            ("athena_state", self.athena_state),
        ):
            if value is not None and (
                not isinstance(value, str) or not value
            ):
                raise ValueError(f"{name} must be None or non-empty")
        if self.athena_state is not None and self.athena_state not in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
        }:
            raise ValueError("athena_state is invalid")
        if self.status == "succeeded" and self.failure_code is not None:
            raise ValueError("succeeded query audit cannot have a failure code")
        if self.status != "succeeded" and self.failure_code is None:
            raise ValueError("failed query audit requires a failure code")
        if self.status != "succeeded" and any(
            value is not None
            for value in (
                self.row_count,
                self.truncated,
                self.result_bytes,
            )
        ):
            raise ValueError(
                "failed query audit cannot contain result metadata"
            )


@dataclass(frozen=True)
class QueryLifecycleClaim:
    """One lifecycle record exclusively leased to a reconciliation worker."""

    lease: QueryAdmissionLease
    claim_token: str
    status: str
    execution_id: str | None = None
    terminal_audit: QueryTerminalAudit | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lease, QueryAdmissionLease):
            raise ValueError("query reconciliation lease is invalid")
        if not isinstance(self.claim_token, str) or not self.claim_token:
            raise ValueError("query reconciliation claim token is invalid")
        if self.status not in (
            _ACTIVE_LIFECYCLE_STATES | _TERMINAL_LIFECYCLE_STATES
        ):
            raise ValueError("query reconciliation lifecycle status is invalid")
        if self.execution_id is not None and (
            not isinstance(self.execution_id, str) or not self.execution_id
        ):
            raise ValueError("query execution ID is invalid")
        if self.status == "running" and self.execution_id is None:
            raise ValueError("running query lifecycle requires an execution ID")
        if self.status == "accepted" and self.execution_id is not None:
            raise ValueError(
                "accepted query lifecycle must not have an execution ID"
            )
        if self.status in _TERMINAL_LIFECYCLE_STATES:
            if self.terminal_audit is None:
                raise ValueError(
                    "terminal reconciliation claim requires pending audit data"
                )
            if self.terminal_audit.status != self.status:
                raise ValueError(
                    "terminal audit status does not match lifecycle status"
                )
            if self.terminal_audit.execution_id != self.execution_id:
                raise ValueError(
                    "terminal audit execution ID does not match lifecycle"
                )
            if (
                self.terminal_audit.accounted_scan_bytes
                > self.lease.reserved_scan_bytes
            ):
                raise ValueError(
                    "terminal accounting exceeds the query reservation"
                )
        elif self.terminal_audit is not None:
            raise ValueError(
                "active reconciliation claim cannot have terminal audit data"
            )


@dataclass(frozen=True)
class QueryLifecyclePage:
    """One bounded page of durably claimed lifecycle records."""

    claims: tuple[QueryLifecycleClaim, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claims, tuple) or any(
            not isinstance(claim, QueryLifecycleClaim)
            for claim in self.claims
        ):
            raise ValueError("query reconciliation claims are invalid")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ValueError("query reconciliation cursor is invalid")


@dataclass(frozen=True)
class QueryReconciliationResult:
    """Bounded reconciliation run counters for metrics and alarms."""

    claimed: int
    finalized: int
    audited: int
    deferred: int
    lost_claims: int
    failed: int
    pages: int
    next_cursor: str | None


class QueryReconciliationStore(Protocol):
    """Durable backend contract implemented by the scheduled worker store.

    The claim scan must return only expired active records or terminal records
    with pending audit evidence, and atomically lease each returned record.
    ``finalize_query_reconciliation`` must atomically reconcile reservation
    counters, release admission slots, make the lifecycle terminal, and retain
    ``terminal_audit`` as pending. ``ack_query_reconciliation_audit`` clears
    that pending marker only after the tenant audit append succeeds.
    """

    enabled: bool

    async def claim_query_reconciliation_page(
        self,
        *,
        owner_token: str,
        now: datetime,
        claim_seconds: int,
        limit: int,
        cursor: str | None,
    ) -> QueryLifecyclePage: ...

    async def finalize_query_reconciliation(
        self,
        *,
        claim: QueryLifecycleClaim,
        terminal_audit: QueryTerminalAudit,
        now: datetime,
    ) -> bool: ...

    async def defer_query_reconciliation(
        self,
        *,
        claim: QueryLifecycleClaim,
        now: datetime,
    ) -> bool: ...

    async def ack_query_reconciliation_audit(
        self,
        *,
        claim: QueryLifecycleClaim,
        now: datetime,
    ) -> bool: ...


class QueryLifecycleReconciler:
    """Recover accepted/running queries without trusting process-local state."""

    def __init__(
        self,
        *,
        store: QueryReconciliationStore,
        repository: DatasourceRepository,
        bindings: AthenaRoleBindings,
        executor: AthenaExecutor,
        audit_trail: AuditTrail,
        claim_seconds: int = 60,
        page_size: int = 25,
    ) -> None:
        if (
            isinstance(claim_seconds, bool)
            or not isinstance(claim_seconds, int)
            or not 15 <= claim_seconds <= 900
        ):
            raise ValueError(
                "query reconciliation claim_seconds must be between 15 and 900"
            )
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError(
                "query reconciliation page_size must be between 1 and 100"
            )
        self.store = store
        self.repository = repository
        self.bindings = bindings
        self.executor = executor
        self.audit_trail = audit_trail
        self.claim_seconds = claim_seconds
        self.page_size = page_size

    async def run(
        self,
        *,
        cursor: str | None = None,
        max_pages: int = 10,
    ) -> QueryReconciliationResult:
        """Process a bounded, cursor-safe batch suitable for scheduled Lambda."""
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 100
        ):
            raise ValueError(
                "query reconciliation max_pages must be between 1 and 100"
            )
        if not getattr(self.store, "enabled", False):
            raise QueryReconciliationError(
                "durable query reconciliation store is unavailable"
            )
        if not self.audit_trail.durable_enabled:
            raise QueryReconciliationError(
                "durable query reconciliation audit is unavailable"
            )

        owner_token = f"query-reconciler-{uuid.uuid4().hex}"
        seen_cursors: set[str] = set()
        claimed = finalized = audited = deferred = lost = failed = pages = 0
        next_cursor = cursor
        for _ in range(max_pages):
            if next_cursor is not None:
                if next_cursor in seen_cursors:
                    raise QueryReconciliationError(
                        "query reconciliation cursor repeated"
                    )
                seen_cursors.add(next_cursor)
            now = datetime.now(timezone.utc)
            try:
                page = await self.store.claim_query_reconciliation_page(
                    owner_token=owner_token,
                    now=now,
                    claim_seconds=self.claim_seconds,
                    limit=self.page_size,
                    cursor=next_cursor,
                )
            except Exception as exc:
                raise QueryReconciliationError(
                    "query reconciliation claim scan failed"
                ) from exc
            if not isinstance(page, QueryLifecyclePage):
                raise QueryReconciliationError(
                    "query reconciliation store returned an invalid page"
                )
            if len(page.claims) > self.page_size:
                raise QueryReconciliationError(
                    "query reconciliation store exceeded the page limit"
                )
            pages += 1
            claimed += len(page.claims)
            for claim in page.claims:
                try:
                    outcome = await self._reconcile_claim(claim)
                except Exception:
                    failed += 1
                    logger.exception(
                        "Query reconciliation failed tenant=%s project=%s "
                        "request_id=%s",
                        claim.lease.tenant_id,
                        claim.lease.project_id,
                        claim.lease.request_id,
                    )
                    continue
                if outcome == "finalized":
                    finalized += 1
                    audited += 1
                elif outcome == "audited":
                    audited += 1
                elif outcome == "deferred":
                    deferred += 1
                elif outcome == "lost":
                    lost += 1
            next_cursor = page.next_cursor
            if next_cursor is None:
                break

        return QueryReconciliationResult(
            claimed=claimed,
            finalized=finalized,
            audited=audited,
            deferred=deferred,
            lost_claims=lost,
            failed=failed,
            pages=pages,
            next_cursor=next_cursor,
        )

    async def _reconcile_claim(self, claim: QueryLifecycleClaim) -> str:
        if claim.status in _TERMINAL_LIFECYCLE_STATES:
            await self._audit_and_ack(claim, claim.terminal_audit)
            return "audited"

        terminal_audit = await self._terminal_state(claim)
        if terminal_audit is None:
            released = await self.store.defer_query_reconciliation(
                claim=claim,
                now=datetime.now(timezone.utc),
            )
            return "deferred" if released is True else "lost"

        finalized = await self.store.finalize_query_reconciliation(
            claim=claim,
            terminal_audit=terminal_audit,
            now=datetime.now(timezone.utc),
        )
        if finalized is not True:
            return "lost"
        terminal_claim = QueryLifecycleClaim(
            lease=claim.lease,
            claim_token=claim.claim_token,
            status=terminal_audit.status,
            execution_id=terminal_audit.execution_id,
            terminal_audit=terminal_audit,
        )
        await self._audit_and_ack(terminal_claim, terminal_audit)
        return "finalized"

    async def _terminal_state(
        self,
        claim: QueryLifecycleClaim,
    ) -> QueryTerminalAudit | None:
        lease = claim.lease
        if claim.status == "accepted":
            return QueryTerminalAudit(
                status="failed",
                failure_code="query_interrupted_before_execution_id",
                execution_id=None,
                athena_state=None,
                observed_scan_bytes=None,
                accounted_scan_bytes=lease.reserved_scan_bytes,
                engine_execution_ms=None,
                cancellation_requested=False,
                scan_accounting="reserved_fallback",
            )

        try:
            datasource = await self.repository.get(
                lease.tenant_id,
                lease.project_id,
                lease.datasource_id,
            )
        except Exception:
            logger.warning(
                "Datasource reconciliation lookup unavailable request_id=%s",
                lease.request_id,
                exc_info=True,
            )
            return None
        if datasource is None:
            logger.warning(
                "Datasource missing during query reconciliation "
                "request_id=%s",
                lease.request_id,
            )
            return None
        if not self.bindings.allows(
            lease.tenant_id,
            lease.project_id,
            datasource.role_arn,
        ):
            logger.warning(
                "Datasource binding unavailable during query reconciliation "
                "request_id=%s",
                lease.request_id,
            )
            return None

        try:
            termination = await self.executor.cancel(
                datasource,
                tenant_id=lease.tenant_id,
                project_id=lease.project_id,
                principal_id=lease.principal_id,
                request_id=lease.request_id,
                execution_id=claim.execution_id or "",
            )
        except Exception:
            logger.warning(
                "Athena reconciliation status unavailable request_id=%s",
                lease.request_id,
                exc_info=True,
            )
            return None
        if not termination.terminal:
            return None
        return self._terminal_audit(lease, termination)

    @staticmethod
    def _terminal_audit(
        lease: QueryAdmissionLease,
        termination: AthenaQueryTermination,
    ) -> QueryTerminalAudit:
        if termination.state == "SUCCEEDED":
            status = "succeeded"
            failure_code = None
        elif termination.state == "CANCELLED":
            status = "cancelled"
            failure_code = "query_cancelled_after_interruption"
        else:
            status = "failed"
            failure_code = "athena_query_failed"

        if termination.data_scanned_bytes is None:
            accounted = lease.reserved_scan_bytes
            accounting = "reserved_fallback"
        else:
            accounted = min(
                termination.data_scanned_bytes,
                lease.reserved_scan_bytes,
            )
            accounting = (
                "actual"
                if termination.data_scanned_bytes
                <= lease.reserved_scan_bytes
                else "reservation_ceiling"
            )
        return QueryTerminalAudit(
            status=status,
            failure_code=failure_code,
            execution_id=termination.query_execution_id,
            athena_state=termination.state,
            observed_scan_bytes=termination.data_scanned_bytes,
            accounted_scan_bytes=accounted,
            engine_execution_ms=termination.engine_execution_ms,
            cancellation_requested=termination.cancellation_requested,
            scan_accounting=accounting,
        )

    async def _audit_and_ack(
        self,
        claim: QueryLifecycleClaim,
        terminal: QueryTerminalAudit | None,
    ) -> None:
        if terminal is None:
            raise QueryReconciliationError(
                "terminal query reconciliation audit is missing"
            )
        lease = claim.lease
        await self.audit_trail.record(
            event_type=AuditEventType.QUERY_RESULT,
            user_id=lease.principal_id,
            project_id=lease.project_id,
            request_id=lease.request_id,
            tenant_id=lease.tenant_id,
            data={
                "datasource_id": lease.datasource_id,
                "query_sha256": lease.query_sha256,
                "status": terminal.status,
                "failure_code": terminal.failure_code,
                "query_execution_id": terminal.execution_id,
                "athena_state": terminal.athena_state,
                "data_scanned_bytes": terminal.observed_scan_bytes,
                "accounted_scan_bytes": terminal.accounted_scan_bytes,
                "engine_execution_ms": terminal.engine_execution_ms,
                "cancellation_requested": (
                    terminal.cancellation_requested
                ),
                "execution_may_have_started": (
                    terminal.execution_id is not None
                    or terminal.failure_code
                    == "query_interrupted_before_execution_id"
                ),
                "scan_accounting": terminal.scan_accounting,
                "lifecycle_finalized": True,
                "reconciled": True,
                **(
                    {"row_count": terminal.row_count}
                    if terminal.row_count is not None
                    else {}
                ),
                **(
                    {"truncated": terminal.truncated}
                    if terminal.truncated is not None
                    else {}
                ),
                **(
                    {"result_bytes": terminal.result_bytes}
                    if terminal.result_bytes is not None
                    else {}
                ),
            },
        )
        acknowledged = await self.store.ack_query_reconciliation_audit(
            claim=claim,
            now=datetime.now(timezone.utc),
        )
        if acknowledged is not True:
            raise QueryReconciliationError(
                "query reconciliation audit acknowledgement was lost"
            )


class QueryReconciliationWorker:
    """Own one immediate, periodic reconciliation loop per runtime."""

    def __init__(
        self,
        reconciler: QueryLifecycleReconciler,
        *,
        interval_seconds: float = 30.0,
        max_pages: int = 10,
    ) -> None:
        if not isinstance(reconciler, QueryLifecycleReconciler):
            raise ValueError("query reconciliation worker is invalid")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or not 0.01 <= interval_seconds <= 3600
        ):
            raise ValueError(
                "query reconciliation interval must be between 0.01 and 3600"
            )
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 100
        ):
            raise ValueError(
                "query reconciliation max_pages must be between 1 and 100"
            )
        self.reconciler = reconciler
        self.interval_seconds = float(interval_seconds)
        self.max_pages = max_pages
        self._cursor: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    async def start(self) -> None:
        """Start once and schedule the first bounded pass immediately."""
        async with self._lock:
            if self.running:
                return
            stop_event = asyncio.Event()
            self._stop_event = stop_event
            self._task = asyncio.create_task(
                self._run(stop_event),
                name="query-lifecycle-reconciliation",
            )
        # Let the task enter its immediate pass before startup continues.
        await asyncio.sleep(0)

    async def stop(self) -> None:
        """Cancel and await the owned task; repeated shutdown is harmless."""
        async with self._lock:
            task = self._task
            stop_event = self._stop_event
            if task is None:
                return
            if stop_event is not None:
                stop_event.set()
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                if self._task is task:
                    self._task = None
                    self._stop_event = None

    async def _run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                result = await self.reconciler.run(
                    cursor=self._cursor,
                    max_pages=self.max_pages,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic query reconciliation failed")
            else:
                self._cursor = result.next_cursor

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                continue
