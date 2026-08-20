"""Bounded Athena execution through project-bound assumed roles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

from botocore.config import Config

from .models import AthenaDatasource
from .sql_policy import ValidatedQuery


logger = logging.getLogger(__name__)
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
_ACTIVE_STATES = frozenset({"QUEUED", "RUNNING"})
_ENCRYPTED_OUTPUT_OPTIONS = frozenset({"SSE_KMS", "CSE_KMS"})
_MAX_ATHENA_COLUMNS = 1000
_MAX_COLUMN_METADATA_BYTES = 4096
_MAX_RESULT_PAGE_ROWS = 100
_CANCELLATION_TIMEOUT_SECONDS = 5.0
AWS_CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"mode": "standard", "max_attempts": 2},
)


class AthenaExecutionError(RuntimeError):
    """Sanitized Athena execution failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        query_execution_id: str | None = None,
        athena_state: str | None = None,
        data_scanned_bytes: int | None = None,
        engine_execution_ms: int | None = None,
        cancellation_requested: bool = False,
        execution_may_have_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.query_execution_id = query_execution_id
        self.athena_state = athena_state
        self.data_scanned_bytes = data_scanned_bytes
        self.engine_execution_ms = engine_execution_ms
        self.cancellation_requested = cancellation_requested
        self.execution_may_have_started = execution_may_have_started

    def attach_termination(
        self,
        termination: AthenaQueryTermination,
    ) -> AthenaExecutionError:
        """Preserve billable terminal metadata while retaining a safe error."""
        if self.query_execution_id is None:
            self.query_execution_id = termination.query_execution_id
        if self.athena_state is None:
            self.athena_state = termination.state
        if self.data_scanned_bytes is None:
            self.data_scanned_bytes = termination.data_scanned_bytes
        if self.engine_execution_ms is None:
            self.engine_execution_ms = termination.engine_execution_ms
        self.cancellation_requested = (
            self.cancellation_requested
            or termination.cancellation_requested
        )
        self.execution_may_have_started = True
        return self


@dataclass(frozen=True)
class AthenaQueryLimits:
    timeout_seconds: float = 30.0
    max_rows: int = 1000
    max_result_bytes: int = 1024 * 1024
    max_bytes_scanned: int = 1024 * 1024 * 1024
    poll_interval_seconds: float = 0.25

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 300
        ):
            raise ValueError(
                "Athena timeout must be between 0 and 300 seconds"
            )
        if (
            not isinstance(self.max_rows, int)
            or isinstance(self.max_rows, bool)
            or not 1 <= self.max_rows <= 10_000
        ):
            raise ValueError("Athena max_rows must be between 1 and 10000")
        if (
            not isinstance(self.max_result_bytes, int)
            or isinstance(self.max_result_bytes, bool)
            or not 1024 <= self.max_result_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError(
                "Athena max_result_bytes must be between 1 KiB and 16 MiB"
            )
        if (
            not isinstance(self.max_bytes_scanned, int)
            or isinstance(self.max_bytes_scanned, bool)
            or self.max_bytes_scanned <= 0
        ):
            raise ValueError(
                "Athena max_bytes_scanned must be a positive integer"
            )
        if (
            not math.isfinite(self.poll_interval_seconds)
            or not 0.05 <= self.poll_interval_seconds <= 5
        ):
            raise ValueError(
                "Athena poll interval must be between 0.05 and 5 seconds"
            )


@dataclass(frozen=True)
class AthenaQueryResult:
    query_execution_id: str
    columns: tuple[dict[str, str], ...]
    rows: tuple[tuple[str | None, ...], ...]
    row_count: int
    truncated: bool
    data_scanned_bytes: int
    engine_execution_ms: int
    result_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_execution_id": self.query_execution_id,
            "columns": [dict(column) for column in self.columns],
            "rows": [list(row) for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "statistics": {
                "data_scanned_bytes": self.data_scanned_bytes,
                "engine_execution_ms": self.engine_execution_ms,
                "result_bytes": self.result_bytes,
            },
        }


@dataclass(frozen=True)
class AthenaQueryTermination:
    """Observed Athena state used for durable terminal accounting."""

    query_execution_id: str
    state: str
    terminal: bool
    data_scanned_bytes: int | None
    engine_execution_ms: int | None
    cancellation_requested: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.query_execution_id, str)
            or not self.query_execution_id
        ):
            raise ValueError("Athena query execution ID is invalid")
        if self.state not in _TERMINAL_STATES | _ACTIVE_STATES:
            raise ValueError("Athena query state is invalid")
        if self.terminal is not (self.state in _TERMINAL_STATES):
            raise ValueError("Athena query terminal flag does not match state")
        for name, value in (
            ("data_scanned_bytes", self.data_scanned_bytes),
            ("engine_execution_ms", self.engine_execution_ms),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"{name} must be None or non-negative")
        if not isinstance(self.cancellation_requested, bool):
            raise ValueError("cancellation_requested must be boolean")


