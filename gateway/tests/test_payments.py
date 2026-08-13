"""Tests for the x402 payment gate: wallets, pricing, settlement, and the
metered + passthrough flows through the sidecar proxy."""

import base64
import json

import httpx
import pytest
from ostiari_gateway.models import PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.payments import (
    DisabledSettler,
    Quote,
    SimulatedSettler,
    Wallet,
    X402Settler,
    parse_402,
)
from ostiari_gateway.payments import (
    PaymentGate as Gate,
)
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient

_BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _payment_header(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def _required_header(amount: int = 5000, pay_to: str = "0xmerchant") -> str:
    return _payment_header({
        "x402Version": 2,
        "resource": {
            "url": "https://tool.example/premium",
            "description": "Premium search",
            "mimeType": "application/json",
        },
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": str(amount),
            "asset": _BASE_USDC,
            "payTo": pay_to,
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USDC", "version": "2"},
        }],
    })


def _response_header(*, success: bool = True, tx: str = "0xsettled") -> str:
    payload = {
        "success": success,
        "transaction": tx,
        "network": "eip155:8453",
    }
    if not success:
        payload["errorReason"] = "facilitator rejected payment"
    return _payment_header(payload)

# ─── Wallet model (unit) ─────────────────────────────────────────────────────

class TestWallet:
    def test_can_afford_ok(self):
        w = Wallet(agent_id="a", balance_usdc=1.0)
        assert w.can_afford(0.5) == (True, "")

    def test_insufficient_balance(self):
        w = Wallet(agent_id="a", balance_usdc=0.001)
        ok, reason = w.can_afford(0.5)
        assert ok is False and "insufficient balance" in reason

    def test_per_call_limit(self):
        w = Wallet(agent_id="a", balance_usdc=100, per_call_limit_usdc=0.01)
        ok, reason = w.can_afford(0.5)
        assert ok is False and "per-call limit" in reason

    def test_daily_limit(self):
        w = Wallet(agent_id="a", balance_usdc=100, daily_limit_usdc=1.0, spent_today_usdc=0.9)
        ok, reason = w.can_afford(0.5)
        assert ok is False and "daily limit" in reason

    def test_paused_wallet_rejected(self):
        w = Wallet(agent_id="a", balance_usdc=100, status="paused")
        ok, reason = w.can_afford(0.001)
        assert ok is False and "paused" in reason

    def test_debit_advances_spend(self):
        w = Wallet(agent_id="a", balance_usdc=1.0)
        w.debit(0.3)
        assert w.balance_usdc == pytest.approx(0.7)
        assert w.spent_today_usdc == pytest.approx(0.3)

    def test_debit_auto_pauses_at_daily_limit(self):
        w = Wallet(agent_id="a", balance_usdc=10, daily_limit_usdc=0.5)
        w.debit(0.5)
        assert w.status == "paused"


# ─── SimulatedSettler (unit) ─────────────────────────────────────────────────

class TestSimulatedSettler:
    async def test_settles_and_debits(self):
        s = SimulatedSettler()
        w = Wallet(agent_id="a", balance_usdc=1.0)
        r = await s.settle(quote=Quote(action="x", amount_usdc=0.2), wallet=w)
        assert r.settled is True
        assert r.tx_hash.startswith("sim-")
        assert w.balance_usdc == pytest.approx(0.8)

    async def test_rejects_when_unaffordable(self):
        s = SimulatedSettler()
        w = Wallet(agent_id="a", balance_usdc=0.01)
        r = await s.settle(quote=Quote(action="x", amount_usdc=0.5), wallet=w)
        assert r.settled is False
        assert "insufficient balance" in r.reason
        assert w.balance_usdc == pytest.approx(0.01)  # untouched


class TestDisabledSettler:
    async def test_never_debits(self):
        s = DisabledSettler()
        w = Wallet(agent_id="a", balance_usdc=1.0)

        r = await s.settle(quote=Quote(action="x", amount_usdc=0.2), wallet=w)

        assert r.settled is False
        assert r.mode == "off"
        assert "disabled" in r.reason
        assert w.balance_usdc == pytest.approx(1.0)

    async def test_shared_wallet_is_not_debited(self):
        class Store:
            def __init__(self):
                self.debits = 0

            def upsert_wallet(self, agent_id, wallet):
                return None

            def wallet_debit(self, agent_id, amount):
                self.debits += 1
                return True, "", 0.8

        store = Store()
        gate = Gate(settler=DisabledSettler())
        gate.configure({
            "mode": "metered",
            "default": 0.2,
            "wallets": [{"agent_id": "a", "balance_usdc": 1.0}],
        })
        gate.attach_shared_store(store)

        result = await gate.charge_before(agent_id="a", action="x")

        assert result.settled is False
        assert "disabled" in result.reason
        assert store.debits == 0
        assert gate.get_wallet("a").balance_usdc == pytest.approx(1.0)


