"""Cost and usage tracking API."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import broker_pilot
from control_plane.auth.dependencies import get_current_org
from control_plane.auth.workload import authorize_reported_gateway
from control_plane.database import get_db
from control_plane.models.database import DEFAULT_ORG, UsageRecord
from control_plane.models.schemas import CostSummary, UsageRecordCreate, UsageRecordResponse
from control_plane.models.scoping import scoped

router = APIRouter(prefix="/api/costs", tags=["costs"])
log = logging.getLogger("control_plane.costs")

MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.0 / 1_000_000, "output": 15.0 / 1_000_000},
    "claude-haiku-4-5": {"input": 0.80 / 1_000_000, "output": 4.0 / 1_000_000},
    "claude-opus-4-6": {"input": 15.0 / 1_000_000, "output": 75.0 / 1_000_000},
    "gpt-4o": {"input": 2.50 / 1_000_000, "output": 10.0 / 1_000_000},
    "gpt-4o-mini": {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000},
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        for key, val in MODEL_PRICING.items():
            if key in model:
                pricing = val
                break
    if pricing is None:
        pricing = {"input": 3.0 / 1_000_000, "output": 15.0 / 1_000_000}
    return (input_tokens * pricing["input"]) + (output_tokens * pricing["output"])


@router.post("/record", response_model=UsageRecordResponse)
async def record_usage(
    body: UsageRecordCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Record a usage event from a gateway (called after each LLM invocation)."""
    gateway = await authorize_reported_gateway(request, db, body.gateway_id)
    org = gateway.org_id if gateway is not None else DEFAULT_ORG
    record, created = await _upsert_usage(db, body, org)
    if created:
        await _broker_account(db, record, org)
    await db.commit()
    await db.refresh(record)

    error = await _collect_broker_charge(record, org)
    await db.commit()
    if error:
        raise HTTPException(
            status_code=503,
            detail=f"Usage recorded; broker billing remains pending: {error}",
        )
    return record


def _usage_values(body: UsageRecordCreate, org: str) -> dict[str, Any]:
    cost = body.cost_usd
    if cost == 0.0 and body.total_tokens > 0:
        cost = _estimate_cost(body.model, body.input_tokens, body.output_tokens)
    provider = broker_pilot.canonical_provider(
        body.provider or broker_pilot.provider_for(body.model)
    )
    return {
        "org_id": org,
        "gateway_id": body.gateway_id,
        "event_id": body.event_id,
        "agent_id": body.agent_id,
        "model": body.model,
        "experiment_name": body.experiment_name,
        "experiment_variant": body.experiment_variant,
        "provider": provider,
        "input_tokens": body.input_tokens,
        "output_tokens": body.output_tokens,
        "total_tokens": body.total_tokens,
        "cost_usd": cost,
        "action": body.action,
    }


def _same_usage(record: UsageRecord, values: dict[str, Any]) -> bool:
    exact_fields = (
        "org_id",
        "gateway_id",
        "event_id",
        "agent_id",
        "model",
        "experiment_name",
        "experiment_variant",
        "provider",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "action",
    )
    return all(getattr(record, field) == values[field] for field in exact_fields) and (
        abs(float(record.cost_usd) - float(values["cost_usd"])) < 1e-12
    )


async def _upsert_usage(
    db: AsyncSession,
    body: UsageRecordCreate,
    org: str,
) -> tuple[UsageRecord, bool]:
    """Insert one usage event, or return its identical prior retry."""
    values = _usage_values(body, org)
    if not body.event_id:
        record = UsageRecord(**values)
        db.add(record)
        await db.flush()
        return record, True

    dialect = db.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(UsageRecord)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[
                UsageRecord.org_id,
                UsageRecord.gateway_id,
                UsageRecord.event_id,
            ]
        )
        .returning(UsageRecord.id)
    )
    record_id = (await db.execute(stmt)).scalar_one_or_none()
    if record_id is not None:
        record = await db.get(UsageRecord, record_id)
        if record is None:  # pragma: no cover - defensive against a broken driver
            raise RuntimeError("Inserted usage record could not be reloaded")
        return record, True

    record = (
        await db.execute(
            select(UsageRecord).where(
                UsageRecord.org_id == org,
                UsageRecord.gateway_id == body.gateway_id,
                UsageRecord.event_id == body.event_id,
            )
        )
    ).scalar_one()
    if not _same_usage(record, values):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Usage event '{body.event_id}' was already recorded with "
                "different data"
            ),
        )
    return record, False


async def _broker_account(
    db: AsyncSession,
    record: UsageRecord,
    org: str = DEFAULT_ORG,
) -> None:
    """Atomically debit a provisioned pool and stage its customer charge."""
    if record.total_tokens <= 0:
        return

    from control_plane.routers.broker_pilot import draw_down
    from control_plane.routers.token_broker import _config as broker_config

    config = broker_config[org]
    our_cost = float(record.cost_usd) * (
        1 - float(config.get("bulk_discount", 0.0))
    )
    pool = await draw_down(
        db,
        model=record.model,
        provider=record.provider,
        tokens=record.total_tokens,
        our_cost_usd=our_cost,
        org=org,
    )
    if pool is None:
        return

    record.broker_cost_usd = our_cost
    record.broker_charge_usd = our_cost * (
        1 + float(config.get("markup", 0.0))
    )
    record.billing_status = "pending"
    record.billing_error = ""


