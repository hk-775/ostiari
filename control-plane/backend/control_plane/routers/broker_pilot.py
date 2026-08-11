"""Token broker pilot API — pool inventory, draw-down, depletion, reconciliation.

Production path beyond token_broker.py's reporting:
  - provision/fund per-provider token pools,
  - draw consumed tokens down as usage is recorded (called from costs.record),
  - halt a pool when it depletes,
  - reconcile our computed cost against the provider's actual invoice.

Billing collection uses a swappable Collector (simulated now; env selects live).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import broker_pilot
from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import DEFAULT_ORG, ReconciliationRecord, TokenPool, UsageRecord
from control_plane.models.scoping import scoped, stamp

log = logging.getLogger("control_plane.broker_pilot")

router = APIRouter(prefix="/api/token-broker/pilot", tags=["token-broker-pilot"])

# Billing collector — simulated unless OSTIARI_BROKER_BILLING=live.
if os.environ.get("OSTIARI_BROKER_BILLING", "simulated").lower() == "live":
    try:
        _stripe_customer_map = json.loads(
            os.environ.get("STRIPE_CUSTOMER_MAP", "").strip() or "{}"
        )
        if not isinstance(_stripe_customer_map, dict):
            raise ValueError("STRIPE_CUSTOMER_MAP must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid STRIPE_CUSTOMER_MAP: {exc}") from exc
    _collector: broker_pilot.Collector = broker_pilot.StripeCollector(
        api_key=os.environ.get("STRIPE_API_KEY", ""),
        meter_event_name=os.environ.get(
            "STRIPE_METER_EVENT_NAME",
            "ostiari_broker_usage",
        ),
        customer_map=_stripe_customer_map,
        default_customer=os.environ.get("STRIPE_CUSTOMER_ID", ""),
        api_base=os.environ.get("STRIPE_API_BASE", "https://api.stripe.com"),
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
async def list_pools(db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    return await pool_snapshot(db, org)


@router.post("/pools/fund")
async def fund_pool(body: PoolFund, db: AsyncSession = Depends(get_db),
                    org: str = Depends(get_current_org)):
    """Add purchased token inventory to a provider pool (create if new)."""
    provider = broker_pilot.canonical_provider(body.provider)
    # Composite-key fetch. `db.get(TokenPool, provider)` would now be a TypeError,
    # which is the point of making org_id part of the key: funding a pool cannot
    # accidentally top up another tenant's inventory.
    p = await _get_pool(db, org, provider)
    if p is None:
        p = TokenPool(
            provider=provider, org_id=org, purchased_tokens=0, purchased_cost_usd=0.0,
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
    # Deliberately unscoped: the collector is process-level deployment config
    # (which billing backend this control plane runs), not tenant data. Every
    # other route on this router takes an org.
    status = getattr(_collector, "status", None)
    return status() if status else {"mode": _collector.mode}


# ─── Reconciliation ──────────────────────────────────────────────────────────

@router.post("/reconcile")
async def reconcile(body: ReconcileInput, db: AsyncSession = Depends(get_db),
                    org: str = Depends(get_current_org)):
    """Compare our tracked consumption cost vs the provider's actual invoice."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=body.period_days)

    # Scoped for the same reason token_broker's report is: the operator is
    # entering *their* provider invoice, so computing it against every tenant's
    # usage inflated one org's drift by the whole fleet's traffic — and drift is
    # the number this page exists to show.
    records = (await db.execute(
        scoped(select(UsageRecord).where(UsageRecord.timestamp >= since), UsageRecord, org)
    )).scalars().all()

    target_provider = broker_pilot.canonical_provider(body.provider)
    computed = 0.0
    tokens = 0
    for r in records:
        usage_provider = r.provider or broker_pilot.provider_for(r.model)
        if broker_pilot.canonical_provider(usage_provider) == target_provider:
            computed += float(r.cost_usd or 0.0)
            tokens += int(r.total_tokens or 0)

    rec = ReconciliationRecord(
        provider=target_provider, period_start=since, period_end=now,
        computed_cost_usd=round(computed, 6), invoiced_cost_usd=body.invoiced_cost_usd,
        consumed_tokens=tokens,
    )
    stamp(rec, org)
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return _recon_dict(rec)


@router.get("/reconciliations")
async def list_reconciliations(db: AsyncSession = Depends(get_db),
                               org: str = Depends(get_current_org)):
    rows = (await db.execute(
        scoped(select(ReconciliationRecord), ReconciliationRecord, org)
        .order_by(ReconciliationRecord.created_at.desc()).limit(50)
    )).scalars().all()
    return [_recon_dict(r) for r in rows]


# ─── Draw-down (called from usage recording) ─────────────────────────────────

async def draw_down(db: AsyncSession, *, model: str, tokens: int, our_cost_usd: float,
                    org: str = DEFAULT_ORG, provider: str = "") -> TokenPool | None:
    """Decrement the provider pool for consumed tokens; halt on depletion.

    Best-effort: if no pool exists for the provider *in this org*, this is a
    no-op (the pilot may not have provisioned every provider). Does not commit —
    the caller owns the transaction so draw-down and the usage record land
    atomically.

    `org` must be the org that owns the reporting gateway, never a value from the
    request body — gateways post usage with no user token, so the payload naming
    its own org would let one tenant burn down another's purchased inventory. It
    defaults to the single-org tenant so the pilot's own callers and tests stay
    unchanged.
    """
    if tokens <= 0:
        return None
    provider = broker_pilot.canonical_provider(
        provider or broker_pilot.provider_for(model)
    )
    pool = await _get_pool(db, org, provider)
    if pool is None:
        return None

    was_depleted = pool.status == "depleted"
    # Atomic SQL arithmetic prevents two concurrent gateway batches from reading
    # the same balance and overwriting one another's consumption.
    await db.execute(
        update(TokenPool)
        .where(TokenPool.org_id == org, TokenPool.provider == provider)
        .values(
            consumed_tokens=TokenPool.consumed_tokens + tokens,
            consumed_cost_usd=TokenPool.consumed_cost_usd + our_cost_usd,
            status=case(
                (
                    TokenPool.purchased_tokens
                    - (TokenPool.consumed_tokens + tokens)
                    <= TokenPool.low_threshold_tokens,
                    "depleted",
                ),
                else_=TokenPool.status,
            ),
        )
    )
    await db.flush()
    await db.refresh(pool)
    if pool.status == "depleted" and not was_depleted:
        log.warning(
            "Token pool '%s' depleted (%d tokens remaining)",
            provider,
            _remaining(pool),
        )
    return pool


def _remaining(p: TokenPool) -> int:
    return max(0, p.purchased_tokens - p.consumed_tokens)


async def _get_pool(db: AsyncSession, org: str, provider: str) -> TokenPool | None:
    """One org's pool for one provider, by composite primary key.

    `get_scoped` in models.scoping is the wrong tool here: it fetches by pk and
    *then* compares org_id, which assumes org_id is not part of the key. For this
    table it is, so the org goes into the lookup itself.
    """
    return await db.get(
        TokenPool,
        {
            "org_id": org,
            "provider": broker_pilot.canonical_provider(provider),
        },
    )


async def pool_snapshot(db: AsyncSession, org: str) -> list[dict]:
    """Return the current provider inventory shipped to gateways."""
    rows = (
        await db.execute(
            scoped(
                select(TokenPool).order_by(TokenPool.provider),
                TokenPool,
                org,
            )
        )
    ).scalars().all()
    return [_pool_dict(pool) for pool in rows]


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
