"""Settlement backends — the swappable seam between demo and real x402.

`Settler` is the only component that differs between a self-contained demo and
a live on-chain deployment:

  - SimulatedSettler: moves USDC in the in-gateway wallet ledger. No chain, no
    keys, no facilitator. This is what the demo uses.
  - X402Settler (stub): would verify the agent's signed EIP-3009 authorization
    and POST it to an x402 facilitator, which broadcasts the USDC transfer on
    Base. Left unimplemented on purpose — going live is dropping this in plus a
    funded wallet and a facilitator URL; nothing else in the gate changes.

Both satisfy the same Protocol, so the gate and everything above it (wallet
model, dashboard, CLI, traces) is written once.
"""

from __future__ import annotations

from typing import Protocol

from ostiari_gateway.payments.models import Quote, Receipt, Wallet


class Settler(Protocol):
    """Settles a quoted USDC charge against an agent's wallet."""

    @property
    def mode(self) -> str: ...

    async def settle(
        self, *, quote: Quote, wallet: Wallet, payment_header: str | None = None
    ) -> Receipt:
        """Attempt to settle `quote` from `wallet`. Never raises — returns a
        Receipt with settled=False and a reason on failure."""
        ...


class SimulatedSettler:
    """Demo settler — debits the local USDC ledger, no blockchain involved."""

    mode = "simulated"

    def __init__(self) -> None:
        self._counter = 0

    async def settle(
        self, *, quote: Quote, wallet: Wallet, payment_header: str | None = None
    ) -> Receipt:
        ok, reason = wallet.can_afford(quote.amount_usdc)
        if not ok:
            return Receipt(settled=False, amount_usdc=quote.amount_usdc,
                           reason=reason, mode=self.mode)
        wallet.debit(quote.amount_usdc)
        self._counter += 1
        # Deterministic pseudo tx id (no wall-clock/random so tests are stable).
        tx = f"sim-{quote.action}-{self._counter}"
        return Receipt(settled=True, amount_usdc=quote.amount_usdc,
                       tx_hash=tx, mode=self.mode)


class X402Settler:
    """Live settler (stub) — settles real USDC on Base via an x402 facilitator.

    Not implemented: this is the single seam that turns the simulated demo into
    a live deployment. A real build would, in settle():
      1. verify the agent's signed EIP-3009 transferWithAuthorization (the
         X-PAYMENT header) against the quote,
      2. POST it to the facilitator's /settle endpoint (Coinbase-hosted or
         self-hosted) to broadcast on Base,
      3. return Receipt(settled=True, tx_hash=<base tx>) on confirmation.
    Private keys live in a KMS/secrets manager, never here or in the DB.
    """

    mode = "live"

    def __init__(self, facilitator_url: str, signer: object | None = None) -> None:
        self.facilitator_url = facilitator_url
        self.signer = signer

    async def settle(
        self, *, quote: Quote, wallet: Wallet, payment_header: str | None = None
    ) -> Receipt:
        raise NotImplementedError(
            "X402Settler is a stub. Provide a facilitator client + signer to go live."
        )
