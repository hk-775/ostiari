"""Request-independent Lambda workers for durable AxonLLM background work."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging
import math
import os
from collections.abc import Callable
from typing import Protocol

from src.gateway.query.reconciliation import (
    QueryLifecycleReconciler,
    QueryReconciliationResult,
)
from src.gateway.security.event_dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

_MAX_SQS_BATCH_SIZE = 10


class SecurityEventDelivery(Protocol):
    """Minimal event-delivery boundary used by the Lambda host."""

    async def deliver_outbox_body(self, body: object) -> str:
        """Deliver one immutable outbox envelope."""

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        """Close request-scoped delivery resources."""


class QueryReconciliation(Protocol):
    """Minimal scheduled reconciliation boundary used by Lambda."""

    async def run(
        self,
        *,
        cursor: str | None = None,
        max_pages: int = 10,
    ) -> QueryReconciliationResult:
        """Run one bounded reconciliation pass."""


class ExportProcessing(Protocol):
    """Minimal durable-export boundary used by the Lambda host."""

    async def process(self, body: object) -> str:
        """Process one immutable export queue envelope."""


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise RuntimeError(f"{name} must be configured")
    return value


def _environment_integer(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw in (None, "") else int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _environment_float(
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw in (None, "") else float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def build_query_reconciler() -> QueryLifecycleReconciler:
    """Construct only the durable services required by scheduled recovery."""

    if os.environ.get("AXON_ATHENA_QUERY_ENABLED", "").lower() != "true":
        raise RuntimeError("scheduled query reconciliation is not enabled")

    from src.gateway.persistence import DynamoPersistence
    from src.gateway.query.athena import AthenaExecutor, AthenaQueryLimits
    from src.gateway.query.models import AthenaRoleBindings
    from src.gateway.query.repository import DynamoDatasourceRepository
    from src.gateway.security.audit_trail import AuditTrail

    region = _required_environment("AWS_REGION")
    table_name = _required_environment("AXON_DYNAMODB_TABLE")
    bindings = AthenaRoleBindings.from_json(_required_environment("AXON_ATHENA_QUERY_BINDINGS"))
    if bindings.empty:
        raise RuntimeError("scheduled query reconciliation requires Athena role bindings")
    persistence = DynamoPersistence(
        table_name=table_name,
        region=region,
        routing_config_signing_mode="disabled",
    )
    if not persistence.enabled:
        raise RuntimeError("scheduled query reconciliation requires DynamoDB persistence")
    max_datasources = _environment_integer(
        "AXON_ATHENA_QUERY_MAX_DATASOURCES_PER_TENANT",
        default=500,
        minimum=1,
        maximum=10_000,
    )
    page_size = _environment_integer(
        "AXON_QUERY_RECONCILIATION_PAGE_SIZE",
        default=2,
        minimum=1,
        maximum=10,
    )
    executor = AthenaExecutor(
        limits=AthenaQueryLimits(
            timeout_seconds=_environment_float(
                "AXON_ATHENA_QUERY_TIMEOUT_SECONDS",
                default=30.0,
                minimum=0.001,
                maximum=300.0,
            ),
            max_rows=_environment_integer(
                "AXON_ATHENA_QUERY_MAX_ROWS",
                default=1_000,
                minimum=1,
                maximum=10_000,
            ),
            max_result_bytes=_environment_integer(
                "AXON_ATHENA_QUERY_MAX_RESULT_BYTES",
                default=1024 * 1024,
                minimum=1024,
                maximum=16 * 1024 * 1024,
            ),
            max_bytes_scanned=_environment_integer(
                "AXON_ATHENA_QUERY_MAX_BYTES_SCANNED",
                default=1024 * 1024 * 1024,
                minimum=1,
                maximum=2**63 - 1,
            ),
            poll_interval_seconds=_environment_float(
                "AXON_ATHENA_QUERY_POLL_INTERVAL_SECONDS",
                default=0.25,
                minimum=0.05,
                maximum=5.0,
            ),
        )
    )
    audit_trail = AuditTrail(persistence=persistence)
    return QueryLifecycleReconciler(
        store=persistence,
        repository=DynamoDatasourceRepository(
            persistence,
            max_datasources_per_tenant=max_datasources,
        ),
        bindings=bindings,
        executor=executor,
        audit_trail=audit_trail,
        claim_seconds=300,
        page_size=page_size,
    )


def _security_event_records(event: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(event, dict):
        raise ValueError("security event Lambda input must be an object")
    records = event.get("Records")
    if not isinstance(records, list) or not records or len(records) > _MAX_SQS_BATCH_SIZE:
        raise ValueError("security event Lambda input has an invalid batch")

    validated: list[tuple[str, str]] = []
    message_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("eventSource") != "aws:sqs":
            raise ValueError("security event Lambda record is not from SQS")
        message_id = record.get("messageId")
        body = record.get("body")
        event_source_arn = record.get("eventSourceARN")
        if (
            not isinstance(message_id, str)
            or not message_id
            or message_id in message_ids
            or not isinstance(body, str)
            or not body
            or not isinstance(event_source_arn, str)
            or not event_source_arn.endswith(".fifo")
        ):
            raise ValueError("security event Lambda record is invalid")
        message_ids.add(message_id)
        validated.append((message_id, body))
    return tuple(validated)


def _single_export_record(event: object) -> tuple[str, str]:
    if not isinstance(event, dict):
        raise ValueError("export Lambda input must be an object")
    records = event.get("Records")
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("export Lambda input must contain one record")
    record = records[0]
    if not isinstance(record, dict) or record.get("eventSource") != "aws:sqs":
        raise ValueError("export Lambda record is not from SQS")
    message_id = record.get("messageId")
    body = record.get("body")
    event_source_arn = record.get("eventSourceARN")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(body, str)
        or not body
        or not isinstance(event_source_arn, str)
        or not event_source_arn.endswith(".fifo")
    ):
        raise ValueError("export Lambda record is invalid")
    return message_id, body


async def process_export_sqs_batch(
    event: object,
    *,
    worker_factory: Callable[[], ExportProcessing] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Process one FIFO export message with partial-batch retry semantics."""

    message_id, body = _single_export_record(event)
    if worker_factory is None:
        from src.gateway.export_jobs import build_export_job_worker

        worker_factory = build_export_job_worker
    worker = worker_factory()
    try:
        await worker.process(body)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error(
            "Export worker failed message_id=%s",
            message_id,
            exc_info=True,
        )
        return {"batchItemFailures": [{"itemIdentifier": message_id}]}
    return {"batchItemFailures": []}