@dataclass
class _ExecutionTracker:
    query_execution_id: str | None = None
    cancellation_requested: bool = False
    start_attempted: bool = False


async def _finish_shielded_task(task: asyncio.Task[Any]) -> Any:
    """Wait for an owned thread/callback task despite caller cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class AthenaClientFactory(Protocol):
    def __call__(
        self,
        datasource: AthenaDatasource,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
    ) -> Any: ...


def _session_identity(
    tenant_id: str,
    project_id: str,
    principal_id: str,
    request_id: str,
) -> str:
    digest = hashlib.sha256(
        "\x00".join(
            (tenant_id, project_id, principal_id, request_id)
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"axon-query-{digest}"


def _principal_tag(principal_id: str) -> str:
    """Return a bounded, non-identifying STS tag value."""
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()


def _client_request_token(
    query: ValidatedQuery,
    datasource: AthenaDatasource,
    *,
    tenant_id: str,
    project_id: str,
    principal_id: str,
    request_id: str,
) -> str:
    """Make exact client retries idempotent without exposing identity."""
    material = "\x00".join(
        (
            tenant_id,
            project_id,
            principal_id,
            request_id,
            datasource.datasource_id,
            datasource.role_arn,
            datasource.region,
            datasource.catalog,
            datasource.database,
            datasource.workgroup,
            query.sha256,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _compact_json_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _valid_s3_output_location(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "s3"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and port is None
        and not parsed.query
        and not parsed.fragment
    )


def _non_negative_statistic(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise AthenaExecutionError(
            "athena_status_failed",
            f"Athena returned an invalid {name}.",
        )
    return value


def _termination_from_execution(
    execution: object,
    execution_id: str,
    *,
    cancellation_requested: bool = False,
) -> AthenaQueryTermination:
    if not isinstance(execution, dict):
        raise AthenaExecutionError(
            "athena_status_failed",
            "Athena returned an invalid query status.",
            query_execution_id=execution_id,
            cancellation_requested=cancellation_requested,
        )
    status = execution.get("Status")
    if not isinstance(status, dict):
        raise AthenaExecutionError(
            "athena_status_failed",
            "Athena returned an invalid query status.",
            query_execution_id=execution_id,
            cancellation_requested=cancellation_requested,
        )
    state = status.get("State")
    if (
        not isinstance(state, str)
        or state not in _TERMINAL_STATES | _ACTIVE_STATES
    ):
        raise AthenaExecutionError(
            "athena_status_failed",
            "Athena returned an invalid query status.",
            query_execution_id=execution_id,
            cancellation_requested=cancellation_requested,
        )

    scanned: int | None = None
    engine_execution_ms: int | None = None
    statistics = execution.get("Statistics")
    if statistics is not None:
        if not isinstance(statistics, dict):
            raise AthenaExecutionError(
                "athena_status_failed",
                "Athena returned invalid query statistics.",
                query_execution_id=execution_id,
                athena_state=state,
                cancellation_requested=cancellation_requested,
            )
        try:
            scanned = _non_negative_statistic(
                statistics.get("DataScannedInBytes", 0),
                "scan statistic",
            )
        except AthenaExecutionError as exc:
            exc.query_execution_id = execution_id
            exc.athena_state = state
            exc.cancellation_requested = cancellation_requested
            raise
        try:
            engine_execution_ms = _non_negative_statistic(
                statistics.get("EngineExecutionTimeInMillis", 0),
                "execution time statistic",
            )
        except AthenaExecutionError as exc:
            exc.query_execution_id = execution_id
            exc.athena_state = state
            exc.data_scanned_bytes = scanned
            exc.cancellation_requested = cancellation_requested
            raise
    return AthenaQueryTermination(
        query_execution_id=execution_id,
        state=state,
        terminal=state in _TERMINAL_STATES,
        data_scanned_bytes=scanned,
        engine_execution_ms=engine_execution_ms,
        cancellation_requested=cancellation_requested,
    )


class BotoAthenaClientFactory:
    """Assume the datasource role with non-authoritative audit tags."""

    def __init__(self, boto3_session: Any | None = None) -> None:
        self._session = boto3_session

    def __call__(
        self,
        datasource: AthenaDatasource,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
    ) -> Any:
        import boto3

        session = self._session or boto3.Session(
            region_name=datasource.region
        )
        sts = session.client(
            "sts",
            region_name=datasource.region,
            config=AWS_CLIENT_CONFIG,
        )
        identity = _session_identity(
            tenant_id,
            project_id,
            principal_id,
            request_id,
        )
        response = sts.assume_role(
            RoleArn=datasource.role_arn,
            RoleSessionName=identity,
            SourceIdentity=identity,
            DurationSeconds=900,
            Tags=[
                {"Key": "AxonTenant", "Value": tenant_id},
                {"Key": "AxonProject", "Value": project_id},
                {
                    "Key": "AxonPrincipal",
                    "Value": _principal_tag(principal_id),
                },
            ],
        )
        credentials = response.get("Credentials")
        if not isinstance(credentials, dict):
            raise RuntimeError("STS returned no credentials")
        assumed = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=datasource.region,
        )
        return assumed.client(
            "athena",
            region_name=datasource.region,
            config=AWS_CLIENT_CONFIG,
        )


class AthenaExecutor:
    """Run one validated query with strict workgroup and result bounds."""

    def __init__(
        self,
        *,
        client_factory: AthenaClientFactory | None = None,
        limits: AthenaQueryLimits | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._client_factory = (
            client_factory or BotoAthenaClientFactory()
        )
        self.limits = limits or AthenaQueryLimits()
        self._sleep = sleep

    async def check_ready(
        self,
        datasource: AthenaDatasource,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
    ) -> bool:
        try:
            client = await asyncio.to_thread(
                self._client_factory,
                datasource,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                request_id=request_id,
            )
            await self._validate_workgroup(client, datasource)
            return True
        except Exception:
            logger.warning(
                "Athena readiness failed for datasource %s",
                datasource.datasource_id,
                exc_info=True,
            )
            return False

    async def _validate_workgroup(
        self,
        client: Any,
        datasource: AthenaDatasource,
    ) -> None:
        try:
            response = await asyncio.to_thread(
                client.get_work_group,
                WorkGroup=datasource.workgroup,
            )
        except Exception as exc:
            raise AthenaExecutionError(
                "athena_workgroup_unavailable",
                "Athena workgroup validation failed.",
            ) from exc
        workgroup = response.get("WorkGroup")
        if not isinstance(workgroup, dict):
            raise AthenaExecutionError(
                "athena_workgroup_invalid",
                "Athena workgroup configuration is invalid.",
            )
        configuration = workgroup.get("Configuration")
        if (
            workgroup.get("State") != "ENABLED"
            or not isinstance(configuration, dict)
            or configuration.get("EnforceWorkGroupConfiguration") is not True
            or configuration.get("PublishCloudWatchMetricsEnabled")
            is not True
        ):
            raise AthenaExecutionError(
                "athena_workgroup_unsafe",
                "Athena workgroup must be enabled and enforce its configuration.",
            )
        result_configuration = configuration.get("ResultConfiguration")
        if not isinstance(result_configuration, dict):
            raise AthenaExecutionError(
                "athena_workgroup_unsafe",
                "Athena workgroup has no enforced result configuration.",
            )
        output_location = result_configuration.get("OutputLocation")
        encryption = result_configuration.get(
            "EncryptionConfiguration"
        )
        if (
            not _valid_s3_output_location(output_location)
            or not isinstance(encryption, dict)
            or encryption.get("EncryptionOption")
            not in _ENCRYPTED_OUTPUT_OPTIONS
            or not isinstance(encryption.get("KmsKey"), str)
            or not encryption["KmsKey"]
        ):
            raise AthenaExecutionError(
                "athena_workgroup_unsafe",
                "Athena results must use an enforced KMS-encrypted S3 location.",
            )
        cutoff = configuration.get("BytesScannedCutoffPerQuery")
        if (
            isinstance(cutoff, bool)
            or not isinstance(cutoff, int)
            or cutoff <= 0
            or cutoff > self.limits.max_bytes_scanned
        ):
            raise AthenaExecutionError(
                "athena_workgroup_unsafe",
                "Athena workgroup scan cutoff is missing or exceeds the AxonLLM limit.",
            )

    async def execute(
        self,
        query: ValidatedQuery,
        datasource: AthenaDatasource,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
        max_rows: int | None = None,
        on_started: Callable[[str], Awaitable[None]] | None = None,
    ) -> AthenaQueryResult:
        tracker = _ExecutionTracker()
        try:
            return await asyncio.wait_for(
                self._execute_bounded(
                    query,
                    datasource,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    principal_id=principal_id,
                    request_id=request_id,
                    max_rows=max_rows,
                    on_started=on_started,
                    tracker=tracker,
                ),
                timeout=self.limits.timeout_seconds,
            )
        except TimeoutError as exc:
            raise AthenaExecutionError(
                "athena_query_timeout",
                "Athena query exceeded its execution deadline.",
                query_execution_id=tracker.query_execution_id,
                cancellation_requested=tracker.cancellation_requested,
                execution_may_have_started=tracker.start_attempted,
            ) from exc

    async def cancel(
        self,
        datasource: AthenaDatasource,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
        execution_id: str,
    ) -> AthenaQueryTermination:
        """Idempotently request cancellation and observe billable final state."""
        if (
            datasource.tenant_id != tenant_id
            or datasource.project_id != project_id
        ):
            raise AthenaExecutionError(
                "datasource_identity_mismatch",
                "Datasource ownership could not be verified.",
                query_execution_id=execution_id,
            )
        if not isinstance(execution_id, str) or not execution_id:
            raise AthenaExecutionError(
                "athena_status_failed",
                "Athena query execution identifier is invalid.",
            )
        try:
            client = await asyncio.to_thread(
                self._client_factory,
                datasource,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                request_id=request_id,
            )
        except Exception as exc:
            raise AthenaExecutionError(
                "athena_role_unavailable",
                "The project query role could not be assumed.",
                query_execution_id=execution_id,
            ) from exc

        execution = await self._get_query_execution(client, execution_id)
        termination = _termination_from_execution(execution, execution_id)
        if termination.terminal:
            return termination

        cancellation_requested = False
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    client.stop_query_execution,
                    QueryExecutionId=execution_id,
                ),
                timeout=_CANCELLATION_TIMEOUT_SECONDS,
            )
            cancellation_requested = True
        except Exception:
            logger.warning(
                "Could not stop Athena query %s",
                execution_id,
                exc_info=True,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CANCELLATION_TIMEOUT_SECONDS
        termination = replace(
            termination,
            cancellation_requested=cancellation_requested,
        )
        while not termination.terminal:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return termination
            await self._sleep(
                min(self.limits.poll_interval_seconds, remaining)
            )
            execution = await self._get_query_execution(
                client,
                execution_id,
            )
            termination = _termination_from_execution(
                execution,
                execution_id,
                cancellation_requested=cancellation_requested,
            )
        return termination

    async def _execute_bounded(
        self,
        query: ValidatedQuery,
        datasource: AthenaDatasource,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
        max_rows: int | None = None,
        on_started: Callable[[str], Awaitable[None]] | None = None,
        tracker: _ExecutionTracker | None = None,
    ) -> AthenaQueryResult:
        if (
            datasource.tenant_id != tenant_id
            or datasource.project_id != project_id
        ):
            raise AthenaExecutionError(
                "datasource_identity_mismatch",
                "Datasource ownership could not be verified.",
            )
        row_limit = self.limits.max_rows if max_rows is None else max_rows
        if (
            isinstance(row_limit, bool)
            or not isinstance(row_limit, int)
            or not 1 <= row_limit <= self.limits.max_rows
        ):
            raise AthenaExecutionError(
                "invalid_query_limit",
                f"max_rows must be between 1 and {self.limits.max_rows}.",
            )
        try:
            client = await asyncio.to_thread(
                self._client_factory,
                datasource,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                request_id=request_id,
            )
        except Exception as exc:
            raise AthenaExecutionError(
                "athena_role_unavailable",
                "The project query role could not be assumed.",
            ) from exc
        await self._validate_workgroup(client, datasource)

        execution_id: str | None = None
        terminal = False
        try:
            start_task: asyncio.Task[Any] | None = None
            try:
                if tracker is not None:
                    tracker.start_attempted = True
                start_task = asyncio.create_task(
                    asyncio.to_thread(
                        client.start_query_execution,
                        QueryString=query.sql,
                        QueryExecutionContext={
                            "Catalog": datasource.catalog,
                            "Database": datasource.database,
                        },
                        WorkGroup=datasource.workgroup,
                        ClientRequestToken=_client_request_token(
                            query,
                            datasource,
                            tenant_id=tenant_id,
                            project_id=project_id,
                            principal_id=principal_id,
                            request_id=request_id,
                        ),
                    )
                )
                try:
                    started = await asyncio.shield(start_task)
                except asyncio.CancelledError:
                    # The SDK thread cannot be cancelled. Keep ownership until it
                    # returns so a created execution is identified, persisted,
                    # and stopped before cancellation leaves this method.
                    try:
                        started = await _finish_shielded_task(start_task)
                        execution_id = (
                            started.get("QueryExecutionId")
                            if isinstance(started, dict)
                            else None
                        )
                        if (
                            isinstance(execution_id, str)
                            and execution_id
                        ):
                            if tracker is not None:
                                tracker.query_execution_id = execution_id
                            if on_started is not None:
                                callback_task = asyncio.create_task(
                                    on_started(execution_id)
                                )
                                await _finish_shielded_task(callback_task)
                    except Exception:
                        logger.warning(
                            "Athena start result could not be captured during "
                            "cancellation",
                            exc_info=True,
                        )
                    raise
            except Exception as exc:
                raise AthenaExecutionError(
                    "athena_start_failed",
                    "Athena rejected the query execution request.",
                    execution_may_have_started=True,
                ) from exc
            execution_id = (
                started.get("QueryExecutionId")
                if isinstance(started, dict)
                else None
            )
            if not isinstance(execution_id, str) or not execution_id:
                raise AthenaExecutionError(
                    "athena_start_failed",
                    "Athena returned no query execution identifier.",
                    execution_may_have_started=True,
                )
            if tracker is not None:
                tracker.query_execution_id = execution_id
            if on_started is not None:
                await on_started(execution_id)

            execution = await self._wait_for_query(client, execution_id)
            terminal = True
            termination = _termination_from_execution(
                execution,
                execution_id,
            )
            if termination.data_scanned_bytes is None:
                raise AthenaExecutionError(
                    "athena_status_failed",
                    "Athena returned invalid query statistics.",
                ).attach_termination(termination)
            if termination.engine_execution_ms is None:
                raise AthenaExecutionError(
                    "athena_status_failed",
                    "Athena returned invalid query statistics.",
                ).attach_termination(termination)
            if termination.state != "SUCCEEDED":
                logger.warning(
                    "Athena query ended state=%s execution_id=%s",
                    termination.state,
                    execution_id,
                )
                code = (
                    "athena_query_cancelled"
                    if termination.state == "CANCELLED"
                    else "athena_query_failed"
                )
                raise AthenaExecutionError(
                    code,
                    "Athena did not complete the query successfully.",
                ).attach_termination(termination)
            scanned = termination.data_scanned_bytes
            engine_execution_ms = termination.engine_execution_ms
            if scanned > self.limits.max_bytes_scanned:
                raise AthenaExecutionError(
                    "athena_scan_limit_exceeded",
                    "Athena query exceeded the configured scan limit.",
                ).attach_termination(termination)
            try:
                (
                    columns,
                    rows,
                    truncated,
                    result_bytes,
                ) = await self._read_results(
                    client,
                    execution_id,
                    max_rows=row_limit,
                )
            except AthenaExecutionError as exc:
                raise exc.attach_termination(termination)
            return AthenaQueryResult(
                query_execution_id=execution_id,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                data_scanned_bytes=scanned,
                engine_execution_ms=engine_execution_ms,
                result_bytes=result_bytes,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if execution_id is not None and not terminal:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            client.stop_query_execution,
                            QueryExecutionId=execution_id,
                        ),
                        timeout=_CANCELLATION_TIMEOUT_SECONDS,
                    )
                    if tracker is not None:
                        tracker.cancellation_requested = True
                except Exception:
                    logger.warning(
                        "Could not stop Athena query %s",
                        execution_id,
                        exc_info=True,
                    )

    async def _get_query_execution(
        self,
        client: Any,
        execution_id: str,
    ) -> dict:
        try:
            response = await asyncio.to_thread(
                client.get_query_execution,
                QueryExecutionId=execution_id,
            )
        except Exception as exc:
            raise AthenaExecutionError(
                "athena_status_failed",
                "Athena query status is unavailable.",
                query_execution_id=execution_id,
            ) from exc
        execution = (
            response.get("QueryExecution")
            if isinstance(response, dict)
            else None
        )
        if not isinstance(execution, dict):
            raise AthenaExecutionError(
                "athena_status_failed",
                "Athena returned an invalid query status.",
                query_execution_id=execution_id,
            )
        return execution

    async def _wait_for_query(
        self,
        client: Any,
        execution_id: str,
    ) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.timeout_seconds
        while True:
            execution = await self._get_query_execution(
                client,
                execution_id,
            )
            termination = _termination_from_execution(
                execution,
                execution_id,
            )
            if termination.terminal:
                return execution
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AthenaExecutionError(
                    "athena_query_timeout",
                    "Athena query exceeded its execution deadline.",
                )
            await self._sleep(
                min(self.limits.poll_interval_seconds, remaining)
            )

    async def _read_results(
        self,
        client: Any,
        execution_id: str,
        *,
        max_rows: int,
    ) -> tuple[
        tuple[dict[str, str], ...],
        tuple[tuple[str | None, ...], ...],
        bool,
        int,
    ]:
        token: str | None = None
        columns: tuple[dict[str, str], ...] = ()
        rows: list[tuple[str | None, ...]] = []
        result_bytes = 0
        first_row = True
        truncated = False
        seen_tokens: set[str] = set()
        while True:
            kwargs: dict[str, Any] = {
                "QueryExecutionId": execution_id,
                "MaxResults": min(
                    _MAX_RESULT_PAGE_ROWS,
                    max_rows + 2,
                ),
            }
            if token is not None:
                kwargs["NextToken"] = token
            try:
                response = await asyncio.to_thread(
                    client.get_query_results,
                    **kwargs,
                )
            except Exception as exc:
                raise AthenaExecutionError(
                    "athena_results_failed",
                    "Athena query results are unavailable.",
                ) from exc
            result_set = (
                response.get("ResultSet")
                if isinstance(response, dict)
                else None
            )
            if not isinstance(result_set, dict):
                raise AthenaExecutionError(
                    "athena_results_failed",
                    "Athena returned an invalid result set.",
                )
            metadata = result_set.get("ResultSetMetadata", {})
            raw_columns = metadata.get("ColumnInfo", [])
            if (
                not isinstance(raw_columns, list)
                or not raw_columns
                or len(raw_columns) > _MAX_ATHENA_COLUMNS
            ):
                raise AthenaExecutionError(
                    "athena_results_failed",
                    "Athena returned invalid column metadata.",
                )
            page_columns: list[dict[str, str]] = []
            for column in raw_columns:
                if not isinstance(column, dict):
                    raise AthenaExecutionError(
                        "athena_results_failed",
                        "Athena returned invalid column metadata.",
                    )
                name = column.get("Name")
                column_type = column.get("Type")
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name.encode("utf-8"))
                    > _MAX_COLUMN_METADATA_BYTES
                    or not isinstance(column_type, str)
                    or not column_type
                    or len(column_type.encode("utf-8"))
                    > _MAX_COLUMN_METADATA_BYTES
                ):
                    raise AthenaExecutionError(
                        "athena_results_failed",
                        "Athena returned invalid column metadata.",
                    )
                page_columns.append(
                    {"name": name, "type": column_type}
                )
            normalized_columns = tuple(page_columns)
            if not columns:
                columns = normalized_columns
                result_bytes = _compact_json_bytes(
                    {
                        "columns": [dict(column) for column in columns],
                        "rows": [],
                    }
                )
                if result_bytes > self.limits.max_result_bytes:
                    raise AthenaExecutionError(
                        "athena_results_failed",
                        "Athena column metadata exceeds the result limit.",
                    )
            elif normalized_columns != columns:
                raise AthenaExecutionError(
                    "athena_results_failed",
                    "Athena changed column metadata between result pages.",
                )
            raw_rows = result_set.get("Rows", [])
            if not isinstance(raw_rows, list):
                raise AthenaExecutionError(
                    "athena_results_failed",
                    "Athena returned invalid rows.",
                )
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    raise AthenaExecutionError(
                        "athena_results_failed",
                        "Athena returned an invalid row.",
                    )
                data = raw_row.get("Data", [])
                if not isinstance(data, list):
                    raise AthenaExecutionError(
                        "athena_results_failed",
                        "Athena returned invalid row data.",
                    )
                values: list[str | None] = []
                for field in data:
                    if not isinstance(field, dict):
                        raise AthenaExecutionError(
                            "athena_results_failed",
                            "Athena returned invalid row data.",
                        )
                    value = field.get("VarCharValue")
                    if value is not None and not isinstance(value, str):
                        raise AthenaExecutionError(
                            "athena_results_failed",
                            "Athena returned invalid row data.",
                        )
                    values.append(value)
                row = tuple(values)
                if len(row) < len(columns):
                    row += (None,) * (len(columns) - len(row))
                if len(row) > len(columns):
                    raise AthenaExecutionError(
                        "athena_results_failed",
                        "Athena returned too many fields in a row.",
                    )
                if first_row:
                    first_row = False
                    if row == tuple(
                        column["name"] for column in columns
                    ):
                        continue
                row_bytes = _compact_json_bytes(row)
                if rows:
                    row_bytes += 1
                if (
                    len(rows) >= max_rows
                    or result_bytes + row_bytes
                    > self.limits.max_result_bytes
                ):
                    truncated = True
                    return (
                        columns,
                        tuple(rows),
                        truncated,
                        result_bytes,
                    )
                rows.append(row)
                result_bytes += row_bytes
            token = response.get("NextToken")
            if token is None:
                return (
                    columns,
                    tuple(rows),
                    truncated,
                    result_bytes,
                )
            if not isinstance(token, str) or not token:
                raise AthenaExecutionError(
                    "athena_results_failed",
                    "Athena returned an invalid pagination token.",
                )
            if token in seen_tokens:
                raise AthenaExecutionError(
                    "athena_results_failed",
                    "Athena repeated a pagination token.",
                )
            seen_tokens.add(token)
