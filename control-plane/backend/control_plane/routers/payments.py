"""Payments API — x402 wallets, pricing, ledger, and push-to-gateway.

The control plane owns wallet balances/limits (DB) and the per-gateway pricing
policy (mode + priced patterns). Both are pushed to the gateway's payment gate
via POST /config/payments. The ledger (PaymentRecord) is the billing/audit
trail and the dashboard's data source.
"""

from __future__ import annotations

from collections import defaultdict

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway, PaymentRecord, Wallet
from control_plane.models.scoping import get_scoped, scoped, stamp

router = APIRouter(prefix="/api/payments", tags=["payments"])

# Per-gateway pricing policy (mode + priced patterns). Kept in the control
# plane; pushed to gateways alongside wallet balances. Defaults to off.
_pricing: dict[str, dict[str, dict]] = defaultdict(dict)


def _gateway_pricing(org: str, gateway_id: str) -> dict:
    return _pricing[org].get(gateway_id, {"mode": "off", "default": 0.0, "overrides": {}})


# ─── Schemas ─────────────────────────────────────────────────────────────────

class WalletUpsert(BaseModel):
    agent_id: str
    balance_usdc: float = 0.0
    address: str = ""
    daily_limit_usdc: float | None = None
    per_call_limit_usdc: float | None = None


class WalletPatch(BaseModel):
    daily_limit_usdc: float | None = None
    per_call_limit_usdc: float | None = None
    status: str | None = None  # active | paused


class FundRequest(BaseModel):
    amount_usdc: float


class PricingConfig(BaseModel):
    mode: str = "off"                     # off | metered | passthrough
    default: float = 0.0
    overrides: dict[str, float] = {}


class PaymentIngest(BaseModel):
    agent_id: str
    gateway_id: str = ""
    action: str = ""
    amount_usdc: float = 0.0
    settled: bool = True
    tx_hash: str = ""
    mode: str = "simulated"
    source: str = "policy"


# ─── Ingest (from gateways) ──────────────────────────────────────────────────

@router.post("/ingest")
async def ingest_payment(body: PaymentIngest, db: AsyncSession = Depends(get_db)):
    """Record a charge reported by a gateway; mirror the settled balance in DB.

    The gateway settles against its local wallet copy and reports here so the
    ledger, summary, and dashboard reflect real spend. On a settled charge we
    also decrement the DB wallet so CP balances track the gateway's.
    """
    db.add(PaymentRecord(
        agent_id=body.agent_id, gateway_id=body.gateway_id, action=body.action,
        amount_usdc=body.amount_usdc, settled=body.settled, tx_hash=body.tx_hash,
        mode=body.mode, source=body.source,
    ))
    if body.settled:
        w = await db.get(Wallet, body.agent_id)
        if w is not None:
            w.balance_usdc -= body.amount_usdc
            w.spent_today_usdc += body.amount_usdc
            if w.daily_limit_usdc is not None and w.spent_today_usdc >= w.daily_limit_usdc:
                w.status = "paused"
    await db.commit()
    return {"recorded": True}


# ─── Wallets ─────────────────────────────────────────────────────────────────

