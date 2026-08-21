"""Durable asynchronous exports for the serverless control plane."""

from __future__ import annotations

import asyncio
import base64
import csv
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping, Protocol
import uuid

from src.gateway.security.audit_trail import LEGACY_TENANT_ID


logger = logging.getLogger(__name__)

EXPORT_JOB_SCHEMA = "axonllm.export-job/v1"
EXPORT_MESSAGE_SCHEMA = "axonllm.export-message/v1"
EXPORT_RESULT_SCHEMA = "axonllm.export-result/v1"
EXPORT_TTL_SECONDS = 24 * 60 * 60
EXPORT_DOWNLOAD_SECONDS = 60
EXPORT_MAX_ATTEMPTS = 3
EXPORT_LEASE_SECONDS = 14 * 60
_JOB_ID = re.compile(r"^exp_[0-9a-f]{32}$")
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_FILTER_KEYS = frozenset(
    {
        "end_time",
        "model",
        "project_id",
        "provider",
        "start_time",
        "user_id",
    }
)
_ALWAYS_REDACT_KEYS = (
    "secret",
    "token",
    "password",
    "api_key",
    "credential",
    "authorization",
    "cookie",
    "header",
    "exception",
    "traceback",
    "stack_trace",
    "raw_error",
    "error",
)
_READER_REDACT_KEYS = (
    "url",
    "uri",
    "endpoint",
    "topic",
    "log_group",
    "log_stream",
    "log_target",
    "destination",
    "target",
    "source_ip",
)
USAGE_RECORD_COLUMNS = (
    "request_id",
    "timestamp",
    "project_id",
    "user_id",
    "provider",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "cost",
    "latency_ms",
    "status",
    "routing_strategy",
)
USAGE_BREAKDOWN_COLUMNS = (
    "group_by",
    "group_key",
    "requests",
    "tokens",
    "cost",
)


class ExportKind(StrEnum):
    """Supported report families."""

    USAGE = "usage"
    AUDIT = "audit"


class ExportFormat(StrEnum):
    """Supported serialized report formats."""

    CSV = "csv"
    JSON = "json"


class ExportLevel(StrEnum):
    """Supported usage report granularities."""

    RECORDS = "records"
    BREAKDOWN = "breakdown"


