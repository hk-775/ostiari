"""Data models for the x402 payment gate."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Quote:
    """A price challenge — what must be paid for a call to proceed.

    Mirrors the x402 402-response header: amount + asset + payee + a nonce.
    In passthrough mode this is parsed from the tool's HTTP 402 response; in
    metered mode it's built from the gateway's price policy.
    """

    action: str
    amount_usdc: float
    pay_to: str = ""          # payee address (empty in metered/sim demo)
    asset: str = "USDC"
    network: str = "eip155:8453"
    scheme: str = "exact"
    atomic_amount: int = 0    # USDC base units from the x402 requirement
    nonce: str = ""
    source: str = "policy"    # "policy" (metered) | "tool_402" (passthrough)
    payment_required: str = ""  # original v2 PAYMENT-REQUIRED header


@dataclass
class Receipt:
    """Outcome of a settlement attempt."""

    settled: bool
    amount_usdc: float = 0.0
    tx_hash: str = ""         # "sim-..." in simulated mode, real Base tx hash when live
    reason: str = ""
    mode: str = "simulated"
    pending: bool = False     # signed/authorized, awaiting downstream settlement response
    payment_response: str = ""


@dataclass
class Wallet:
    """An agent's USDC wallet (in-gateway view).

    The control plane is the source of truth for balances/limits and pushes
    them to the gateway; the gateway mutates its local copy as it settles and
    reports spend back. `spent_today` drives the auto-pause (circuit-breaker
    style) when a daily limit is set.
    """

    agent_id: str
    balance_usdc: float = 0.0
    address: str = ""
    daily_limit_usdc: float | None = None
    per_call_limit_usdc: float | None = None
    spent_today_usdc: float = 0.0
    status: str = "active"    # active | paused

    def can_afford(self, amount: float) -> tuple[bool, str]:
        """Check balance, per-call cap, daily cap, and pause state."""
        if self.status != "active":
            return False, f"wallet {self.status}"
        # A charge must be positive. A negative amount would otherwise pass every
        # check below and CREDIT the wallet in debit() (balance -= negative), so a
        # malicious 402 like {"amount_usdc": -100} could refill a wallet and reset
        # its daily cap. Reject it here (defense in depth for all settle paths).
        if amount < 0:
            return False, f"invalid negative charge: ${amount:.4f}"
        if self.per_call_limit_usdc is not None and amount > self.per_call_limit_usdc:
            return False, (
                f"per-call limit exceeded: ${amount:.4f} > ${self.per_call_limit_usdc:.4f}"
            )
        if self.balance_usdc < amount:
            return False, (
                f"insufficient balance: ${self.balance_usdc:.4f} < ${amount:.4f}"
            )
        if self.daily_limit_usdc is not None and self.spent_today_usdc + amount > self.daily_limit_usdc:
            return False, (
                f"daily limit exceeded: ${self.spent_today_usdc + amount:.4f} > "
                f"${self.daily_limit_usdc:.4f}"
            )
        return True, ""

    def debit(self, amount: float) -> None:
        """Deduct a settled charge and advance daily spend; auto-pause if capped."""
        self.balance_usdc -= amount
        self.spent_today_usdc += amount
        if self.daily_limit_usdc is not None and self.spent_today_usdc >= self.daily_limit_usdc:
            self.status = "paused"


@dataclass
class PaymentDecision:
    """Result the proxy acts on: whether the call may proceed, and the receipt."""

    settled: bool
    free: bool = False        # True when no payment was required at all
    amount_usdc: float = 0.0
    balance_usdc: float = 0.0
    wallet_debited: bool = False
    reason: str = ""
    receipt: Receipt | None = None
    quote: Quote | None = None
    retry_header: dict[str, str] = field(default_factory=dict)  # X-PAYMENT for passthrough retry