async def _collect_broker_charge(record: UsageRecord, org: str) -> str | None:
    """Collect one staged charge; failures stay persisted for retry."""
    if record.billing_status not in {"pending", "failed"}:
        return None

    from control_plane.routers.broker_pilot import _collector

    idempotency_key = (
        f"{record.gateway_id}:{record.event_id}"
        if record.event_id
        else f"usage:{record.gateway_id}:{record.id}"
    )
    try:
        result = await _collector.collect(
            customer=org,
            amount_usd=float(record.broker_charge_usd),
            model=record.model,
            idempotency_key=idempotency_key,
        )
        if not result.get("collected"):
            raise RuntimeError("collector did not confirm the charge")
    except Exception as exc:  # noqa: BLE001 - persisted and retried by the gateway
        record.billing_status = "failed"
        record.billing_error = str(exc)
        log.warning(
            "Broker billing failed for usage event %s: %s",
            idempotency_key,
            exc,
        )
        return str(exc)

    record.billing_status = "collected"
    record.billing_ref = str(result.get("ref", ""))[:128]
    record.billing_error = ""
    return None


@router.post("/record/batch")
async def record_usage_batch(
    records: list[UsageRecordCreate],
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Record, debit, and bill a retry-safe batch of usage events."""
    created = 0
    duplicates = 0
    org_cache: dict[str, str] = {}  # one gateway lookup per batch, not per record
    processed: dict[int, tuple[UsageRecord, str]] = {}
    for body in records:
        if body.gateway_id not in org_cache:
            gateway = await authorize_reported_gateway(
                request,
                db,
                body.gateway_id,
            )
            org_cache[body.gateway_id] = (
                gateway.org_id if gateway is not None else DEFAULT_ORG
            )
        org = org_cache[body.gateway_id]
        record, was_created = await _upsert_usage(db, body, org)
        if was_created:
            await _broker_account(db, record, org)
            created += 1
        else:
            duplicates += 1
        processed[record.id] = (record, org)

    # Usage and pool debits land before calling an external collector. If the
    # process exits during billing, the next gateway retry finds these rows and
    # retries only the still-pending charge without debiting the pool again.
    await db.commit()

    billing_failures: list[dict[str, str]] = []
    for record, org in processed.values():
        error = await _collect_broker_charge(record, org)
        if error:
            billing_failures.append(
                {
                    "event_id": record.event_id or f"usage:{record.id}",
                    "error": error,
                }
            )
    await db.commit()

    from control_plane.routers.broker_pilot import pool_snapshot

    broker_pools = {
        gateway_id: await pool_snapshot(db, org)
        for gateway_id, org in org_cache.items()
    }
    response: dict[str, Any] = {"recorded": created}
    if duplicates:
        response["duplicates"] = duplicates
    if any(broker_pools.values()):
        response["broker_pools"] = broker_pools
    if billing_failures:
        response["billing_failures"] = billing_failures
        return JSONResponse(status_code=503, content=response)
    return response


@router.get("/summary", response_model=CostSummary)
async def get_cost_summary(
    period_days: int = Query(default=7, le=90),
    gateway_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Get cost summary with breakdowns by model, gateway, agent, and day.

    Scoped to the caller's org: unscoped, the by_gateway/by_agent breakdowns
    enumerate every tenant's gateway names, agent names, and dollar spend.
    """
    since = datetime.now(timezone.utc) - timedelta(days=period_days)

    query = scoped(select(UsageRecord).where(UsageRecord.timestamp >= since), UsageRecord, org)
    if gateway_id:
        query = query.where(UsageRecord.gateway_id == gateway_id)

    result = await db.execute(query)
    records = result.scalars().all()

    total_cost = sum(r.cost_usd for r in records)
    total_tokens = sum(r.total_tokens for r in records)

    by_model: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "requests": 0})
    by_gateway: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "requests": 0})
    by_agent: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "requests": 0})
    daily: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "requests": 0})

    for r in records:
        by_model[r.model]["cost"] += r.cost_usd
        by_model[r.model]["tokens"] += r.total_tokens
        by_model[r.model]["requests"] += 1

        by_gateway[r.gateway_id]["cost"] += r.cost_usd
        by_gateway[r.gateway_id]["tokens"] += r.total_tokens
        by_gateway[r.gateway_id]["requests"] += 1

        by_agent[r.agent_id]["cost"] += r.cost_usd
        by_agent[r.agent_id]["tokens"] += r.total_tokens
        by_agent[r.agent_id]["requests"] += 1

        day = r.timestamp.strftime("%Y-%m-%d")
        daily[day]["cost"] += r.cost_usd
        daily[day]["tokens"] += r.total_tokens
        daily[day]["requests"] += 1

    return CostSummary(
        total_cost_usd=round(total_cost, 4),
        total_tokens=total_tokens,
        total_requests=len(records),
        by_model=[{"model": k, **v} for k, v in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True)],
        by_gateway=[{"gateway_id": k, **v} for k, v in sorted(by_gateway.items(), key=lambda x: x[1]["cost"], reverse=True)],
        by_agent=[{"agent_id": k, **v} for k, v in sorted(by_agent.items(), key=lambda x: x[1]["cost"], reverse=True)],
        daily_costs=[{"date": k, **v} for k, v in sorted(daily.items())],
    )


@router.get("/records", response_model=list[UsageRecordResponse])
async def list_usage_records(
    gateway_id: str | None = None,
    model: str | None = None,
    limit: int = Query(default=100, le=1000),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """List individual usage records (caller's org only)."""
    query = scoped(
        select(UsageRecord).order_by(UsageRecord.timestamp.desc()).limit(limit),
        UsageRecord, org,
    )
    if gateway_id:
        query = query.where(UsageRecord.gateway_id == gateway_id)
    if model:
        query = query.where(UsageRecord.model == model)
    result = await db.execute(query)
    return result.scalars().all()