async def process_security_event_sqs_batch(
    event: object,
    *,
    dispatcher_factory: Callable[[], SecurityEventDelivery] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Process an ordered FIFO batch and return Lambda partial failures."""

    records = _security_event_records(event)
    dispatcher = EventDispatcher() if dispatcher_factory is None else dispatcher_factory()
    try:
        for index, (message_id, body) in enumerate(records):
            try:
                await dispatcher.deliver_outbox_body(body)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Security event Lambda delivery failed message_id=%s",
                    message_id,
                )
                return {
                    "batchItemFailures": [
                        {"itemIdentifier": failed_message_id} for failed_message_id, _ in records[index:]
                    ]
                }
        return {"batchItemFailures": []}
    finally:
        await dispatcher.stop()


def security_event_lambda_handler(
    event: object,
    context: object,
) -> dict[str, list[dict[str, str]]]:
    """AWS Lambda entry point for the security-event SQS outbox."""

    del context
    return asyncio.run(process_security_event_sqs_batch(event))


def export_lambda_handler(
    event: object,
    context: object,
) -> dict[str, list[dict[str, str]]]:
    """AWS Lambda entry point for the durable-export FIFO queue."""

    del context
    return asyncio.run(process_export_sqs_batch(event))


async def process_query_reconciliation(
    event: object,
    *,
    reconciler_factory: (Callable[[], QueryReconciliation] | None) = None,
) -> dict[str, object]:
    """Run one scheduled, lease-fenced query reconciliation pass."""

    if not isinstance(event, dict) or event != {"schema": "axonllm.query-reconciliation/v1"}:
        raise ValueError("query reconciliation Lambda input is invalid")
    reconciler = build_query_reconciler() if reconciler_factory is None else reconciler_factory()
    max_pages = _environment_integer(
        "AXON_QUERY_RECONCILIATION_MAX_PAGES",
        default=1,
        minimum=1,
        maximum=10,
    )
    result = await reconciler.run(max_pages=max_pages)
    response = {
        "schema": "axonllm.query-reconciliation-result/v1",
        **{
            {
                "audited": "audited",
                "claimed": "claimed",
                "deferred": "deferred",
                "failed": "failed",
                "finalized": "finalized",
                "lost_claims": "lostClaims",
                "next_cursor": "nextCursor",
                "pages": "pages",
            }[name]: value
            for name, value in asdict(result).items()
        },
    }
    if result.failed:
        raise RuntimeError(f"{result.failed} query reconciliation claim(s) failed")
    return response


def query_reconciliation_lambda_handler(
    event: object,
    context: object,
) -> dict[str, object]:
    """AWS Lambda entry point for scheduled query lifecycle recovery."""

    del context
    return asyncio.run(process_query_reconciliation(event))


__all__ = [
    "export_lambda_handler",
    "process_export_sqs_batch",
    "process_security_event_sqs_batch",
    "process_query_reconciliation",
    "query_reconciliation_lambda_handler",
    "security_event_lambda_handler",
]
