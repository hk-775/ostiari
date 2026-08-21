"""Canonical authorization, audit, and execution for read-only queries."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Any

from src.gateway.auth.authorization import (
    Action,
    AuthorizationDenied,
    ResourceRef,
    require_authorized,
)
from src.gateway.models import Principal
from src.gateway.security.audit_trail import (
    AuditEventType,
    AuditTrail,
)

from .admission import (
    QueryAdmissionController,
    QueryAdmissionError,
    QueryAdmissionLease,
)
from .athena import (
    AthenaExecutionError,
    AthenaExecutor,
    AthenaQueryResult,
    AthenaQueryTermination,
)
from .models import AthenaRoleBindings
from .repository import (
    DatasourceRepository,
    DatasourceStoreUnavailable,
)
from .reconciliation import QueryLifecycleClaim, QueryTerminalAudit
from .sql_policy import QueryPolicyError, validate_athena_select


logger = logging.getLogger(__name__)


class QueryServiceError(RuntimeError):
    """Safe query-plane error carrying an HTTP-compatible status."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


_ATHENA_STATUS = {
    "invalid_query_limit": 400,
    "athena_query_timeout": 504,
    "athena_query_failed": 502,
    "athena_query_cancelled": 502,
    "athena_scan_limit_exceeded": 502,
    "athena_start_failed": 502,
    "athena_status_failed": 502,
    "athena_results_failed": 502,
}


