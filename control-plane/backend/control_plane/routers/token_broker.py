"""Token broker API — bulk-buy/resell economics over usage records.

Reports customer savings and our margin from routing LLM traffic through a
discounted token pool. The bulk discount and markup are operator-editable
assumptions, persisted in the state file (like the ROI cost model).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import token_broker
from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import UsageRecord
from control_plane.models.scoping import scoped

router = APIRouter(prefix="/api/token-broker", tags=["token-broker"])

# Operator-editable margin config; persisted via the state file (see app.py).
_config: dict[str, dict] = defaultdict(
    lambda: {
        "bulk_discount": token_broker.DEFAULT_BULK_DISCOUNT,
        "markup": token_broker.DEFAULT_MARKUP,
    }
)


class BrokerConfig(BaseModel):
    bulk_discount: float
    markup: float


@router.get("/config")
async def get_config(org: str = Depends(get_current_org)):
    return {**_config[org], "customized": _config[org].get("_customized", False)}


@router.post("/config")
async def set_config(body: BrokerConfig, org: str = Depends(get_current_org)):
    _config[org]["bulk_discount"] = max(0.0, min(body.bulk_discount, 0.95))
    _config[org]["markup"] = max(0.0, body.markup)
    _config[org]["_customized"] = True
    return {**_config[org], "customized": True}


@router.post("/config/reset")
async def reset_config(org: str = Depends(get_current_org)):
    _config[org]["bulk_discount"] = token_broker.DEFAULT_BULK_DISCOUNT
    _config[org]["markup"] = token_broker.DEFAULT_MARKUP
    _config[org]["_customized"] = False
    return {**_config[org], "customized": False}


@router.get("/report")
async def report(period_days: int = Query(default=30, le=365), db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Broker economics over usage in the window: savings, cost, margin per model."""
    since = datetime.now(timezone.utc) - timedelta(days=period_days)
    # Scope to the caller's org: this already reads THEIR broker config, so
    # aggregating every tenant's usage against it reported one org's economics
    # over another's spend.
    records = (await db.execute(
        scoped(select(UsageRecord).where(UsageRecord.timestamp >= since), UsageRecord, org)
    )).scalars().all()

    rep = token_broker.compute_broker(
        records,
        bulk_discount=_config[org]["bulk_discount"],
        markup=_config[org]["markup"],
    )
    return {
        "period_days": period_days,
        "bulk_discount": rep.bulk_discount,
        "markup": rep.markup,
        "total_retail_usd": rep.total_retail_usd,
        "total_our_cost_usd": rep.total_our_cost_usd,
        "total_charged_usd": rep.total_charged_usd,
        "total_tokens": rep.total_tokens,
        "customer_savings_usd": rep.customer_savings_usd,
        "savings_pct": rep.savings_pct,
        "margin_usd": rep.margin_usd,
        "models": [
            {
                "model": m.model, "calls": m.calls, "tokens": m.tokens,
                "retail_usd": round(m.retail_usd, 6),
                "our_cost_usd": round(m.our_cost_usd, 6),
                "charged_usd": round(m.charged_usd, 6),
                "customer_savings_usd": round(m.customer_savings_usd, 6),
                "margin_usd": round(m.margin_usd, 6),
            }
            for m in rep.models
        ],
    }
