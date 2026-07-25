"""Payment gate — decides whether a tool call must be paid for, and settles it.

Three modes (per gateway, pushed from the control plane):
  - off:         never charge (default; preserves pre-payment behavior).
  - metered:     price the call from a policy BEFORE execution, then settle.
                 Ostiari is the payee (per-governed-call billing model).
  - passthrough: charge only when the downstream tool returns HTTP 402. The
                 tool is the payee; Ostiari pays from the agent wallet and
                 retries. This is native x402.

Pricing (metered) uses fnmatch patterns, first match wins, default free:
    pricing:
      mode: metered
      default: 0.0
      overrides:
        "web_search": 0.005
        "github.*":   0.002
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

from ostiari_gateway.payments.models import PaymentDecision, Quote, Wallet
from ostiari_gateway.payments.settler import Settler, SimulatedSettler

log = logging.getLogger("ostiari.sidecar.payments")


class PaymentGate:
    """Holds per-agent wallets + pricing policy; charges calls via a Settler."""

    def __init__(self, settler: Settler | None = None) -> None:
        self._settler: Settler = settler or SimulatedSettler()
        self._wallets: dict[str, Wallet] = {}
        self._mode: str = "off"
        self._default_price: float = 0.0
        self._overrides: dict[str, float] = {}

    # ─── Configuration (pushed from control plane) ──────────────────────────

    def configure(self, config: dict[str, Any]) -> None:
        """Apply a pricing/wallet config bundle."""
        self._mode = config.get("mode", "off")
        self._default_price = float(config.get("default", 0.0))
        self._overrides = {k: float(v) for k, v in (config.get("overrides") or {}).items()}
        for w in config.get("wallets", []) or []:
            self.upsert_wallet(w)
        log.info(
            "Payments configured: mode=%s, default=$%s, priced_patterns=%d, wallets=%d",
            self._mode, self._default_price, len(self._overrides), len(self._wallets),
        )

    def upsert_wallet(self, w: dict[str, Any]) -> None:
        """Create or update a single agent wallet from a dict."""
        agent_id = w["agent_id"]
        self._wallets[agent_id] = Wallet(
            agent_id=agent_id,
            balance_usdc=float(w.get("balance_usdc", 0.0)),
            address=w.get("address", ""),
            daily_limit_usdc=w.get("daily_limit_usdc"),
            per_call_limit_usdc=w.get("per_call_limit_usdc"),
            spent_today_usdc=float(w.get("spent_today_usdc", 0.0)),
            status=w.get("status", "active"),
        )

    # ─── Pricing ────────────────────────────────────────────────────────────

    def price_for(self, action: str) -> float:
        """Metered price for an action (0.0 == free). First matching pattern wins."""
        for pattern, price in self._overrides.items():
            if fnmatch.fnmatch(action, pattern):
                return price
        return self._default_price

    # ─── Gate decisions ──────────────────────────────────────────────────────

    async def charge_before(self, *, agent_id: str, action: str) -> PaymentDecision:
        """Metered pre-charge: price the call, settle it, decide.

        Called BEFORE execution. In `off`/`passthrough` mode nothing is charged
        here (passthrough charges reactively — see settle_402).
        """
        if self._mode != "metered":
            return PaymentDecision(settled=True, free=True)

        amount = self.price_for(action)
        if amount <= 0:
            return PaymentDecision(settled=True, free=True)

        return await self._settle(agent_id=agent_id, action=action, amount=amount,
                                  source="policy")

    async def settle_402(
        self, *, agent_id: str, action: str, quote: Quote
    ) -> PaymentDecision:
        """Passthrough: settle a charge demanded by a tool's HTTP 402 response.

        Called AFTER the tool returns 402, before the retry. Only active in
        passthrough mode; in other modes a downstream 402 is passed through
        untouched (caller decides).
        """
        return await self._settle(agent_id=agent_id, action=action,
                                  amount=quote.amount_usdc, source="tool_402",
                                  pay_to=quote.pay_to)

    async def _settle(
        self, *, agent_id: str, action: str, amount: float, source: str, pay_to: str = ""
    ) -> PaymentDecision:
        wallet = self._wallets.get(agent_id)
        if wallet is None:
            # No wallet on file — treat as unfunded rather than free, so payment
            # can't be bypassed by simply not provisioning a wallet.
            return PaymentDecision(
                settled=False, amount_usdc=amount, balance_usdc=0.0,
                reason=f"no wallet provisioned for agent '{agent_id}'",
            )

        quote = Quote(action=action, amount_usdc=amount, pay_to=pay_to, source=source)
        receipt = await self._settler.settle(quote=quote, wallet=wallet)
        retry_header: dict[str, str] = {}
        if receipt.settled and source == "tool_402":
            # Minimal X-PAYMENT proof for the tool retry. A live build would put
            # the signed settlement/authorization here; the sim uses the tx id.
            retry_header = {"X-PAYMENT": receipt.tx_hash}

        return PaymentDecision(
            settled=receipt.settled,
            amount_usdc=amount,
            balance_usdc=wallet.balance_usdc,
            reason=receipt.reason,
            receipt=receipt,
            quote=quote,
            retry_header=retry_header,
        )

    # ─── Introspection (for /payments status + reporting) ────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def settler_mode(self) -> str:
        """'simulated' or 'live' — which settlement backend is active."""
        return self._settler.mode

    def get_wallet(self, agent_id: str) -> Wallet | None:
        return self._wallets.get(agent_id)

    def status(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "settler": self._settler.mode,
            "default_price_usdc": self._default_price,
            "priced_patterns": self._overrides,
            "wallets": [
                {
                    "agent_id": w.agent_id, "balance_usdc": round(w.balance_usdc, 6),
                    "daily_limit_usdc": w.daily_limit_usdc,
                    "per_call_limit_usdc": w.per_call_limit_usdc,
                    "spent_today_usdc": round(w.spent_today_usdc, 6),
                    "status": w.status,
                }
                for w in self._wallets.values()
            ],
        }


def parse_402(body: Any, status_code: int, action: str) -> Quote | None:
    """Build a Quote from a tool's HTTP 402 response, or None if not a 402.

    Accepts the price in the JSON body (`amount_usdc`/`amount`, `pay_to`,
    `nonce`). A fuller x402 build would also read the standard `X-Payment-*`
    headers; the body form keeps the demo tool simple.
    """
    if status_code != 402:
        return None
    data = body if isinstance(body, dict) else {}
    amount = data.get("amount_usdc", data.get("amount", 0.0))
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0.0
    # A tool must never demand a negative charge — that would credit the wallet
    # on settle. Clamp to 0 (treated as free) rather than trusting the downstream.
    if amount < 0:
        amount = 0.0
    return Quote(
        action=action,
        amount_usdc=amount,
        pay_to=data.get("pay_to", ""),
        asset=data.get("asset", "USDC"),
        nonce=data.get("nonce", ""),
        source="tool_402",
    )