class TestX402Settler:
    def test_build_http_client_uses_v218_selector_contract(self):
        captured = {}

        class Client:
            def __init__(self, payment_requirements_selector=None):
                captured["selector"] = payment_requirements_selector
                self.registrations = []

            def register(self, network, scheme):
                self.registrations.append((network, scheme))

        class HttpClient:
            def __init__(self, client):
                self.client = client

        class Signer:
            def __init__(self, private_key):
                self.private_key = private_key

        class Scheme:
            scheme = "exact"

            def __init__(self, signer):
                self.signer = signer

        settler = X402Settler(private_key="test-key", requester=lambda **_kwargs: None)
        settler._sdk = (Client, HttpClient, Signer, Scheme)
        quote = Quote(
            action="premium",
            amount_usdc=0.005,
            atomic_amount=5000,
            pay_to="0xmerchant",
            asset=_BASE_USDC,
            network="eip155:8453",
            scheme="exact",
            source="tool_402",
        )

        paid_http = settler._build_http_client(quote)

        assert paid_http.client.registrations[0][0] == "eip155:*"
        selector = captured["selector"]
        selected = selector(2, [{
            "amount": "5000",
            "network": "eip155:8453",
            "payTo": "0xmerchant",
            "scheme": "exact",
            "asset": _BASE_USDC,
        }])
        assert selected["amount"] == "5000"

    async def test_installed_x402_sdk_builds_http_client(self):
        pytest.importorskip("x402")
        settler = X402Settler(private_key="0x" + ("11" * 32))
        quote = Quote(
            action="premium",
            amount_usdc=0.005,
            atomic_amount=5000,
            pay_to="0xmerchant",
            asset=_BASE_USDC,
            network="eip155:8453",
            scheme="exact",
            source="tool_402",
        )

        paid_http = settler._build_http_client(quote)
        await paid_http.aclose()

    async def test_authorizes_passthrough_then_confirms_payment_response(self):
        async def requester(**_kwargs):
            return httpx.Response(
                200,
                headers={"PAYMENT-RESPONSE": _response_header()},
                request=httpx.Request("POST", "https://tool.example/premium"),
                json={"ok": True},
            )

        settler = X402Settler(requester=requester)
        wallet = Wallet(agent_id="a", balance_usdc=1.0)
        quote = Quote(
            action="premium",
            amount_usdc=0.005,
            atomic_amount=5000,
            pay_to="0xmerchant",
            asset=_BASE_USDC,
            source="tool_402",
            payment_required=_required_header(),
        )

        pending = await settler.settle(quote=quote, wallet=wallet)
        assert pending.settled is True
        assert pending.pending is True
        assert wallet.balance_usdc == pytest.approx(0.995)

        response = await settler.request(
            quote=quote,
            method="POST",
            url="https://tool.example/premium",
            params=None,
            json_body={"q": "x"},
            headers={},
            timeout=3,
        )
        receipt = settler.confirm(
            quote=quote,
            status_code=response.status_code,
            payment_headers={
                "payment-response": response.headers["PAYMENT-RESPONSE"],
            },
        )
        assert receipt.settled is True
        assert receipt.tx_hash == "0xsettled"
        assert receipt.pending is False

    async def test_live_metered_mode_fails_closed(self):
        settler = X402Settler(requester=lambda **_kwargs: None)
        receipt = await settler.settle(
            quote=Quote(action="premium", amount_usdc=0.005, source="policy"),
            wallet=Wallet(agent_id="a", balance_usdc=1.0),
        )
        assert receipt.settled is False
        assert "passthrough" in receipt.reason

    def test_missing_or_failed_payment_response_is_not_confirmed(self):
        quote = Quote(action="premium", amount_usdc=0.005, source="tool_402")
        missing = X402Settler.confirm(
            quote=quote,
            status_code=200,
            payment_headers={},
        )
        failed = X402Settler.confirm(
            quote=quote,
            status_code=200,
            payment_headers={
                "payment-response": _response_header(success=False),
            },
        )
        assert missing.settled is False
        assert "omitted" in missing.reason
        assert failed.settled is False
        assert "rejected" in failed.reason

    def test_fresh_challenge_must_match_approved_quote(self):
        quote = Quote(
            action="premium",
            amount_usdc=0.005,
            atomic_amount=5000,
            pay_to="0xmerchant",
            asset=_BASE_USDC,
            network="eip155:8453",
            source="tool_402",
        )
        selector = X402Settler._selector_for(quote)
        chosen = selector(2, [{
            "amount": "5000",
            "network": "eip155:8453",
            "payTo": "0xmerchant",
            "scheme": "exact",
            "asset": _BASE_USDC,
        }])
        assert chosen["payTo"] == "0xmerchant"
        with pytest.raises(RuntimeError, match="changed"):
            selector(2, [{
                "amount": "5000",
                "network": "eip155:8453",
                "payTo": "0xattacker",
                "scheme": "exact",
                "asset": _BASE_USDC,
            }])

    async def test_unapproved_asset_fails_before_wallet_debit(self):
        settler = X402Settler(requester=lambda **_kwargs: None)
        wallet = Wallet(agent_id="a", balance_usdc=1.0)
        receipt = await settler.settle(
            quote=Quote(
                action="premium",
                amount_usdc=0.005,
                atomic_amount=5000,
                pay_to="0xmerchant",
                asset="0xvaluable-token",
                source="tool_402",
                payment_required=_required_header(),
            ),
            wallet=wallet,
        )
        assert receipt.settled is False
        assert "approved" in receipt.reason
        assert wallet.balance_usdc == 1.0


