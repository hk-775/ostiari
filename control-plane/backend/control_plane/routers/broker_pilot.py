"""Token broker pilot API — pool inventory, draw-down, depletion, reconciliation.

Production path beyond token_broker.py's reporting:
  - provision/fund per-provider token pools,
  - draw consumed tokens down as usage is recorded (called from costs.record),
  - halt a pool when it depletes,
  - reconcile our computed cost against the provider's actual invoice.

Billing collection uses a swappable Collector (simulated now; env selects live).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import broker_pilot
from control_plane.database import get_db
from control_plane.models.database import ReconciliationRecord, TokenPool, UsageRecord

log = logging.getLogger("control_plane.broker_pilot")

router = APIRouter(prefix="/api/token-broker/pilot", tags=["token-broker-pilot"])

# Billing collector — simulated unless OSTIARI_BROKER_BILLING=live.
if os.environ.get("OSTIARI_BROKER_BILLING", "simulated").lower() == "live":
    _collector: broker_pilot.Collector = broker_pilot.StripeCollector(
        api_key=os.environ.get("STRIPE_API_KEY", ""),
        price_id=os.environ.get("STRIPE_PRICE_ID", ""),
    )
else:
    _collector = broker_pilot.SimulatedCollector()


class PoolFund(BaseModel):
    provider: str
    tokens: int
    cost_usd: float                       # our bulk cost for these tokens
    low_threshold_tokens: int = 0


class ReconcileInput(BaseModel):
    provider: str
    period_days: int = 30
    invoiced_cost_usd: float              # provider's actual bill for the period


# ─── Pool inventory ──────────────────────────────────────────────────────────

@router.get("/pools")
async def list_pools(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(TokenPool))).scalars().all()
    return [_pool_dict(p) for p in rows]


@router.post("/pools/fund")
async def fund_pool(body: PoolFund, db: AsyncSession = Depends(get_db)):
    """Add purchased token inventory to a provider pool (create if new)."""
    p = await db.get(TokenPool, body.provider)
    if p is None:
        p = TokenPool(
            provider=body.provider, purchased_tokens=0, purchased_cost_usd=0.0,
            consumed_tokens=0, consumed_cost_usd=0.0, low_threshold_tokens=0, status="active",
        )
        db.add(p)
    p.purchased_tokens += body.tokens
    p.purchased_cost_usd += body.cost_usd
    if body.low_threshold_tokens:
        p.low_threshold_tokens = body.low_threshold_tokens
    # Re-activate if a top-up lifts it back above the threshold.
    if p.status == "depleted" and _remaining(p) > p.low_threshold_tokens:
        p.status = "active"
    await db.commit()
    await db.refresh(p)
    return _pool_dict(p)


@router.get("/collector")
async def collector_info():
    return {"mode": _collector.mode}


# ─── Reconciliation ──────────────────────────────────────────────────────────

@router.post("/reconcile")
async def reconcile(body: ReconcileInput, db: AsyncSession = Depends(get_db)):
    """Compare our tracked consumption cost vs the provider's actual invoice."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=body.period_days)

    records = (await db.execute(
        select(UsageRecord).where(UsageRecord.timestamp >= since)
    )).scalars().all()

    computed = 0.0
    tokens = 0
    for r in records:
        if broker_pilot.provider_for(r.model) == body.provider:
            computed += float(r.cost_usd or 0.0)
            tokens += int(r.total_tokens or 0)

    rec = ReconciliationRecord(
        provider=body.provider, period_start=since, period_end=now,
        computed_cost_usd=round(computed, 6), invoiced_cost_usd=body.invoiced_cost_usd,
        consumed_tokens=tokens,
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return _recon_dict(rec)


@router.get("/reconciliations")
async def list_reconciliations(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ReconciliationRecord).order_by(ReconciliationRecord.created_at.desc()).limit(50)
    )).scalars().all()
    return [_recon_dict(r) for r in rows]


# ─── Draw-down (called from usage recording) ─────────────────────────────────

async def draw_down(db: AsyncSession, *, model: str, tokens: int, our_cost_usd: float) -> None:
    """Decrement the provider pool for consumed tokens; halt on depletion.

    Best-effort: if no pool exists for the provider, this is a no-op (pilot may
    not have provisioned every provider). Does not commit — the caller owns the
    transaction so draw-down and the usage record land atomically.
    """
    provider = broker_pilot.provider_for(model)
    pool = await db.get(TokenPool, provider)
    if pool is None:
        return
    pool.consumed_tokens += tokens
    pool.consumed_cost_usd += our_cost_usd
    if _remaining(pool) <= pool.low_threshold_tokens:
        if pool.status != "depleted":
            log.warning("Token pool '%s' depleted (%d tokens remaining)", provider, _remaining(pool))
        pool.status = "depleted"


def _remaining(p: TokenPool) -> int:
    return max(0, p.purchased_tokens - p.consumed_tokens)


# ─── Serializers ─────────────────────────────────────────────────────────────

def _pool_dict(p: TokenPool) -> dict:
    remaining = _remaining(p)
    pct = round(remaining / p.purchased_tokens * 100, 1) if p.purchased_tokens else 0.0
    return {
        "provider": p.provider,
        "purchased_tokens": p.purchased_tokens,
        "purchased_cost_usd": round(p.purchased_cost_usd, 4),
        "consumed_tokens": p.consumed_tokens,
        "consumed_cost_usd": round(p.consumed_cost_usd, 4),
        "remaining_tokens": remaining,
        "remaining_pct": pct,
        "low_threshold_tokens": p.low_threshold_tokens,
        "status": p.status,
    }


def _recon_dict(r: ReconciliationRecord) -> dict:
    drift = round(r.invoiced_cost_usd - r.computed_cost_usd, 6)
    base = r.computed_cost_usd or 0.0
    drift_pct = round(drift / base * 100, 2) if base else 0.0
    return {
        "id": r.id, "provider": r.provider,
        "period_start": r.period_start.isoformat() if r.period_start else "",
        "period_end": r.period_end.isoformat() if r.period_end else "",
        "computed_cost_usd": round(r.computed_cost_usd, 6),
        "invoiced_cost_usd": round(r.invoiced_cost_usd, 6),
        "drift_usd": drift, "drift_pct": drift_pct,
        "consumed_tokens": r.consumed_tokens,
    }
