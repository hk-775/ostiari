"""Cost and usage tracking API."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.database import get_db
from control_plane.models.database import UsageRecord
from control_plane.models.schemas import CostSummary, UsageRecordCreate, UsageRecordResponse

router = APIRouter(prefix="/api/costs", tags=["costs"])

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
async def record_usage(body: UsageRecordCreate, db: AsyncSession = Depends(get_db)):
    """Record a usage event from a gateway (called after each LLM invocation)."""
    cost = body.cost_usd
    if cost == 0.0 and body.total_tokens > 0:
        cost = _estimate_cost(body.model, body.input_tokens, body.output_tokens)

    record = UsageRecord(
        gateway_id=body.gateway_id,
        agent_id=body.agent_id,
        model=body.model,
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        total_tokens=body.total_tokens,
        cost_usd=cost,
        action=body.action,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@router.post("/record/batch")
async def record_usage_batch(records: list[UsageRecordCreate], db: AsyncSession = Depends(get_db)):
    """Record a batch of usage events (for efficiency)."""
    created = 0
    for body in records:
        cost = body.cost_usd
        if cost == 0.0 and body.total_tokens > 0:
            cost = _estimate_cost(body.model, body.input_tokens, body.output_tokens)
        record = UsageRecord(
            gateway_id=body.gateway_id,
            agent_id=body.agent_id,
            model=body.model,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            total_tokens=body.total_tokens,
            cost_usd=cost,
            action=body.action,
        )
        db.add(record)
        created += 1
    await db.commit()
    return {"recorded": created}


@router.get("/summary", response_model=CostSummary)
async def get_cost_summary(
    period_days: int = Query(default=7, le=90),
    gateway_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get cost summary with breakdowns by model, gateway, agent, and day."""
    since = datetime.now(timezone.utc) - timedelta(days=period_days)

    query = select(UsageRecord).where(UsageRecord.timestamp >= since)
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
):
    """List individual usage records."""
    query = select(UsageRecord).order_by(UsageRecord.timestamp.desc()).limit(limit)
    if gateway_id:
        query = query.where(UsageRecord.gateway_id == gateway_id)
    if model:
        query = query.where(UsageRecord.model == model)
    result = await db.execute(query)
    return result.scalars().all()