# ─── Pricing (unit) ──────────────────────────────────────────────────────────

class TestPricing:
    def test_default_free(self):
        g = Gate()
        g.configure({"mode": "metered", "default": 0.0})
        assert g.price_for("anything") == 0.0

    def test_override_exact(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"web_search": 0.005}})
        assert g.price_for("web_search") == 0.005

    def test_override_glob(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"github.*": 0.002}})
        assert g.price_for("github.create_pr") == 0.002
        assert g.price_for("db_query") == 0.0

    def test_first_match_wins(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"a2a.*": 0.01, "*": 0.001}})
        assert g.price_for("a2a.devops") == 0.01


# ─── Gate decisions (unit) ───────────────────────────────────────────────────

class TestGateDecisions:
    async def test_off_mode_is_free(self):
        g = Gate()
        g.configure({"mode": "off"})
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled and d.free

    async def test_metered_free_action(self):
        g = Gate()
        g.configure({"mode": "metered", "default": 0.0, "wallets": [{"agent_id": "a", "balance_usdc": 1}]})
        d = await g.charge_before(agent_id="a", action="db_query")
        assert d.settled and d.free

    async def test_metered_charges_priced_action(self):
        g = Gate()
        g.configure({
            "mode": "metered", "overrides": {"web_search": 0.005},
            "wallets": [{"agent_id": "a", "balance_usdc": 1.0}],
        })
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled and not d.free
        assert d.amount_usdc == 0.005
        assert g.get_wallet("a").balance_usdc == pytest.approx(0.995)

    async def test_metered_blocks_unfunded(self):
        g = Gate()
        g.configure({
            "mode": "metered", "overrides": {"web_search": 0.005},
            "wallets": [{"agent_id": "a", "balance_usdc": 0.001}],
        })
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled is False
        assert "insufficient balance" in d.reason

    async def test_no_wallet_is_blocked_not_free(self):
        g = Gate()
        g.configure({"mode": "metered", "overrides": {"web_search": 0.005}})
        d = await g.charge_before(agent_id="ghost", action="web_search")
        assert d.settled is False
        assert "no wallet" in d.reason

    async def test_passthrough_not_charged_before(self):
        g = Gate()
        g.configure({"mode": "passthrough", "wallets": [{"agent_id": "a", "balance_usdc": 1}]})
        d = await g.charge_before(agent_id="a", action="web_search")
        assert d.settled and d.free  # passthrough settles reactively, not here


# ─── parse_402 (unit) ────────────────────────────────────────────────────────

