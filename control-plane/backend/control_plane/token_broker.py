"""Token broker economics.

Ostiari/AxonLLM buys LLM tokens at negotiated bulk discounts and routes
customer traffic through that pool, charging a margin that's still below the
customer's retail price. Everyone wins: the customer pays less than list, we
keep a spread.

Three prices per unit of usage:
  - retail:   what the customer would pay a provider directly (public rate).
  - our_cost: what we pay from the bulk pool = retail * (1 - bulk_discount).
  - charged:  what we bill the customer = our_cost * (1 + markup).

Derived: customer_savings = retail - charged; our_margin = charged - our_cost.
The invariant we surface: charged < retail (customer saves) AND charged >
our_cost (we profit) whenever (1 - bulk_discount)*(1 + markup) < 1, i.e. the
markup doesn't eat the whole discount.

bulk_discount and markup are operator assumptions (like the ROI cost model),
not measured — the point is a transparent, tunable margin/savings story.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_BULK_DISCOUNT = 0.25   # 25% off retail via volume agreements
DEFAULT_MARKUP = 0.12          # 12% markup over our bulk cost


@dataclass
class ModelBrokerRow:
    model: str
    calls: int = 0
    tokens: int = 0
    retail_usd: float = 0.0
    our_cost_usd: float = 0.0
    charged_usd: float = 0.0

    @property
    def customer_savings_usd(self) -> float:
        return self.retail_usd - self.charged_usd

    @property
    def margin_usd(self) -> float:
        return self.charged_usd - self.our_cost_usd


@dataclass
class BrokerReport:
    bulk_discount: float
    markup: float
    total_retail_usd: float = 0.0
    total_our_cost_usd: float = 0.0
    total_charged_usd: float = 0.0
    total_tokens: int = 0
    models: list[ModelBrokerRow] = field(default_factory=list)

    @property
    def customer_savings_usd(self) -> float:
        return round(self.total_retail_usd - self.total_charged_usd, 2)

    @property
    def margin_usd(self) -> float:
        return round(self.total_charged_usd - self.total_our_cost_usd, 2)

    @property
    def savings_pct(self) -> float:
        if self.total_retail_usd <= 0:
            return 0.0
        return round((self.total_retail_usd - self.total_charged_usd) / self.total_retail_usd * 100, 1)


def compute_broker(
    records: Iterable[Any],
    *,
    bulk_discount: float = DEFAULT_BULK_DISCOUNT,
    markup: float = DEFAULT_MARKUP,
) -> BrokerReport:
    """Aggregate usage records into broker economics.

    Each record's `cost_usd` is treated as the retail price (what it would cost
    at list). our_cost and charged are derived per the discount/markup.
    """
    bulk_discount = max(0.0, min(bulk_discount, 0.95))
    markup = max(0.0, markup)
    by_model: dict[str, ModelBrokerRow] = {}

    for r in records:
        model = getattr(r, "model", None) or "unknown"
        retail = float(getattr(r, "cost_usd", 0.0) or 0.0)
        tokens = int(getattr(r, "total_tokens", 0) or 0)
        our_cost = retail * (1 - bulk_discount)
        charged = our_cost * (1 + markup)

        row = by_model.get(model)
        if row is None:
            row = ModelBrokerRow(model=model)
            by_model[model] = row
        row.calls += 1
        row.tokens += tokens
        row.retail_usd += retail
        row.our_cost_usd += our_cost
        row.charged_usd += charged

    models = sorted(by_model.values(), key=lambda m: m.retail_usd, reverse=True)
    return BrokerReport(
        bulk_discount=bulk_discount,
        markup=markup,
        total_retail_usd=round(sum(m.retail_usd for m in models), 6),
        total_our_cost_usd=round(sum(m.our_cost_usd for m in models), 6),
        total_charged_usd=round(sum(m.charged_usd for m in models), 6),
        total_tokens=sum(m.tokens for m in models),
        models=models,
    )
