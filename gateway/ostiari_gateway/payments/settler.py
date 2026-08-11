"""Settlement backends for simulated and live x402 payments."""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from ostiari_gateway.payments.models import Quote, Receipt, Wallet

_DEFAULT_USDC_ASSETS = {
    "eip155:8453": {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    },
    "eip155:84532": {
        "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
    },
}


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
    """Live x402 v2 buyer for downstream HTTP 402 resources.

    The official x402 SDK owns challenge parsing, scheme selection, signing,
    and retrying the request. Ostiari remains responsible for its per-agent
    policy wallet: ``settle`` atomically authorizes/debits that budget before
    the signed request is sent, and ``confirm`` requires the downstream
    ``PAYMENT-RESPONSE`` before paid output is exposed.

    Live metered mode (Ostiari acting as the seller) is intentionally rejected.
    It requires the caller, rather than the gateway, to provide a payment
    signature and is a separate resource-server contract.
    """

    mode = "live"

    def __init__(
        self,
        private_key: str = "",
        *,
        requester: Callable[..., Awaitable[httpx.Response]] | None = None,
        allowed_assets: dict[str, list[str] | set[str]] | None = None,
    ) -> None:
        self._private_key = private_key.strip()
        self._requester = requester
        self._sdk: tuple[Any, Any, Any, Any] | None = None
        self._allowed_assets = {
            network: {asset.lower() for asset in assets}
            for network, assets in _DEFAULT_USDC_ASSETS.items()
        }
        for network, assets in (allowed_assets or {}).items():
            self._allowed_assets.setdefault(str(network), set()).update(
                str(asset).lower() for asset in assets
            )
        if requester is None:
            if not self._private_key:
                raise RuntimeError(
                    "OSTIARI_X402_PRIVATE_KEY is required when OSTIARI_X402_MODE=live"
                )
            self._sdk = self._load_sdk()

    @staticmethod
    def _load_sdk() -> tuple[Any, Any, Any, Any]:
        try:
            from x402 import x402Client
            from x402.http.clients.httpx import x402HttpxClient
            from x402.mechanisms.evm import EthAccountSigner
            from x402.mechanisms.evm.exact import ExactEvmScheme
        except ImportError as exc:
            raise RuntimeError(
                "Live x402 requires the gateway payments extra: "
                "pip install 'ostiari-gateway[payments]'"
            ) from exc
        return x402Client, x402HttpxClient, EthAccountSigner, ExactEvmScheme

    @staticmethod
    def parse_allowed_assets(raw: str) -> dict[str, list[str]]:
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid OSTIARI_X402_ALLOWED_ASSETS JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "OSTIARI_X402_ALLOWED_ASSETS must be a JSON object"
            )
        result: dict[str, list[str]] = {}
        for network, assets in payload.items():
            if not isinstance(assets, list) or not all(
                isinstance(asset, str) and asset for asset in assets
            ):
                raise RuntimeError(
                    "OSTIARI_X402_ALLOWED_ASSETS values must be address arrays"
                )
            result[str(network)] = assets
        return result

    def validate_quote(self, quote: Quote, wallet: Wallet | None = None) -> str:
        if quote.source != "tool_402":
            return "live x402 supports passthrough mode only"
        if not quote.payment_required:
            return "downstream 402 did not include a valid PAYMENT-REQUIRED challenge"
        if quote.atomic_amount <= 0 or quote.amount_usdc <= 0:
            return "downstream x402 challenge has a non-positive amount"
        if not quote.network.startswith("eip155:"):
            return f"unsupported x402 network: {quote.network or 'unknown'}"
        if quote.scheme != "exact":
            return f"unsupported x402 scheme: {quote.scheme or 'unknown'}"
        allowed = self._allowed_assets.get(quote.network, set())
        if quote.asset.lower() not in allowed:
            return (
                "x402 asset is not an approved 6-decimal USDC contract for "
                f"{quote.network}"
            )
        if wallet is not None:
            ok, reason = wallet.can_afford(quote.amount_usdc)
            return "" if ok else reason
        return ""

    async def settle(
        self, *, quote: Quote, wallet: Wallet, payment_header: str | None = None
    ) -> Receipt:
        reason = self.validate_quote(quote, wallet)
        if reason:
            return Receipt(
                settled=False,
                amount_usdc=quote.amount_usdc,
                reason=reason,
                mode=self.mode,
            )
        wallet.debit(quote.amount_usdc)
        return Receipt(
            settled=True,
            amount_usdc=quote.amount_usdc,
            mode=self.mode,
            pending=True,
        )

    async def request(
        self,
        *,
        quote: Quote,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.Response:
        """Execute a paid retry with the official x402 HTTPX adapter."""
        if self._requester is not None:
            return await self._requester(
                quote=quote,
                method=method,
                url=url,
                params=params,
                json_body=json_body,
                headers=headers,
                timeout=timeout,
            )

        if self._sdk is None:  # pragma: no cover - constructor guarantees this
            raise RuntimeError("x402 SDK is not configured")

        async with self._build_http_client(quote) as paid_http:
            request_method = getattr(paid_http, method.lower(), None)
            if request_method is None:
                raise RuntimeError(f"x402 client does not support HTTP method {method}")
            return await request_method(
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )

    def _build_http_client(self, quote: Quote) -> Any:
        """Build the official signer/client/HTTP adapter stack."""
        if self._sdk is None:
            raise RuntimeError("x402 SDK is not configured")
        x402_client_cls, httpx_client_cls, signer_cls, scheme_cls = self._sdk
        signer = signer_cls(self._private_key)
        client = x402_client_cls(
            max_amount=max(quote.atomic_amount, 1),
            payment_selector=self._selector_for(quote),
        )
        client.register("eip155:*", scheme_cls(signer))
        return httpx_client_cls(client)

    @classmethod
    def confirm(
        cls,
        *,
        quote: Quote,
        status_code: int,
        payment_headers: dict[str, str] | None,
    ) -> Receipt:
        """Validate the downstream settlement response and extract its tx id."""
        headers = payment_headers or {}
        raw = headers.get("payment-response") or headers.get("x-payment-response") or ""
        if status_code >= 400:
            return Receipt(
                settled=False,
                amount_usdc=quote.amount_usdc,
                reason=f"paid downstream request returned HTTP {status_code}",
                mode=cls.mode,
            )
        if not raw:
            return Receipt(
                settled=False,
                amount_usdc=quote.amount_usdc,
                reason="paid downstream response omitted PAYMENT-RESPONSE",
                mode=cls.mode,
            )
        try:
            payload = cls._decode_header(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            return Receipt(
                settled=False,
                amount_usdc=quote.amount_usdc,
                reason=f"invalid PAYMENT-RESPONSE: {exc}",
                mode=cls.mode,
            )
        if payload.get("success") is not True:
            return Receipt(
                settled=False,
                amount_usdc=quote.amount_usdc,
                reason=str(payload.get("errorReason") or "x402 settlement was not confirmed"),
                mode=cls.mode,
                payment_response=raw,
            )
        network = str(payload.get("network") or "")
        if network and network != quote.network:
            return Receipt(
                settled=False,
                amount_usdc=quote.amount_usdc,
                reason=(
                    "PAYMENT-RESPONSE network does not match the approved "
                    f"challenge: {network}"
                ),
                mode=cls.mode,
                payment_response=raw,
            )
        tx_hash = str(
            payload.get("transaction")
            or payload.get("transactionHash")
            or payload.get("txHash")
            or ""
        )
        if not tx_hash:
            return Receipt(
                settled=False,
                amount_usdc=quote.amount_usdc,
                reason="PAYMENT-RESPONSE did not include a transaction reference",
                mode=cls.mode,
                payment_response=raw,
            )
        return Receipt(
            settled=True,
            amount_usdc=quote.amount_usdc,
            tx_hash=tx_hash,
            mode=cls.mode,
            payment_response=raw,
        )

    @staticmethod
    def _decode_header(value: str) -> dict[str, Any]:
        padded = value + ("=" * (-len(value) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        payload = json.loads(decoded)
        if not isinstance(payload, dict):
            raise ValueError("header payload must be a JSON object")
        return payload

    @staticmethod
    def _field(value: Any, *names: str) -> Any:
        for name in names:
            if isinstance(value, dict) and name in value:
                return value[name]
            if hasattr(value, name):
                return getattr(value, name)
        return None

    @classmethod
    def _selector_for(cls, quote: Quote) -> Callable[[int, list[Any]], Any]:
        """Pin the SDK's fresh challenge to the requirement already approved."""

        def select(_version: int, requirements: list[Any]) -> Any:
            for requirement in requirements:
                amount = cls._field(requirement, "amount", "max_amount_required")
                network = cls._field(requirement, "network")
                pay_to = cls._field(requirement, "pay_to", "payTo")
                scheme = cls._field(requirement, "scheme")
                asset = cls._field(requirement, "asset")
                if (
                    str(amount) == str(quote.atomic_amount)
                    and str(network) == quote.network
                    and str(pay_to).lower() == quote.pay_to.lower()
                    and str(scheme) == quote.scheme
                    and str(asset).lower() == quote.asset.lower()
                ):
                    return requirement
            raise RuntimeError(
                "downstream x402 requirements changed after Ostiari approved the quote"
            )

        return select