class TestParse402:
    def test_none_when_not_402(self):
        assert parse_402({"result": "ok"}, 200, "x") is None

    def test_quote_from_402_body(self):
        q = parse_402({"amount_usdc": 0.005, "pay_to": "0xabc"}, 402, "premium_search")
        assert q is not None
        assert q.amount_usdc == 0.005
        assert q.pay_to == "0xabc"
        assert q.source == "tool_402"

    def test_amount_alias_and_bad_value(self):
        assert parse_402({"amount": 0.01}, 402, "x").amount_usdc == 0.01
        assert parse_402({"amount_usdc": "nope"}, 402, "x").amount_usdc == 0.0

    def test_quote_from_standard_v2_header(self):
        q = parse_402(
            {"error": "payment required"},
            402,
            "premium_search",
            {"payment-required": _required_header()},
        )
        assert q is not None
        assert q.amount_usdc == pytest.approx(0.005)
        assert q.atomic_amount == 5000
        assert q.pay_to == "0xmerchant"
        assert q.network == "eip155:8453"
        assert q.payment_required


# ─── Integration: metered mode through the proxy ─────────────────────────────

@pytest.fixture
def metered_client(httpserver):
    httpserver.expect_request("/search", method="POST").respond_with_json({"results": ["a", "b"]})
    config = SidecarConfig(
        sidecar_id="pay-test",
        tools=[ToolDefinition(name="web_search", endpoint=httpserver.url_for("/search"))],
        policy=PolicyConfig(allow=["web_search"]),
        payments={
            "mode": "metered",
            "overrides": {"web_search": 0.005},
            "wallets": [
                {"agent_id": "rich", "balance_usdc": 1.0},
                {"agent_id": "broke", "balance_usdc": 0.001},
            ],
        },
    )
    return TestClient(create_app(initial_config=config))