class QueryService:
    """Execute only project-bound, audited `query.select` operations."""

    def __init__(
        self,
        *,
        repository: DatasourceRepository,
        bindings: AthenaRoleBindings,
        executor: AthenaExecutor,
        audit_trail: AuditTrail,
        require_durable_audit: bool = True,
        admission: QueryAdmissionController | None = None,
        require_durable_admission: bool = False,
    ) -> None:
        self.repository = repository
        self.bindings = bindings
        self.executor = executor
        self.audit_trail = audit_trail
        self.require_durable_audit = require_durable_audit
        self.admission = admission
        self.require_durable_admission = require_durable_admission

    def _require_audit(self) -> None:
        if (
            self.require_durable_audit
            and not self.audit_trail.durable_enabled
        ):
            raise QueryServiceError(
                503,
                "query_audit_unavailable",
                "Durable query audit is unavailable.",
            )

    def _require_admission(self) -> None:
        if self.require_durable_admission and self.admission is None:
            raise QueryServiceError(
                503,
                "query_admission_unavailable",
                "Distributed query admission is unavailable.",
            )

    async def _record(
        self,
        event_type: AuditEventType,
        *,
        principal: Principal,
        project_id: str,
        request_id: str,
        data: dict[str, Any],
    ) -> None:
        self._require_audit()
        try:
            await self.audit_trail.record(
                event_type=event_type,
                user_id=principal.principal_id,
                project_id=project_id,
                request_id=request_id,
                data=data,
                tenant_id=principal.tenant_id,
            )
        except Exception as exc:
            raise QueryServiceError(
                503,
                "query_audit_unavailable",
                "Durable query audit is unavailable.",
            ) from exc

    async def _ack_terminal_audit(
        self,
        claim: object | None,
    ) -> None:
        if not isinstance(claim, QueryLifecycleClaim):
            return
        acknowledge = getattr(self.admission, "ack_audit", None)
        if not callable(acknowledge):
            logger.error(
                "Query audit acknowledgement is unavailable request_id=%s",
                claim.lease.request_id,
            )
            return
        try:
            await acknowledge(claim)
        except Exception:
            # The durable pending marker remains for the reconciliation worker.
            logger.warning(
                "Query audit acknowledgement deferred request_id=%s",
                claim.lease.request_id,
                exc_info=True,
            )

    @staticmethod
    def _error_termination(
        error: AthenaExecutionError,
        execution_id: str | None,
    ) -> AthenaQueryTermination | None:
        resolved_execution_id = (
            error.query_execution_id or execution_id
        )
        if resolved_execution_id is None or error.athena_state is None:
            return None
        return AthenaQueryTermination(
            query_execution_id=resolved_execution_id,
            state=error.athena_state,
            terminal=error.athena_state
            in {"SUCCEEDED", "FAILED", "CANCELLED"},
            data_scanned_bytes=error.data_scanned_bytes,
            engine_execution_ms=error.engine_execution_ms,
            cancellation_requested=error.cancellation_requested,
        )

    async def _cancel_observed_execution(
        self,
        *,
        datasource: Any,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
        execution_id: str | None,
        observed: AthenaQueryTermination | None = None,
    ) -> AthenaQueryTermination | None:
        if observed is not None and observed.terminal:
            return observed
        if execution_id is None:
            return observed
        cancel = getattr(self.executor, "cancel", None)
        if not callable(cancel):
            return observed
        try:
            return await cancel(
                datasource,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                request_id=request_id,
                execution_id=execution_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Athena cancellation status could not be observed "
                "request_id=%s execution_id=%s",
                request_id,
                execution_id,
                exc_info=True,
            )
            return observed

    async def _finalize_and_record_failure(
        self,
        *,
        principal: Principal,
        project_id: str,
        request_id: str,
        datasource_id: str,
        query_sha256: str,
        lease: QueryAdmissionLease | None,
        status: str,
        failure_code: str,
        execution_id: str | None,
        termination: AthenaQueryTermination | None,
        execution_may_have_started: bool = False,
        reconciled: bool = False,
    ) -> None:
        terminal_observed = (
            execution_id is None
            or (
                termination is not None
                and termination.terminal
            )
        )
        observed_scan_bytes = (
            termination.data_scanned_bytes
            if termination is not None
            else None
        )
        accounted_scan_bytes: int | None = None
        scan_accounting = "not_reserved"
        lifecycle_finalized = lease is None
        admission_error: QueryAdmissionError | None = None
        audit_claim: object | None = None
        terminal_audit: QueryTerminalAudit | None = None

        if lease is not None:
            if execution_id is None:
                if execution_may_have_started:
                    accounted_scan_bytes = lease.reserved_scan_bytes
                    scan_accounting = "reserved_fallback"
                else:
                    accounted_scan_bytes = 0
                    scan_accounting = "zero_before_start"
            elif terminal_observed:
                if observed_scan_bytes is None:
                    accounted_scan_bytes = lease.reserved_scan_bytes
                    scan_accounting = "reserved_fallback"
                else:
                    accounted_scan_bytes = min(
                        observed_scan_bytes,
                        lease.reserved_scan_bytes,
                    )
                    scan_accounting = (
                        "actual"
                        if observed_scan_bytes
                        <= lease.reserved_scan_bytes
                        else "reservation_ceiling"
                    )
            else:
                accounted_scan_bytes = lease.reserved_scan_bytes
                scan_accounting = "reservation_held"

            if terminal_observed:
                terminal_audit = QueryTerminalAudit(
                    status=status,
                    failure_code=failure_code,
                    execution_id=execution_id,
                    athena_state=(
                        termination.state
                        if termination is not None
                        else None
                    ),
                    observed_scan_bytes=observed_scan_bytes,
                    accounted_scan_bytes=accounted_scan_bytes,
                    engine_execution_ms=(
                        termination.engine_execution_ms
                        if termination is not None
                        else None
                    ),
                    cancellation_requested=(
                        termination.cancellation_requested
                        if termination is not None
                        else False
                    ),
                    scan_accounting=scan_accounting,
                )
                try:
                    audit_claim = await self.admission.finalize(  # type: ignore[union-attr]
                        lease,
                        status=status,
                        actual_scan_bytes=accounted_scan_bytes,
                        execution_id=execution_id,
                        failure_code=failure_code,
                        terminal_audit=terminal_audit,
                    )
                    lifecycle_finalized = True
                except QueryAdmissionError as exc:
                    admission_error = exc

        audit_status = (
            status
            if terminal_observed or lease is None
            else "reconciliation_pending"
        )
        await self._record(
            AuditEventType.QUERY_RESULT,
            principal=principal,
            project_id=project_id,
            request_id=request_id,
            data={
                "datasource_id": datasource_id,
                "query_sha256": query_sha256,
                "status": audit_status,
                "failure_code": failure_code,
                "query_execution_id": execution_id,
                "athena_state": (
                    termination.state
                    if termination is not None
                    else None
                ),
                "data_scanned_bytes": observed_scan_bytes,
                "accounted_scan_bytes": accounted_scan_bytes,
                "engine_execution_ms": (
                    termination.engine_execution_ms
                    if termination is not None
                    else None
                ),
                "cancellation_requested": (
                    termination.cancellation_requested
                    if termination is not None
                    else False
                ),
                "execution_may_have_started": (
                    execution_may_have_started
                    or execution_id is not None
                ),
                "scan_accounting": scan_accounting,
                "lifecycle_finalized": lifecycle_finalized,
                "reconciled": reconciled,
            },
        )
        await self._ack_terminal_audit(audit_claim)
        if admission_error is not None:
            raise QueryServiceError(
                admission_error.status_code,
                admission_error.code,
                admission_error.message,
            ) from admission_error

    @staticmethod
    def _request_id(value: str | None) -> str:
        if value is None:
            return f"qry_{uuid.uuid4().hex}"
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(ord(character) < 32 for character in value)
        ):
            raise QueryServiceError(
                400,
                "invalid_request_id",
                "request_id is invalid.",
            )
        return value

    @staticmethod
    def _identity(value: object, name: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(ord(character) < 32 for character in value)
        ):
            raise QueryServiceError(
                400,
                "invalid_query_request",
                f"{name} must be a non-empty identifier.",
            )
        return value

    async def execute(
        self,
        *,
        principal: Principal,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        sql: object,
        max_rows: int | None = None,
        request_id: str | None = None,
        rehearsal: Any | None = None,
        rehearsal_binding: Any | None = None,
        rehearsal_ledger: Any | None = None,
    ) -> dict[str, Any]:
        tenant_id = self._identity(tenant_id, "tenant_id")
        project_id = self._identity(project_id, "project_id")
        datasource_id = self._identity(
            datasource_id,
            "datasource_id",
        )
        resolved_request_id = self._request_id(request_id)
        resource = ResourceRef(
            resource_type="datasource",
            resource_id=datasource_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        try:
            require_authorized(
                principal,
                Action.QUERY_SELECT,
                resource,
            )
        except AuthorizationDenied as exc:
            raise QueryServiceError(
                exc.decision.status_code,
                "resource_not_found"
                if exc.decision.conceal_resource
                else "query_not_authorized",
                "The requested resource was not found."
                if exc.decision.conceal_resource
                else "The principal is not authorized to run queries.",
            ) from exc
        try:
            datasource = await self.repository.get(
                tenant_id,
                project_id,
                datasource_id,
            )
        except DatasourceStoreUnavailable as exc:
            raise QueryServiceError(
                503,
                "datasource_store_unavailable",
                "Datasource configuration is temporarily unavailable.",
            ) from exc
        if datasource is None:
            raise QueryServiceError(
                404,
                "resource_not_found",
                "The requested resource was not found.",
            )
        if not datasource.enabled:
            raise QueryServiceError(
                403,
                "datasource_disabled",
                "The datasource is disabled.",
            )
        if not self.bindings.allows(
            tenant_id,
            project_id,
            datasource.role_arn,
        ):
            if (
                getattr(rehearsal, "operation", None)
                == "verify-deferred-accounting"
                and rehearsal_binding is not None
                and rehearsal_ledger is not None
            ):
                await asyncio.to_thread(
                    rehearsal_ledger.append_observation,
                    rehearsal_binding,
                    "query-lifecycle",
                    {
                        "phase": "deferred",
                        "request_id": resolved_request_id,
                        "reservation_units": 0,
                        "terminal_state": "DEFERRED",
                    },
                )
            raise QueryServiceError(
                503,
                "datasource_binding_invalid",
                "Datasource role binding is not approved by the deployment.",
            )

        try:
            validated = validate_athena_select(sql, datasource)
        except QueryPolicyError as exc:
            raw_hash = hashlib.sha256(
                str(sql).encode("utf-8", errors="replace")
            ).hexdigest()
            await self._record(
                AuditEventType.QUERY_REJECTED,
                principal=principal,
                project_id=project_id,
                request_id=resolved_request_id,
                data={
                    "datasource_id": datasource_id,
                    "query_sha256": raw_hash,
                    "reason": "query_policy_rejected",
                },
            )
            raise QueryServiceError(
                400,
                "query_policy_rejected",
                str(exc),
            ) from exc

        await self._record(
            AuditEventType.QUERY_REQUEST,
            principal=principal,
            project_id=project_id,
            request_id=resolved_request_id,
            data={
                "datasource_id": datasource_id,
                "query_sha256": validated.sha256,
                "table_count": validated.table_count,
                "requested_max_rows": max_rows,
            },
        )
        self._require_admission()
        lease: QueryAdmissionLease | None = None
        execution_id: str | None = None
        checkpoint_hold_seconds: int | None = None
        if self.admission is not None:
            try:
                lease = await self.admission.acquire(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    principal_id=principal.principal_id,
                    request_id=resolved_request_id,
                    datasource_id=datasource_id,
                    query_sha256=validated.sha256,
                )
            except QueryAdmissionError as exc:
                await self._record(
                    AuditEventType.QUERY_REJECTED,
                    principal=principal,
                    project_id=project_id,
                    request_id=resolved_request_id,
                    data={
                        "datasource_id": datasource_id,
                        "query_sha256": validated.sha256,
                        "reason": exc.code,
                    },
                )
                raise QueryServiceError(
                    exc.status_code,
                    exc.code,
                    exc.message,
                ) from exc
            if (
                getattr(rehearsal, "operation", None) == "interrupt-query"
                and rehearsal_binding is not None
                and rehearsal_ledger is not None
            ):
                await asyncio.to_thread(
                    rehearsal_ledger.append_observation,
                    rehearsal_binding,
                    "query-lifecycle",
                    {
                        "phase": "reserved",
                        "request_id": resolved_request_id,
                        "reservation_units": lease.reserved_scan_bytes,
                    },
                )
                checkpoint = await asyncio.to_thread(
                    rehearsal_ledger.read_active_checkpoint,
                    rehearsal_binding,
                    "query-after-reservation",
                )
                if checkpoint is not None:
                    hold_seconds = checkpoint.parameters.get("hold_seconds")
                    if (
                        isinstance(hold_seconds, int)
                        and not isinstance(hold_seconds, bool)
                    ):
                        checkpoint_hold_seconds = hold_seconds

        async def _mark_started(value: str) -> None:
            nonlocal execution_id
            execution_id = value
            if self.admission is not None and lease is not None:
                await self.admission.mark_started(lease, value)

        try:
            if checkpoint_hold_seconds is not None:
                await asyncio.sleep(checkpoint_hold_seconds)
            execution_kwargs: dict[str, Any] = {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "principal_id": principal.principal_id,
                "request_id": resolved_request_id,
                "max_rows": max_rows,
            }
            execution_kwargs["on_started"] = _mark_started
            result = await self.executor.execute(
                validated,
                datasource,
                **execution_kwargs,
            )
        except asyncio.CancelledError:
            if (
                getattr(rehearsal, "operation", None) == "interrupt-query"
                and rehearsal_binding is not None
                and rehearsal_ledger is not None
            ):
                await asyncio.to_thread(
                    rehearsal_ledger.append_observation,
                    rehearsal_binding,
                    "query-lifecycle",
                    {
                        "phase": "interrupted",
                        "request_id": resolved_request_id,
                    },
                )

            async def _finish_cancellation() -> None:
                termination = await self._cancel_observed_execution(
                    datasource=datasource,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    principal_id=principal.principal_id,
                    request_id=resolved_request_id,
                    execution_id=execution_id,
                )
                await self._finalize_and_record_failure(
                    principal=principal,
                    project_id=project_id,
                    request_id=resolved_request_id,
                    datasource_id=datasource_id,
                    query_sha256=validated.sha256,
                    lease=lease,
                    status="cancelled",
                    failure_code="query_cancelled",
                    execution_id=execution_id,
                    termination=termination,
                )
                if (
                    getattr(rehearsal, "operation", None)
                    == "interrupt-query"
                    and rehearsal_binding is not None
                    and rehearsal_ledger is not None
                ):
                    await asyncio.to_thread(
                        rehearsal_ledger.append_observation,
                        rehearsal_binding,
                        "query-lifecycle",
                        {
                            "phase": "reconciled",
                            "request_id": resolved_request_id,
                            "reservation_units": 0,
                            "terminal_state": "CANCELLED",
                        },
                    )

            try:
                await asyncio.shield(_finish_cancellation())
            except Exception:
                logger.exception(
                    "Cancelled query cleanup failed request_id=%s",
                    resolved_request_id,
                )
            raise
        except AthenaExecutionError as exc:
            resolved_execution_id = (
                exc.query_execution_id or execution_id
            )
            termination = self._error_termination(
                exc,
                resolved_execution_id,
            )
            termination = await self._cancel_observed_execution(
                datasource=datasource,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal.principal_id,
                request_id=resolved_request_id,
                execution_id=resolved_execution_id,
                observed=termination,
            )
            terminal_status = (
                "cancelled"
                if (
                    exc.code == "athena_query_cancelled"
                    or (
                        termination is not None
                        and termination.state == "CANCELLED"
                    )
                )
                else "failed"
            )
            await self._finalize_and_record_failure(
                principal=principal,
                project_id=project_id,
                request_id=resolved_request_id,
                datasource_id=datasource_id,
                query_sha256=validated.sha256,
                lease=lease,
                status=terminal_status,
                failure_code=exc.code,
                execution_id=resolved_execution_id,
                termination=termination,
                execution_may_have_started=(
                    exc.execution_may_have_started
                ),
            )
            raise QueryServiceError(
                _ATHENA_STATUS.get(exc.code, 503),
                exc.code,
                exc.message,
            ) from exc
        except Exception as exc:
            termination = await self._cancel_observed_execution(
                datasource=datasource,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal.principal_id,
                request_id=resolved_request_id,
                execution_id=execution_id,
            )
            await self._finalize_and_record_failure(
                principal=principal,
                project_id=project_id,
                request_id=resolved_request_id,
                datasource_id=datasource_id,
                query_sha256=validated.sha256,
                lease=lease,
                status="failed",
                failure_code="query_execution_unavailable",
                execution_id=execution_id,
                termination=termination,
            )
            raise QueryServiceError(
                503,
                "query_execution_unavailable",
                "Query execution is temporarily unavailable.",
            ) from exc
        if self.admission is not None and lease is not None:
            terminal_audit = QueryTerminalAudit(
                status="succeeded",
                failure_code=None,
                execution_id=result.query_execution_id,
                athena_state="SUCCEEDED",
                observed_scan_bytes=result.data_scanned_bytes,
                accounted_scan_bytes=result.data_scanned_bytes,
                engine_execution_ms=result.engine_execution_ms,
                cancellation_requested=False,
                scan_accounting="actual",
                row_count=result.row_count,
                truncated=result.truncated,
                result_bytes=result.result_bytes,
            )
            try:
                audit_claim = await self.admission.finalize(
                    lease,
                    status="succeeded",
                    actual_scan_bytes=result.data_scanned_bytes,
                    execution_id=result.query_execution_id,
                    terminal_audit=terminal_audit,
                )
            except QueryAdmissionError as exc:
                raise QueryServiceError(
                    exc.status_code,
                    exc.code,
                    exc.message,
                ) from exc
        else:
            audit_claim = None
        await self._record_result(
            principal=principal,
            project_id=project_id,
            request_id=resolved_request_id,
            datasource_id=datasource_id,
            query_sha256=validated.sha256,
            result=result,
        )
        await self._ack_terminal_audit(audit_claim)
        response = result.to_dict()
        response.update(
            {
                "request_id": resolved_request_id,
                "datasource_id": datasource_id,
                "project_id": project_id,
            }
        )
        return response

    async def _record_result(
        self,
        *,
        principal: Principal,
        project_id: str,
        request_id: str,
        datasource_id: str,
        query_sha256: str,
        result: AthenaQueryResult,
    ) -> None:
        await self._record(
            AuditEventType.QUERY_RESULT,
            principal=principal,
            project_id=project_id,
            request_id=request_id,
            data={
                "datasource_id": datasource_id,
                "query_sha256": query_sha256,
                "status": "succeeded",
                "query_execution_id": result.query_execution_id,
                "row_count": result.row_count,
                "truncated": result.truncated,
                "data_scanned_bytes": result.data_scanned_bytes,
                "engine_execution_ms": result.engine_execution_ms,
                "result_bytes": result.result_bytes,
            },
        )