class ExportStatus(StrEnum):
    """Durable export lifecycle."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"


class ExportJobError(RuntimeError):
    """Base exception for durable export operations."""


class ExportJobNotFound(ExportJobError):
    """The caller cannot observe the requested export."""


class ExportJobNotReady(ExportJobError):
    """The export has not produced a downloadable object."""


class ExportJobLeaseLost(ExportJobError):
    """A stale worker attempted to update a reclaimed export."""


@dataclass(frozen=True)
class ExportJob:
    """One tenant- and requester-bound export request."""

    job_id: str
    tenant_id: str
    requested_by: str
    kind: ExportKind
    format: ExportFormat
    level: ExportLevel
    filters: tuple[tuple[str, str], ...]
    restricted: bool
    status: ExportStatus
    created_at: datetime
    expires_at: datetime
    attempt_count: int = 0
    claim_token: str = ""
    lease_expires_at: int = 0
    object_key: str = ""
    filename: str = ""
    content_type: str = ""
    content_sha256: str = ""
    content_length: int = 0
    row_count: int = 0
    error_code: str = ""

    def __post_init__(self) -> None:
        if _JOB_ID.fullmatch(self.job_id) is None:
            raise ValueError("export job_id is invalid")
        for label, value in (
            ("tenant_id", self.tenant_id),
            ("requested_by", self.requested_by),
        ):
            if _SAFE_TEXT.fullmatch(value) is None or value != value.strip():
                raise ValueError(f"export {label} is invalid")
        if self.kind is ExportKind.AUDIT and (
            self.format is not ExportFormat.JSON or self.level is not ExportLevel.RECORDS
        ):
            raise ValueError("audit exports require JSON records")
        if any(
            key not in _FILTER_KEYS or not isinstance(value, str) or not value or value != value.strip()
            for key, value in self.filters
        ):
            raise ValueError("export filters are invalid")
        if len({key for key, _ in self.filters}) != len(self.filters):
            raise ValueError("export filters must not contain duplicates")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("export timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("export expiry must follow creation")
        if self.attempt_count < 0 or self.row_count < 0:
            raise ValueError("export counters must be non-negative")
        if self.content_length < 0:
            raise ValueError("export content length must be non-negative")
        if (
            self.content_sha256
            and re.fullmatch(
                r"[0-9a-f]{64}",
                self.content_sha256,
            )
            is None
        ):
            raise ValueError("export content hash is invalid")

    @property
    def filter_map(self) -> dict[str, str]:
        return dict(self.filters)

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


@dataclass(frozen=True)
class RenderedExport:
    """A completed local artifact awaiting private-object upload."""

    path: Path
    filename: str
    content_type: str
    content_sha256: str
    content_length: int
    row_count: int


class ExportJobStore(Protocol):
    """Durable job-state operations used by API and worker hosts."""

    async def create(self, job: ExportJob) -> None: ...

    async def get(
        self,
        tenant_id: str,
        job_id: str,
    ) -> ExportJob | None: ...

    async def fail_queued(
        self,
        job: ExportJob,
        *,
        error_code: str,
    ) -> None: ...

    async def claim(
        self,
        tenant_id: str,
        job_id: str,
    ) -> ExportJob | None: ...

    async def complete(
        self,
        job: ExportJob,
        rendered: RenderedExport,
        *,
        object_key: str,
    ) -> None: ...

    async def release_or_fail(
        self,
        job: ExportJob,
        *,
        error_code: str,
        final: bool,
    ) -> None: ...


class ExportJobQueue(Protocol):
    """Queue boundary used by the request-driven control API."""

    async def enqueue(self, job: ExportJob) -> None: ...


class ExportObjectStore(Protocol):
    """Private object boundary shared by API and worker hosts."""

    async def upload(
        self,
        job: ExportJob,
        rendered: RenderedExport,
    ) -> str: ...

    async def download_url(self, job: ExportJob) -> str: ...


class ExportDataReader(Protocol):
    """Page-oriented report source used only by the export worker."""

    def usage_records(self, job: ExportJob) -> Iterable[object]: ...

    def audit_records(self, job: ExportJob) -> Iterable[Mapping[str, object]]: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_filters(
    filters: Mapping[str, str | None],
) -> tuple[tuple[str, str], ...]:
    normalized = tuple(sorted((key, value) for key, value in filters.items() if value is not None and value != ""))
    if any(
        key not in _FILTER_KEYS or _SAFE_TEXT.fullmatch(value) is None or value != value.strip()
        for key, value in normalized
    ):
        raise ValueError("export filter is not supported")
    return normalized


def _normalized_usage_filters(
    filters: Mapping[str, str | None],
) -> tuple[tuple[str, str], ...]:
    normalized = dict(_normalized_filters(filters))
    for key in ("start_time", "end_time"):
        raw = normalized.get(key)
        if raw is None:
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"export {key} is invalid") from exc
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        normalized[key] = value.isoformat()
    start = normalized.get("start_time")
    end = normalized.get("end_time")
    if start is not None and end is not None and datetime.fromisoformat(start) > datetime.fromisoformat(end):
        raise ValueError("export start_time must not follow end_time")
    return tuple(sorted(normalized.items()))


def _job_partition(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def _job_sort(job_id: str) -> str:
    return f"EXPORT#{job_id}"


def _job_object_key(job: ExportJob) -> str:
    tenant_hash = hashlib.sha256(job.tenant_id.encode("utf-8")).hexdigest()
    return f"exports/{tenant_hash[:32]}/{job.job_id}/{job.filename}"


def _requester(request: object) -> str:
    state = getattr(request, "state", None)
    context = getattr(state, "context", None)
    for attribute in ("principal_id", "user_id"):
        value = getattr(context, attribute, None)
        if isinstance(value, str) and value and value != "anonymous" and value == value.strip():
            return value
    raise ExportJobError("authenticated export requester is missing")


def request_export_identity(request: object) -> str:
    """Return the authenticated canonical requester for route adapters."""

    return _requester(request)


def usage_record_export_row(record: object) -> dict[str, object]:
    """Serialize one usage record using the established chargeback columns."""

    timestamp = getattr(record, "timestamp", None)
    return {
        "request_id": getattr(record, "request_id", ""),
        "timestamp": timestamp.isoformat() if timestamp is not None else "",
        "project_id": getattr(record, "project_id", ""),
        "user_id": getattr(record, "user_id", ""),
        "provider": getattr(record, "provider", ""),
        "model": getattr(record, "model", ""),
        "prompt_tokens": getattr(record, "prompt_tokens", 0),
        "completion_tokens": getattr(record, "completion_tokens", 0),
        "total_tokens": getattr(record, "total_tokens", 0),
        "cached_tokens": getattr(record, "cached_tokens", 0),
        "cost": getattr(record, "cost", 0),
        "latency_ms": getattr(record, "latency_ms", 0),
        "status": getattr(record, "status", ""),
        "routing_strategy": getattr(record, "routing_strategy", ""),
    }


def redact_audit_value(
    value: object,
    *,
    restricted: bool,
    key: str = "",
) -> object:
    key_lower = key.lower()
    if any(part in key_lower for part in _ALWAYS_REDACT_KEYS):
        return "[REDACTED]"
    if restricted and any(part in key_lower for part in _READER_REDACT_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(child_key): redact_audit_value(
                child_value,
                restricted=restricted,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_audit_value(item, restricted=restricted) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if lowered.startswith(("http://", "https://")):
            return "[REDACTED_URL]"
        if lowered.startswith(("bearer ", "basic ")):
            return "[REDACTED]"
    return value


def serialize_audit_export_row(
    row: Mapping[str, object],
    *,
    restricted: bool,
) -> dict[str, object]:
    """Normalize and redact one durable audit row for export."""

    output = dict(row)
    raw_data = output.get("data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except (TypeError, ValueError):
            raw_data = {}
    output["data"] = redact_audit_value(
        raw_data or {},
        restricted=restricted,
    )
    for internal in ("PK", "SK", "entity_type"):
        output.pop(internal, None)
    return output


def _safe_csv_value(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _matches_usage_filters(
    record: object,
    filters: Mapping[str, str],
) -> bool:
    for key in ("provider", "model", "project_id", "user_id"):
        expected = filters.get(key)
        if expected is not None and getattr(record, key, None) != expected:
            return False
    timestamp = getattr(record, "timestamp", None)
    if timestamp is None:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    for key, operator in (
        ("start_time", lambda value, bound: value >= bound),
        ("end_time", lambda value, bound: value <= bound),
    ):
        raw = filters.get(key)
        if raw is None:
            continue
        try:
            bound = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"export {key} is invalid") from exc
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=timezone.utc)
        if not operator(timestamp, bound):
            return False
    return True


def _usage_breakdown(
    records: Iterable[object],
) -> tuple[list[dict[str, object]], int]:
    groups: dict[tuple[str, str], dict[str, object]] = {}
    count = 0
    for record in records:
        count += 1
        for group_by, group_key in (
            ("provider", getattr(record, "provider", "")),
            ("model", getattr(record, "model", "")),
            ("project", getattr(record, "project_id", "")),
            ("user", getattr(record, "user_id", "")),
        ):
            key = (group_by, str(group_key))
            group = groups.setdefault(
                key,
                {
                    "group_by": group_by,
                    "group_key": str(group_key),
                    "requests": 0,
                    "tokens": 0,
                    "cost": 0.0,
                },
            )
            group["requests"] = int(group["requests"]) + 1
            group["tokens"] = int(group["tokens"]) + int(getattr(record, "total_tokens", 0))
            group["cost"] = float(group["cost"]) + float(getattr(record, "cost", 0.0))
    return [groups[key] for key in sorted(groups)], count


class ExportRenderer:
    """Create bounded local artifacts from page-oriented durable state."""

    def __init__(self, reader: ExportDataReader) -> None:
        self._reader = reader

    def render(self, job: ExportJob) -> RenderedExport:
        suffix = ".csv" if job.format is ExportFormat.CSV else ".json"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f"{job.job_id}-",
            suffix=suffix,
            delete=False,
        ) as output:
            path = Path(output.name)
            if job.kind is ExportKind.USAGE:
                row_count = self._render_usage(output, job)
            else:
                row_count = self._render_audit(output, job)
        content_sha256 = hashlib.sha256()
        content_length = 0
        with path.open("rb") as content:
            for chunk in iter(lambda: content.read(1024 * 1024), b""):
                content_sha256.update(chunk)
                content_length += len(chunk)
        return RenderedExport(
            path=path,
            filename=job.filename,
            content_type=job.content_type,
            content_sha256=content_sha256.hexdigest(),
            content_length=content_length,
            row_count=row_count,
        )

    def _filtered_usage(self, job: ExportJob) -> Iterable[object]:
        for record in self._reader.usage_records(job):
            if _matches_usage_filters(record, job.filter_map):
                yield record

    def _render_usage(self, output, job: ExportJob) -> int:
        records = self._filtered_usage(job)
        if job.level is ExportLevel.BREAKDOWN:
            rows, source_count = _usage_breakdown(records)
            columns = USAGE_BREAKDOWN_COLUMNS
        else:
            rows = (usage_record_export_row(record) for record in records)
            source_count = -1
            columns = USAGE_RECORD_COLUMNS

        if job.format is ExportFormat.CSV:
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            count = 0
            for row in rows:
                writer.writerow({key: _safe_csv_value(value) for key, value in row.items()})
                count += 1
            return source_count if source_count >= 0 else count

        output.write(
            json.dumps(
                {"level": job.level.value},
                separators=(",", ":"),
                sort_keys=True,
            )[:-1]
        )
        output.write(',"rows":[')
        count = 0
        for row in rows:
            if count:
                output.write(",")
            json.dump(
                row,
                output,
                separators=(",", ":"),
                sort_keys=True,
            )
            count += 1
        output.write("]}")
        return source_count if source_count >= 0 else count

    def _render_audit(self, output, job: ExportJob) -> int:
        output.write(
            json.dumps(
                {"tenant_id": job.tenant_id},
                separators=(",", ":"),
                sort_keys=True,
            )[:-1]
        )
        output.write(',"records":[')
        count = 0
        for row in self._reader.audit_records(job):
            if count:
                output.write(",")
            json.dump(
                serialize_audit_export_row(
                    row,
                    restricted=job.restricted,
                ),
                output,
                separators=(",", ":"),
                sort_keys=True,
            )
            count += 1
        output.write(f'],"count":{count}}}')
        return count


class ExportJobService:
    """Request-driven API for creating and reading durable exports."""

    def __init__(
        self,
        *,
        store: ExportJobStore,
        queue: ExportJobQueue,
        objects: ExportObjectStore,
    ) -> None:
        self._store = store
        self._queue = queue
        self._objects = objects

    async def create_usage(
        self,
        *,
        tenant_id: str,
        requested_by: str,
        format: str,
        level: str,
        filters: Mapping[str, str | None],
    ) -> ExportJob:
        try:
            export_format = ExportFormat(format)
            export_level = ExportLevel(level)
        except ValueError as exc:
            raise ValueError("usage export format or level is invalid") from exc
        return await self._create(
            tenant_id=tenant_id,
            requested_by=requested_by,
            kind=ExportKind.USAGE,
            format=export_format,
            level=export_level,
            filters=_normalized_usage_filters(filters),
            restricted=False,
        )

    async def create_audit(
        self,
        *,
        tenant_id: str,
        requested_by: str,
        project_id: str | None,
        restricted: bool,
    ) -> ExportJob:
        return await self._create(
            tenant_id=tenant_id,
            requested_by=requested_by,
            kind=ExportKind.AUDIT,
            format=ExportFormat.JSON,
            level=ExportLevel.RECORDS,
            filters=_normalized_filters({"project_id": project_id}),
            restricted=restricted,
        )

    async def _create(
        self,
        *,
        tenant_id: str,
        requested_by: str,
        kind: ExportKind,
        format: ExportFormat,
        level: ExportLevel,
        filters: tuple[tuple[str, str], ...],
        restricted: bool,
    ) -> ExportJob:
        now = _utcnow()
        job_id = f"exp_{uuid.uuid4().hex}"
        filename = (
            f"axonllm-{kind.value}-{level.value}.{format.value}"
            if kind is ExportKind.USAGE
            else "axonllm-audit-records.json"
        )
        job = ExportJob(
            job_id=job_id,
            tenant_id=tenant_id,
            requested_by=requested_by,
            kind=kind,
            format=format,
            level=level,
            filters=filters,
            restricted=restricted,
            status=ExportStatus.QUEUED,
            created_at=now,
            expires_at=now + timedelta(seconds=EXPORT_TTL_SECONDS),
            filename=filename,
            content_type=("text/csv" if format is ExportFormat.CSV else "application/json"),
        )
        await self._store.create(job)
        try:
            await self._queue.enqueue(job)
        except Exception:
            await self._store.fail_queued(
                job,
                error_code="enqueue_failed",
            )
            raise ExportJobError("export could not be queued") from None
        return job

    async def get(
        self,
        *,
        tenant_id: str,
        requested_by: str,
        job_id: str,
        kind: ExportKind,
    ) -> ExportJob:
        job = await self._store.get(tenant_id, job_id)
        if job is None or job.requested_by != requested_by or job.kind is not kind or job.expired:
            raise ExportJobNotFound("export job was not found")
        return job

    async def download_url(
        self,
        *,
        tenant_id: str,
        requested_by: str,
        job_id: str,
        kind: ExportKind,
    ) -> str:
        job = await self.get(
            tenant_id=tenant_id,
            requested_by=requested_by,
            job_id=job_id,
            kind=kind,
        )
        if job.status is not ExportStatus.COMPLETE or not job.object_key:
            raise ExportJobNotReady("export job is not complete")
        return await self._objects.download_url(job)


class ExportJobWorker:
    """Claim, render, upload, and finalize one queued export."""

    def __init__(
        self,
        *,
        store: ExportJobStore,
        objects: ExportObjectStore,
        renderer: ExportRenderer,
    ) -> None:
        self._store = store
        self._objects = objects
        self._renderer = renderer

    async def process(self, body: object) -> str:
        tenant_id, job_id = _parse_export_message(body)
        job = await self._store.claim(tenant_id, job_id)
        if job is None:
            return job_id
        rendered: RenderedExport | None = None
        try:
            rendered = await asyncio.to_thread(self._renderer.render, job)
            object_key = await self._objects.upload(job, rendered)
            await self._store.complete(
                job,
                rendered,
                object_key=object_key,
            )
            return job_id
        except asyncio.CancelledError:
            await self._store.release_or_fail(
                job,
                error_code="worker_cancelled",
                final=False,
            )
            raise
        except Exception:
            final = job.attempt_count >= EXPORT_MAX_ATTEMPTS
            await self._store.release_or_fail(
                job,
                error_code=("generation_failed" if final else "retry_pending"),
                final=final,
            )
            logger.error(
                "Export job failed job_id=%s attempt=%s final=%s",
                job.job_id,
                job.attempt_count,
                final,
                exc_info=True,
            )
            if not final:
                raise
            return job_id
        finally:
            if rendered is not None:
                try:
                    rendered.path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Unable to remove local export artifact job_id=%s",
                        job.job_id,
                    )


def _parse_export_message(body: object) -> tuple[str, str]:
    if not isinstance(body, str) or not body:
        raise ValueError("export queue body is invalid")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("export queue body is invalid") from exc
    if not isinstance(parsed, dict) or set(parsed) != {
        "jobId",
        "schema",
        "tenantId",
    }:
        raise ValueError("export queue body is invalid")
    if parsed.get("schema") != EXPORT_MESSAGE_SCHEMA:
        raise ValueError("export queue schema is invalid")
    tenant_id = parsed.get("tenantId")
    job_id = parsed.get("jobId")
    if (
        not isinstance(tenant_id, str)
        or _SAFE_TEXT.fullmatch(tenant_id) is None
        or not isinstance(job_id, str)
        or _JOB_ID.fullmatch(job_id) is None
    ):
        raise ValueError("export queue identifiers are invalid")
    return tenant_id, job_id


def export_message(job: ExportJob) -> str:
    """Return the minimal immutable SQS envelope for one export."""

    return json.dumps(
        {
            "jobId": job.job_id,
            "schema": EXPORT_MESSAGE_SCHEMA,
            "tenantId": job.tenant_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def export_job_public(job: ExportJob) -> dict[str, object]:
    """Return the non-secret API representation of one job."""

    result: dict[str, object] = {
        "schema": EXPORT_RESULT_SCHEMA,
        "jobId": job.job_id,
        "kind": job.kind.value,
        "format": job.format.value,
        "level": job.level.value,
        "status": job.status.value,
        "createdAt": job.created_at.isoformat(),
        "expiresAt": job.expires_at.isoformat(),
        "attempts": job.attempt_count,
    }
    if job.status is ExportStatus.COMPLETE:
        result.update(
            {
                "contentLength": job.content_length,
                "contentSha256": job.content_sha256,
                "filename": job.filename,
                "rowCount": job.row_count,
            }
        )
    if job.status is ExportStatus.FAILED:
        result["error"] = {"type": job.error_code or "export_failed"}
    return result


def _native(value: object) -> object:
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, dict):
        return {key: _native(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_native(child) for child in value]
    return value


def _job_item(job: ExportJob) -> dict[str, object]:
    return {
        "PK": _job_partition(job.tenant_id),
        "SK": _job_sort(job.job_id),
        "entity_type": "export_job",
        "schema": EXPORT_JOB_SCHEMA,
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "requested_by": job.requested_by,
        "kind": job.kind.value,
        "format": job.format.value,
        "level": job.level.value,
        "filters": json.dumps(
            dict(job.filters),
            separators=(",", ":"),
            sort_keys=True,
        ),
        "restricted": job.restricted,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "expires_at": int(job.expires_at.timestamp()),
        "attempt_count": job.attempt_count,
        "claim_token": job.claim_token,
        "lease_expires_at": job.lease_expires_at,
        "object_key": job.object_key,
        "filename": job.filename,
        "content_type": job.content_type,
        "content_sha256": job.content_sha256,
        "content_length": job.content_length,
        "row_count": job.row_count,
        "error_code": job.error_code,
    }


def _job_from_item(item: Mapping[str, object]) -> ExportJob:
    normalized = _native(dict(item))
    filters_raw = normalized.get("filters", "{}")
    filters = json.loads(filters_raw) if isinstance(filters_raw, str) else {}
    if not isinstance(filters, dict):
        raise ValueError("stored export filters are invalid")
    expires_at = normalized.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        raise ValueError("stored export expiry is invalid")
    return ExportJob(
        job_id=str(normalized.get("job_id", "")),
        tenant_id=str(normalized.get("tenant_id", "")),
        requested_by=str(normalized.get("requested_by", "")),
        kind=ExportKind(str(normalized.get("kind", ""))),
        format=ExportFormat(str(normalized.get("format", ""))),
        level=ExportLevel(str(normalized.get("level", ""))),
        filters=tuple(sorted((str(key), str(value)) for key, value in filters.items())),
        restricted=bool(normalized.get("restricted", False)),
        status=ExportStatus(str(normalized.get("status", ""))),
        created_at=datetime.fromisoformat(str(normalized.get("created_at", ""))),
        expires_at=datetime.fromtimestamp(
            float(expires_at),
            tz=timezone.utc,
        ),
        attempt_count=int(normalized.get("attempt_count", 0)),
        claim_token=str(normalized.get("claim_token", "")),
        lease_expires_at=int(normalized.get("lease_expires_at", 0)),
        object_key=str(normalized.get("object_key", "")),
        filename=str(normalized.get("filename", "")),
        content_type=str(normalized.get("content_type", "")),
        content_sha256=str(normalized.get("content_sha256", "")),
        content_length=int(normalized.get("content_length", 0)),
        row_count=int(normalized.get("row_count", 0)),
        error_code=str(normalized.get("error_code", "")),
    )


class DynamoExportJobStore:
    """DynamoDB implementation of the export lease/state contract."""

    def __init__(
        self,
        *,
        table_name: str,
        region: str,
        table: object | None = None,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._table = table

    def _resource(self):
        if self._table is None:
            import boto3

            self._table = boto3.resource(
                "dynamodb",
                region_name=self._region,
            ).Table(self._table_name)
        return self._table

    async def create(self, job: ExportJob) -> None:
        def _put() -> None:
            self._resource().put_item(
                Item=_job_item(job),
                ConditionExpression=("attribute_not_exists(PK) AND attribute_not_exists(SK)"),
            )

        await asyncio.to_thread(_put)

    async def get(
        self,
        tenant_id: str,
        job_id: str,
    ) -> ExportJob | None:
        def _get() -> Mapping[str, object] | None:
            response = self._resource().get_item(
                Key={
                    "PK": _job_partition(tenant_id),
                    "SK": _job_sort(job_id),
                },
                ConsistentRead=True,
            )
            item = response.get("Item")
            return item if isinstance(item, dict) else None

        item = await asyncio.to_thread(_get)
        return None if item is None else _job_from_item(item)

    async def fail_queued(
        self,
        job: ExportJob,
        *,
        error_code: str,
    ) -> None:
        await self._update_status(
            job,
            status=ExportStatus.FAILED,
            error_code=error_code,
            require_claim=False,
        )

    async def claim(
        self,
        tenant_id: str,
        job_id: str,
    ) -> ExportJob | None:
        from botocore.exceptions import ClientError

        now = int(_utcnow().timestamp())
        token = uuid.uuid4().hex

        def _claim() -> Mapping[str, object]:
            response = self._resource().update_item(
                Key={
                    "PK": _job_partition(tenant_id),
                    "SK": _job_sort(job_id),
                },
                UpdateExpression=(
                    "SET #status = :processing, claim_token = :token, "
                    "lease_expires_at = :lease, error_code = :empty "
                    "ADD attempt_count :one"
                ),
                ConditionExpression=("#status = :queued OR (#status = :processing AND lease_expires_at < :now)"),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":empty": "",
                    ":lease": now + EXPORT_LEASE_SECONDS,
                    ":now": now,
                    ":one": 1,
                    ":processing": ExportStatus.PROCESSING.value,
                    ":queued": ExportStatus.QUEUED.value,
                    ":token": token,
                },
                ReturnValues="ALL_NEW",
            )
            return response["Attributes"]

        try:
            return _job_from_item(await asyncio.to_thread(_claim))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != ("ConditionalCheckFailedException"):
                raise
            existing = await self.get(tenant_id, job_id)
            if existing is None:
                raise ExportJobNotFound("queued export job was not found")
            return None

    async def complete(
        self,
        job: ExportJob,
        rendered: RenderedExport,
        *,
        object_key: str,
    ) -> None:
        updated = replace(
            job,
            status=ExportStatus.COMPLETE,
            object_key=object_key,
            content_sha256=rendered.content_sha256,
            content_length=rendered.content_length,
            row_count=rendered.row_count,
            lease_expires_at=0,
            error_code="",
        )
        await self._update_status(
            updated,
            status=ExportStatus.COMPLETE,
            error_code="",
            require_claim=True,
        )

    async def release_or_fail(
        self,
        job: ExportJob,
        *,
        error_code: str,
        final: bool,
    ) -> None:
        await self._update_status(
            replace(
                job,
                status=(ExportStatus.FAILED if final else ExportStatus.QUEUED),
                lease_expires_at=0,
                error_code=error_code,
            ),
            status=(ExportStatus.FAILED if final else ExportStatus.QUEUED),
            error_code=error_code,
            require_claim=True,
        )

    async def _update_status(
        self,
        job: ExportJob,
        *,
        status: ExportStatus,
        error_code: str,
        require_claim: bool,
    ) -> None:
        from botocore.exceptions import ClientError

        names = {"#status": "status"}
        values: dict[str, object] = {
            ":status": status.value,
            ":error": error_code,
            ":empty": "",
            ":zero": 0,
        }
        expression = "SET #status = :status, error_code = :error, claim_token = :empty, lease_expires_at = :zero"
        if status is ExportStatus.COMPLETE:
            expression += (
                ", object_key = :object_key, "
                "content_sha256 = :content_sha256, "
                "content_length = :content_length, "
                "row_count = :row_count"
            )
            values.update(
                {
                    ":object_key": job.object_key,
                    ":content_sha256": job.content_sha256,
                    ":content_length": job.content_length,
                    ":row_count": job.row_count,
                }
            )
        condition = "#status = :queued"
        values[":queued"] = ExportStatus.QUEUED.value
        if require_claim:
            condition = "#status = :processing AND claim_token = :claim_token"
            values[":processing"] = ExportStatus.PROCESSING.value
            values[":claim_token"] = job.claim_token

        def _update() -> None:
            self._resource().update_item(
                Key={
                    "PK": _job_partition(job.tenant_id),
                    "SK": _job_sort(job.job_id),
                },
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )

        try:
            await asyncio.to_thread(_update)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == ("ConditionalCheckFailedException"):
                raise ExportJobLeaseLost("export job lease no longer belongs to this worker") from exc
            raise


class SqsExportJobQueue:
    """FIFO queue adapter with tenant-scoped ordering."""

    def __init__(
        self,
        *,
        queue_url: str,
        region: str,
        client: object | None = None,
    ) -> None:
        self._queue_url = queue_url
        self._region = region
        self._client = client

    def _sqs(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("sqs", region_name=self._region)
        return self._client

    async def enqueue(self, job: ExportJob) -> None:
        group = hashlib.sha256(job.tenant_id.encode("utf-8")).hexdigest()
        await asyncio.to_thread(
            self._sqs().send_message,
            QueueUrl=self._queue_url,
            MessageBody=export_message(job),
            MessageDeduplicationId=job.job_id,
            MessageGroupId=group,
        )


class S3ExportObjectStore:
    """Private, short-lived S3 export objects and download URLs."""

    def __init__(
        self,
        *,
        bucket_name: str,
        account_id: str,
        region: str,
        client: object | None = None,
    ) -> None:
        self._bucket_name = bucket_name
        self._account_id = account_id
        self._region = region
        self._client = client

    def _s3(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    async def upload(
        self,
        job: ExportJob,
        rendered: RenderedExport,
    ) -> str:
        object_key = _job_object_key(job)
        checksum = base64.b64encode(bytes.fromhex(rendered.content_sha256)).decode("ascii")

        def _put() -> None:
            with rendered.path.open("rb") as body:
                self._s3().put_object(
                    Bucket=self._bucket_name,
                    Key=object_key,
                    Body=body,
                    ChecksumSHA256=checksum,
                    ContentDisposition=(f'attachment; filename="{rendered.filename}"'),
                    ContentLength=rendered.content_length,
                    ContentType=rendered.content_type,
                    ExpectedBucketOwner=self._account_id,
                    Metadata={
                        "axon-job-id": job.job_id,
                        "axon-kind": job.kind.value,
                    },
                )

        await asyncio.to_thread(_put)
        return object_key

    async def download_url(self, job: ExportJob) -> str:
        def _sign() -> str:
            return self._s3().generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self._bucket_name,
                    "Key": job.object_key,
                    "ExpectedBucketOwner": self._account_id,
                    "ResponseContentDisposition": (f'attachment; filename="{job.filename}"'),
                    "ResponseContentType": job.content_type,
                },
                ExpiresIn=EXPORT_DOWNLOAD_SECONDS,
            )

        return await asyncio.to_thread(_sign)


class DynamoExportDataReader:
    """Page through canonical usage and audit rows without loading the table."""

    def __init__(
        self,
        *,
        table_name: str,
        region: str,
        table: object | None = None,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._table = table

    def _resource(self):
        if self._table is None:
            import boto3

            self._table = boto3.resource(
                "dynamodb",
                region_name=self._region,
            ).Table(self._table_name)
        return self._table

    def usage_records(self, job: ExportJob) -> Iterable[object]:
        from boto3.dynamodb.conditions import Attr
        from src.gateway.persistence import DynamoPersistence

        condition = Attr("entity_type").eq("usage_record")
        if job.tenant_id == LEGACY_TENANT_ID:
            condition &= Attr("tenant_id").not_exists() | Attr("tenant_id").eq(LEGACY_TENANT_ID)
        else:
            condition &= Attr("tenant_id").eq(job.tenant_id)
        response = self._resource().scan(FilterExpression=condition)
        while True:
            for item in response.get("Items", []):
                native = DynamoPersistence._convert_decimals_to_native(item)
                yield DynamoPersistence.deserialize_usage_record(native)
            key = response.get("LastEvaluatedKey")
            if not isinstance(key, dict) or not key:
                break
            response = self._resource().scan(
                FilterExpression=condition,
                ExclusiveStartKey=key,
            )

    def audit_records(
        self,
        job: ExportJob,
    ) -> Iterable[Mapping[str, object]]:
        if job.tenant_id == LEGACY_TENANT_ID:
            yield from self._legacy_audit_records(job)
            return
        from boto3.dynamodb.conditions import Key

        condition = Key("PK").eq(_job_partition(job.tenant_id)) & Key("SK").begins_with("AUDIT#RECORD#")
        response = self._resource().query(
            KeyConditionExpression=condition,
            ConsistentRead=True,
        )
        while True:
            for item in response.get("Items", []):
                normalized = _native(dict(item))
                if (
                    job.filter_map.get("project_id") is None
                    or normalized.get("project_id") == job.filter_map["project_id"]
                ):
                    yield normalized
            key = response.get("LastEvaluatedKey")
            if not isinstance(key, dict) or not key:
                break
            response = self._resource().query(
                KeyConditionExpression=condition,
                ExclusiveStartKey=key,
                ConsistentRead=True,
            )

    def _legacy_audit_records(
        self,
        job: ExportJob,
    ) -> Iterable[Mapping[str, object]]:
        from boto3.dynamodb.conditions import Attr

        condition = Attr("PK").begins_with("AUDIT#")
        project_id = job.filter_map.get("project_id")
        if project_id is not None:
            condition = Attr("PK").eq(f"AUDIT#{project_id}")
        response = self._resource().scan(FilterExpression=condition)
        while True:
            for item in response.get("Items", []):
                yield _native(dict(item))
            key = response.get("LastEvaluatedKey")
            if not isinstance(key, dict) or not key:
                break
            response = self._resource().scan(
                FilterExpression=condition,
                ExclusiveStartKey=key,
            )


def build_export_job_service() -> ExportJobService:
    """Build the control-plane export service from explicit environment."""

    region = os.environ.get("AWS_REGION", "").strip()
    table_name = os.environ.get("AXON_DYNAMODB_TABLE", "").strip()
    queue_url = os.environ.get("AXON_EXPORT_QUEUE_URL", "").strip()
    bucket_name = os.environ.get("AXON_EXPORT_BUCKET_NAME", "").strip()
    account_id = os.environ.get("AXON_AWS_ACCOUNT_ID", "").strip()
    if not all((region, table_name, queue_url, bucket_name, account_id)):
        raise RuntimeError("serverless export configuration is incomplete")
    store = DynamoExportJobStore(
        table_name=table_name,
        region=region,
    )
    objects = S3ExportObjectStore(
        bucket_name=bucket_name,
        account_id=account_id,
        region=region,
    )
    return ExportJobService(
        store=store,
        queue=SqsExportJobQueue(
            queue_url=queue_url,
            region=region,
        ),
        objects=objects,
    )


def build_export_job_worker() -> ExportJobWorker:
    """Build the isolated export worker from explicit environment."""

    region = os.environ.get("AWS_REGION", "").strip()
    table_name = os.environ.get("AXON_DYNAMODB_TABLE", "").strip()
    bucket_name = os.environ.get("AXON_EXPORT_BUCKET_NAME", "").strip()
    account_id = os.environ.get("AXON_AWS_ACCOUNT_ID", "").strip()
    if not all((region, table_name, bucket_name, account_id)):
        raise RuntimeError("export worker configuration is incomplete")
    store = DynamoExportJobStore(
        table_name=table_name,
        region=region,
    )
    objects = S3ExportObjectStore(
        bucket_name=bucket_name,
        account_id=account_id,
        region=region,
    )
    return ExportJobWorker(
        store=store,
        objects=objects,
        renderer=ExportRenderer(
            DynamoExportDataReader(
                table_name=table_name,
                region=region,
            )
        ),
    )


__all__ = [
    "DynamoExportDataReader",
    "DynamoExportJobStore",
    "EXPORT_MESSAGE_SCHEMA",
    "ExportFormat",
    "ExportJob",
    "ExportJobError",
    "ExportJobLeaseLost",
    "ExportJobNotFound",
    "ExportJobNotReady",
    "ExportJobService",
    "ExportJobWorker",
    "ExportKind",
    "ExportLevel",
    "ExportRenderer",
    "ExportStatus",
    "RenderedExport",
    "S3ExportObjectStore",
    "SqsExportJobQueue",
    "build_export_job_service",
    "build_export_job_worker",
    "export_job_public",
    "export_message",
    "redact_audit_value",
    "request_export_identity",
    "serialize_audit_export_row",
    "usage_record_export_row",
]