class TestMeteredIntegration:
    def test_funded_agent_pays_and_proceeds(self, metered_client):
        resp = metered_client.post(
            "/tool/web_search", json={"query": "hi"}, headers={"X-Agent-Id": "rich"}
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {"results": ["a", "b"]}
        # balance dropped
        status = metered_client.get("/config/payments").json()
        rich = next(w for w in status["wallets"] if w["agent_id"] == "rich")
        assert rich["balance_usdc"] == pytest.approx(0.995)

    def test_unfunded_agent_blocked_402(self, metered_client):
        resp = metered_client.post(
            "/tool/web_search", json={"query": "hi"}, headers={"X-Agent-Id": "broke"}
        )
        assert resp.status_code == 402
        body = resp.json()
        assert body["blocked"] is True
        assert body["limit_type"] == "payment"
        assert "insufficient balance" in body["reason"]


# ─── Integration: passthrough mode (tool returns 402) ────────────────────────

@pytest.fixture
def passthrough_client(httpserver):
    # Tool 402s without X-PAYMENT, 200s with it.
    def handler(request):
        from werkzeug.wrappers import Response
        if not request.headers.get("X-PAYMENT"):
            return Response(
                '{"amount_usdc": 0.005, "pay_to": "0xmerchant", "nonce": "n1"}',
                status=402, content_type="application/json",
            )
        return Response('{"results": ["premium"]}', status=200, content_type="application/json")

    httpserver.expect_request("/premium", method="POST").respond_with_handler(handler)
    config = SidecarConfig(
        sidecar_id="pay-test",
        tools=[ToolDefinition(name="premium_search", endpoint=httpserver.url_for("/premium"))],
        policy=PolicyConfig(allow=["premium_search"]),
        payments={
            "mode": "passthrough",
            "wallets": [
                {"agent_id": "rich", "balance_usdc": 1.0},
                {"agent_id": "broke", "balance_usdc": 0.001},
            ],
        },
    )
    return TestClient(create_app(initial_config=config))


class TestPassthroughIntegration:
    def test_funded_agent_pays_402_and_gets_result(self, passthrough_client):
        resp = passthrough_client.post(
            "/tool/premium_search", json={"query": "x"}, headers={"X-Agent-Id": "rich"}
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == {"results": ["premium"]}
        status = passthrough_client.get("/config/payments").json()
        rich = next(w for w in status["wallets"] if w["agent_id"] == "rich")
        assert rich["balance_usdc"] == pytest.approx(0.995)

    def test_unfunded_agent_blocked_at_402(self, passthrough_client):
        resp = passthrough_client.post(
            "/tool/premium_search", json={"query": "x"}, headers={"X-Agent-Id": "broke"}
        )
        assert resp.status_code == 402
        assert resp.json()["limit_type"] == "payment"


class TestLivePassthroughIntegration:
    def test_standard_x402_payment_is_confirmed_before_output(self, httpserver):
        challenge = _required_header()
        settlement = _response_header(tx="0xlive")

        def handler(request):
            from werkzeug.wrappers import Response

            if not request.headers.get("PAYMENT-SIGNATURE"):
                return Response(
                    '{"error":"payment required"}',
                    status=402,
                    content_type="application/json",
                    headers={"PAYMENT-REQUIRED": challenge},
                )
            return Response(
                '{"results":["paid"]}',
                status=200,
                content_type="application/json",
                headers={"PAYMENT-RESPONSE": settlement},
            )

        httpserver.expect_request("/live-premium", method="POST").respond_with_handler(handler)

        async def paid_request(**kwargs):
            headers = {
                **kwargs["headers"],
                "PAYMENT-SIGNATURE": "signed-by-test-wallet",
            }
            async with httpx.AsyncClient() as client:
                return await client.request(
                    kwargs["method"],
                    kwargs["url"],
                    params=kwargs["params"],
                    json=kwargs["json_body"],
                    headers=headers,
                    timeout=kwargs["timeout"],
                )

        config = SidecarConfig(
            sidecar_id="pay-test",
            tools=[
                ToolDefinition(
                    name="live_premium",
                    endpoint=httpserver.url_for("/live-premium"),
                )
            ],
            policy=PolicyConfig(allow=["live_premium"]),
            payments={
                "mode": "passthrough",
                "wallets": [{"agent_id": "rich", "balance_usdc": 1.0}],
            },
        )
        client = TestClient(
            create_app(
                initial_config=config,
                payment_settler=X402Settler(requester=paid_request),
            )
        )

        response = client.post(
            "/tool/live_premium",
            json={"query": "x"},
            headers={"X-Agent-Id": "rich"},
        )

        assert response.status_code == 200
        assert response.json()["result"] == {"results": ["paid"]}
        status = client.get("/config/payments").json()
        assert status["settler"] == "live"
        assert status["wallets"][0]["balance_usdc"] == pytest.approx(0.995)

    def test_unconfirmed_live_payment_does_not_expose_tool_output(self, httpserver):
        challenge = _required_header()

        def handler(request):
            from werkzeug.wrappers import Response

            if not request.headers.get("PAYMENT-SIGNATURE"):
                return Response(
                    "{}",
                    status=402,
                    content_type="application/json",
                    headers={"PAYMENT-REQUIRED": challenge},
                )
            return Response(
                '{"secret":"must not escape"}',
                status=200,
                content_type="application/json",
            )

        httpserver.expect_request("/unconfirmed", method="POST").respond_with_handler(handler)

        async def paid_request(**kwargs):
            async with httpx.AsyncClient() as client:
                return await client.request(
                    kwargs["method"],
                    kwargs["url"],
                    json=kwargs["json_body"],
                    headers={**kwargs["headers"], "PAYMENT-SIGNATURE": "signed"},
                )

        config = SidecarConfig(
            sidecar_id="pay-test",
            tools=[
                ToolDefinition(
                    name="unconfirmed",
                    endpoint=httpserver.url_for("/unconfirmed"),
                )
            ],
            policy=PolicyConfig(allow=["unconfirmed"]),
            payments={
                "mode": "passthrough",
                "wallets": [{"agent_id": "rich", "balance_usdc": 1.0}],
            },
        )
        client = TestClient(
            create_app(
                initial_config=config,
                payment_settler=X402Settler(requester=paid_request),
            )
        )

        response = client.post(
            "/tool/unconfirmed",
            json={"query": "x"},
            headers={"X-Agent-Id": "rich"},
        )

        assert response.status_code == 402
        assert "secret" not in response.text
        assert "PAYMENT-RESPONSE" in response.json()["reason"]


# ─── Integration: off mode charges nothing ───────────────────────────────────

def test_off_mode_never_charges(httpserver):
    httpserver.expect_request("/search", method="POST").respond_with_json({"ok": True})
    config = SidecarConfig(
        sidecar_id="pay-test",
        tools=[ToolDefinition(name="web_search", endpoint=httpserver.url_for("/search"))],
        policy=PolicyConfig(allow=["web_search"]),
        payments={"mode": "off", "wallets": [{"agent_id": "a", "balance_usdc": 0.0}]},
    )
    client = TestClient(create_app(initial_config=config))
    resp = client.post("/tool/web_search", json={"query": "hi"}, headers={"X-Agent-Id": "a"})
    assert resp.status_code == 200  # zero balance but off mode → free