@router.get("/wallets")
async def list_wallets(db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    rows = (await db.execute(scoped(select(Wallet), Wallet, org))).scalars().all()
    return [_wallet_dict(w) for w in rows]


@router.post("/wallets")
async def upsert_wallet(body: WalletUpsert, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    w = await get_scoped(db, Wallet, body.agent_id, org)
    if w is None:
        w = Wallet(agent_id=body.agent_id)
        stamp(w, org)
        db.add(w)
    w.balance_usdc = body.balance_usdc
    w.address = body.address
    w.daily_limit_usdc = body.daily_limit_usdc
    w.per_call_limit_usdc = body.per_call_limit_usdc
    await db.commit()
    await db.refresh(w)
    return _wallet_dict(w)


@router.post("/wallets/{agent_id}/fund")
async def fund_wallet(agent_id: str, body: FundRequest, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Deposit USDC into an agent wallet (sim: bump balance; reactivates if paused)."""
    w = await get_scoped(db, Wallet, agent_id, org)
    if w is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    w.balance_usdc += body.amount_usdc
    if w.status == "paused" and (
        w.daily_limit_usdc is None or w.spent_today_usdc < w.daily_limit_usdc
    ):
        w.status = "active"
    await db.commit()
    await db.refresh(w)
    return _wallet_dict(w)


@router.patch("/wallets/{agent_id}")
async def patch_wallet(agent_id: str, body: WalletPatch, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    w = await get_scoped(db, Wallet, agent_id, org)
    if w is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if body.daily_limit_usdc is not None:
        w.daily_limit_usdc = body.daily_limit_usdc
    if body.per_call_limit_usdc is not None:
        w.per_call_limit_usdc = body.per_call_limit_usdc
    if body.status in ("active", "paused"):
        w.status = body.status
    await db.commit()
    await db.refresh(w)
    return _wallet_dict(w)


# ─── Ledger + summary ────────────────────────────────────────────────────────

@router.get("/ledger")
async def ledger(agent_id: str | None = None, limit: int = 100, db: AsyncSession = Depends(get_db)):
    query = select(PaymentRecord).order_by(PaymentRecord.timestamp.desc()).limit(limit)
    if agent_id:
        query = query.where(PaymentRecord.agent_id == agent_id)
    rows = (await db.execute(query)).scalars().all()
    return [_payment_dict(p) for p in rows]


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    """Totals + spend-by-agent for the payments dashboard."""
    total_settled = (await db.execute(
        select(func.coalesce(func.sum(PaymentRecord.amount_usdc), 0.0))
        .where(PaymentRecord.settled == True)  # noqa: E712
    )).scalar_one()
    count = (await db.execute(
        select(func.count()).select_from(PaymentRecord).where(PaymentRecord.settled == True)  # noqa: E712
    )).scalar_one()
    blocked = (await db.execute(
        select(func.count()).select_from(PaymentRecord).where(PaymentRecord.settled == False)  # noqa: E712
    )).scalar_one()
    by_agent_rows = (await db.execute(
        select(PaymentRecord.agent_id, func.coalesce(func.sum(PaymentRecord.amount_usdc), 0.0),
               func.count())
        .where(PaymentRecord.settled == True)  # noqa: E712
        .group_by(PaymentRecord.agent_id)
    )).all()
    fee_rate = 0.03
    return {
        "total_settled_usdc": round(total_settled, 6),
        "settled_count": count,
        "blocked_count": blocked,
        "fee_rate": fee_rate,
        "fees_captured_usdc": round(total_settled * fee_rate, 6),
        "by_agent": [
            {"agent_id": a, "spent_usdc": round(s, 6), "calls": c}
            for a, s, c in sorted(by_agent_rows, key=lambda r: r[1], reverse=True)
        ],
    }


# ─── Pricing + push ──────────────────────────────────────────────────────────

@router.get("/pricing")
async def get_pricing(gateway_id: str = "crm-agent", org: str = Depends(get_current_org)):
    return {"gateway_id": gateway_id, **_gateway_pricing(org, gateway_id)}


@router.post("/pricing")
async def set_pricing(body: PricingConfig, gateway_id: str = "crm-agent", org: str = Depends(get_current_org)):
    _pricing[org][gateway_id] = body.model_dump()
    return {"gateway_id": gateway_id, **_pricing[org][gateway_id]}


@router.post("/push")
async def push_payments(gateway_id: str = "crm-agent", db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Push the full payment config (pricing + wallet balances) to a gateway."""
    gateway = await db.get(Gateway, gateway_id)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    payload = await build_payment_config(db, gateway_id, org)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{gateway.endpoint}/config/payments", json=payload)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to push: {exc}") from None
    return {"gateway_id": gateway_id, "pushed": True, "wallets": len(payload["wallets"])}


async def build_payment_config(db: AsyncSession, gateway_id: str, org: str = "default") -> dict:
    """Assemble the gateway payment bundle: pricing policy + org's wallet balances.

    Called from push paths (push_service, lifecycle) where the org is the
    gateway's own org_id; defaults to "default" for single-org/back-compat.
    """
    wallets = (await db.execute(scoped(select(Wallet), Wallet, org))).scalars().all()
    pricing = _gateway_pricing(org, gateway_id)
    return {
        **pricing,
        "wallets": [
            {
                "agent_id": w.agent_id, "balance_usdc": w.balance_usdc,
                "address": w.address, "daily_limit_usdc": w.daily_limit_usdc,
                "per_call_limit_usdc": w.per_call_limit_usdc,
                "spent_today_usdc": w.spent_today_usdc, "status": w.status,
            }
            for w in wallets
        ],
    }


# ─── Serializers ─────────────────────────────────────────────────────────────

def _wallet_dict(w: Wallet) -> dict:
    return {
        "agent_id": w.agent_id, "address": w.address,
        "balance_usdc": round(w.balance_usdc, 6),
        "daily_limit_usdc": w.daily_limit_usdc,
        "per_call_limit_usdc": w.per_call_limit_usdc,
        "spent_today_usdc": round(w.spent_today_usdc, 6),
        "status": w.status,
    }


def _payment_dict(p: PaymentRecord) -> dict:
    return {
        "id": p.id, "agent_id": p.agent_id, "gateway_id": p.gateway_id,
        "action": p.action, "amount_usdc": round(p.amount_usdc, 6),
        "settled": p.settled, "tx_hash": p.tx_hash, "mode": p.mode,
        "source": p.source, "timestamp": p.timestamp.isoformat() if p.timestamp else "",
    }
