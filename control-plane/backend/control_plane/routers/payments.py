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
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway, PaymentRecord, Wallet
from control_plane.models.scoping import get_scoped, org_of_gateway, scoped, stamp

router = APIRouter(prefix="/api/payments", tags=["payments"])

# Per-gateway pricing policy (mode + priced patterns). Kept in the control
# plane; pushed to gateways alongside wallet balances. Defaults to off.
_pricing: dict[str, dict[str, dict]] = defaultdict(dict)


def _gateway_pricing(org: str, gateway_id: str) -> dict:
    return _pricing[org].get(gateway_id, {"mode": "off", "default": 0.0, "overrides": {}})


# ─── Schemas ─────────────────────────────────────────────────────────────────

class WalletUpsert(BaseModel):
    agent_id: str
    balance_usdc: float = Field(default=0.0, ge=0.0)
    address: str = ""
    daily_limit_usdc: float | None = Field(default=None, ge=0.0)
    per_call_limit_usdc: float | None = Field(default=None, ge=0.0)


class WalletPatch(BaseModel):
    daily_limit_usdc: float | None = Field(default=None, ge=0.0)
    per_call_limit_usdc: float | None = Field(default=None, ge=0.0)
    status: str | None = None  # active | paused


class FundRequest(BaseModel):
    amount_usdc: float = Field(gt=0.0)


class PricingConfig(BaseModel):
    mode: str = "off"                     # off | metered | passthrough
    default: float = Field(default=0.0, ge=0.0)
    overrides: dict[str, float] = Field(default_factory=dict)


class PaymentIngest(BaseModel):
    event_id: str | None = Field(default=None, min_length=1, max_length=64)
    agent_id: str
    gateway_id: str = ""
    action: str = ""
    amount_usdc: float = Field(default=0.0, ge=0.0)
    settled: bool = True
    wallet_debited: bool | None = None
    tx_hash: str = ""
    mode: str = "simulated"
    source: str = "policy"
    reason: str = ""


# ─── Ingest (from gateways) ──────────────────────────────────────────────────

@router.post("/ingest")
async def ingest_payment(body: PaymentIngest, db: AsyncSession = Depends(get_db)):
    """Record a charge reported by a gateway; mirror the settled balance in DB.

    The gateway authorizes against its local/shared policy wallet and reports
    here so the ledger, summary, and dashboard reflect the outcome. The DB
    wallet is decremented when that allowance was consumed, which can be true
    for an unconfirmed live attempt without falsely marking it settled.
    """
    # Unauthenticated gateway path — org comes from the reporting gateway's row
    # (default when the gateway is unknown/empty), not a user token.
    rec_org = await org_of_gateway(db, body.gateway_id)
    record, created = await _upsert_payment(db, body, rec_org)
    if created and record.wallet_debited:
        w = await get_scoped(db, Wallet, body.agent_id, rec_org)
        if w is not None:
            w.balance_usdc = max(0.0, w.balance_usdc - body.amount_usdc)
            w.spent_today_usdc += body.amount_usdc
            if w.daily_limit_usdc is not None and w.spent_today_usdc >= w.daily_limit_usdc:
                w.status = "paused"
    await db.commit()
    return {
        "recorded": created,
        "duplicate": not created,
        "id": record.id,
    }


def _payment_values(body: PaymentIngest, org: str) -> dict:
    return {
        "org_id": org,
        "event_id": body.event_id,
        "agent_id": body.agent_id,
        "gateway_id": body.gateway_id,
        "action": body.action,
        "amount_usdc": body.amount_usdc,
        "settled": body.settled,
        "wallet_debited": (
            body.settled if body.wallet_debited is None else body.wallet_debited
        ),
        "tx_hash": body.tx_hash,
        "mode": body.mode,
        "source": body.source,
        "reason": body.reason,
    }


def _same_payment(record: PaymentRecord, values: dict) -> bool:
    exact_fields = (
        "org_id",
        "event_id",
        "agent_id",
        "gateway_id",
        "action",
        "settled",
        "wallet_debited",
        "tx_hash",
        "mode",
        "source",
        "reason",
    )
    return all(getattr(record, field) == values[field] for field in exact_fields) and (
        abs(float(record.amount_usdc) - float(values["amount_usdc"])) < 1e-12
    )


async def _upsert_payment(
    db: AsyncSession,
    body: PaymentIngest,
    org: str,
) -> tuple[PaymentRecord, bool]:
    values = _payment_values(body, org)
    if not body.event_id:
        record = PaymentRecord(**values)
        db.add(record)
        await db.flush()
        return record, True

    dialect = db.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(PaymentRecord)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[PaymentRecord.gateway_id, PaymentRecord.event_id]
        )
        .returning(PaymentRecord.id)
    )
    record_id = (await db.execute(stmt)).scalar_one_or_none()
    if record_id is not None:
        record = await db.get(PaymentRecord, record_id)
        if record is None:  # pragma: no cover - defensive against a broken driver
            raise RuntimeError("Inserted payment record could not be reloaded")
        return record, True

    record = (
        await db.execute(
            select(PaymentRecord).where(
                PaymentRecord.gateway_id == body.gateway_id,
                PaymentRecord.event_id == body.event_id,
            )
        )
    ).scalar_one()
    if not _same_payment(record, values):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Payment event '{body.event_id}' was already recorded with "
                "different data"
            ),
        )
    return record, False


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
async def ledger(agent_id: str | None = None, limit: int = 100, db: AsyncSession = Depends(get_db),
                 org: str = Depends(get_current_org)):
    query = scoped(select(PaymentRecord), PaymentRecord, org).order_by(
        PaymentRecord.timestamp.desc()).limit(limit)
    if agent_id:
        query = query.where(PaymentRecord.agent_id == agent_id)
    rows = (await db.execute(query)).scalars().all()
    return [_payment_dict(p) for p in rows]


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Totals + spend-by-agent for the payments dashboard (scoped to the org)."""
    total_settled = (await db.execute(
        scoped(select(func.coalesce(func.sum(PaymentRecord.amount_usdc), 0.0)), PaymentRecord, org)
        .where(PaymentRecord.settled == True)  # noqa: E712
    )).scalar_one()
    count = (await db.execute(
        scoped(select(func.count()).select_from(PaymentRecord), PaymentRecord, org)
        .where(PaymentRecord.settled == True)  # noqa: E712
    )).scalar_one()
    blocked = (await db.execute(
        scoped(select(func.count()).select_from(PaymentRecord), PaymentRecord, org)
        .where(PaymentRecord.settled == False)  # noqa: E712
    )).scalar_one()
    by_agent_rows = (await db.execute(
        scoped(select(PaymentRecord.agent_id, func.coalesce(func.sum(PaymentRecord.amount_usdc), 0.0),
               func.count()), PaymentRecord, org)
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
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    payload = await build_payment_config(db, gateway_id, org)
    from control_plane.services.push_service import gateway_config_headers
    async with httpx.AsyncClient(
        timeout=10.0, headers=gateway_config_headers()
    ) as client:
        try:
            resp = await client.post(
                f"{gateway.endpoint}/config/payments", json=payload,
            )
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
        "id": p.id, "event_id": p.event_id or "",
        "agent_id": p.agent_id, "gateway_id": p.gateway_id,
        "action": p.action, "amount_usdc": round(p.amount_usdc, 6),
        "settled": p.settled, "wallet_debited": p.wallet_debited,
        "tx_hash": p.tx_hash, "mode": p.mode,
        "source": p.source, "reason": p.reason,
        "timestamp": p.timestamp.isoformat() if p.timestamp else "",
    }
