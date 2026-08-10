"""Retry-safe token-broker accounting across batch usage ingestion."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


class _Collector:
    mode = "test"

    def __init__(self, outcomes: list[Exception | None] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[dict] = []
        self.refs: dict[str, str] = {}

    async def collect(
        self,
        *,
        customer: str,
        amount_usd: float,
        model: str,
        idempotency_key: str = "",
    ) -> dict:
        self.calls.append(
            {
                "customer": customer,
                "amount_usd": amount_usd,
                "model": model,
                "idempotency_key": idempotency_key,
            }
        )
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome
        ref = self.refs.setdefault(idempotency_key, f"bill-{len(self.refs) + 1}")
        return {"collected": True, "ref": ref, "mode": self.mode}


@pytest.fixture
async def broker(client, monkeypatch):
    from control_plane.routers import broker_pilot

    collector = _Collector()
    monkeypatch.setattr(broker_pilot, "_collector", collector)
    await client.post(
        "/api/gateways",
        json={
            "id": "broker-gw",
            "name": "Broker Gateway",
            "endpoint": "http://gateway",
        },
    )
    await client.post(
        "/api/token-broker/config",
        json={"bulk_discount": 0.25, "markup": 0.12},
    )
    try:
        yield collector
    finally:
        await client.post("/api/token-broker/config/reset")


def _usage(**overrides):
    payload = {
        "gateway_id": "broker-gw",
        "event_id": "evt-1",
        "agent_id": "customer-agent",
        "model": "gpt-4o",
        "provider": "openai",
        "input_tokens": 700,
        "output_tokens": 250,
        "total_tokens": 950,
        "cost_usd": 1.0,
        "action": "chat",
    }
    payload.update(overrides)
    return payload


async def _fund(client, provider="openai", tokens=1000, threshold=100):
    response = await client.post(
        "/api/token-broker/pilot/pools/fund",
        json={
            "provider": provider,
            "tokens": tokens,
            "cost_usd": 0.75,
            "low_threshold_tokens": threshold,
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.usefixtures("app_and_db")
class TestBatchAccounting:
    async def test_batch_debits_bills_and_returns_depleted_state(
        self, client, broker
    ):
        await _fund(client)

        response = await client.post(
            "/api/costs/record/batch",
            json=[_usage()],
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["recorded"] == 1
        pool = body["broker_pools"]["broker-gw"][0]
        assert pool["provider"] == "openai"
        assert pool["consumed_tokens"] == 950
        assert pool["remaining_tokens"] == 50
        assert pool["status"] == "depleted"

        assert len(broker.calls) == 1
        assert broker.calls[0]["amount_usd"] == pytest.approx(0.84)
        assert broker.calls[0]["idempotency_key"] == "evt-1"

        records = (await client.get("/api/costs/records")).json()
        assert len(records) == 1
        assert records[0]["provider"] == "openai"
        assert records[0]["broker_cost_usd"] == pytest.approx(0.75)
        assert records[0]["broker_charge_usd"] == pytest.approx(0.84)
        assert records[0]["billing_status"] == "collected"
        assert records[0]["billing_ref"] == "bill-1"

    async def test_identical_retry_does_not_duplicate_any_ledger(
        self, client, broker
    ):
        await _fund(client, tokens=10_000, threshold=0)
        first = await client.post("/api/costs/record/batch", json=[_usage()])
        second = await client.post("/api/costs/record/batch", json=[_usage()])

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["recorded"] == 0
        assert second.json()["duplicates"] == 1
        assert len(broker.calls) == 1

        records = (await client.get("/api/costs/records")).json()
        pools = (await client.get("/api/token-broker/pilot/pools")).json()
        assert len(records) == 1
        assert pools[0]["consumed_tokens"] == 950
        assert pools[0]["consumed_cost_usd"] == pytest.approx(0.75)

    async def test_reused_event_id_with_different_data_is_rejected(
        self, client, broker
    ):
        await _fund(client, tokens=10_000, threshold=0)
        assert (
            await client.post("/api/costs/record/batch", json=[_usage()])
        ).status_code == 200

        conflict = await client.post(
            "/api/costs/record/batch",
            json=[_usage(total_tokens=951)],
        )

        assert conflict.status_code == 409
        pools = (await client.get("/api/token-broker/pilot/pools")).json()
        assert pools[0]["consumed_tokens"] == 950
        assert len(broker.calls) == 1

    async def test_failed_billing_retries_without_second_pool_debit(
        self, client, broker, monkeypatch
    ):
        from control_plane.routers import broker_pilot

        collector = _Collector([RuntimeError("billing unavailable"), None])
        monkeypatch.setattr(broker_pilot, "_collector", collector)
        await _fund(client, tokens=10_000, threshold=0)

        first = await client.post("/api/costs/record/batch", json=[_usage()])
        assert first.status_code == 503
        assert first.json()["broker_pools"]["broker-gw"][0]["consumed_tokens"] == 950

        retry = await client.post("/api/costs/record/batch", json=[_usage()])
        assert retry.status_code == 200
        assert retry.json()["recorded"] == 0
        assert retry.json()["duplicates"] == 1

        assert [call["idempotency_key"] for call in collector.calls] == [
            "evt-1",
            "evt-1",
        ]
        pools = (await client.get("/api/token-broker/pilot/pools")).json()
        assert pools[0]["consumed_tokens"] == 950
        record = (await client.get("/api/costs/records")).json()[0]
        assert record["billing_status"] == "collected"

    async def test_actual_provider_selects_pool_not_model_family(
        self, client, broker
    ):
        await _fund(client, provider="anthropic", tokens=10_000, threshold=0)
        await _fund(client, provider="bedrock", tokens=10_000, threshold=0)

        response = await client.post(
            "/api/costs/record/batch",
            json=[
                _usage(
                    event_id="evt-bedrock",
                    model="claude-sonnet-4-6",
                    provider="bedrock-mantle",
                )
            ],
        )

        assert response.status_code == 200
        pools = {
            pool["provider"]: pool
            for pool in (await client.get("/api/token-broker/pilot/pools")).json()
        }
        assert pools["anthropic"]["consumed_tokens"] == 0
        assert pools["bedrock"]["consumed_tokens"] == 950
        record = (await client.get("/api/costs/records")).json()[0]
        assert record["provider"] == "bedrock"

    async def test_heartbeat_always_carries_current_pool_state(
        self, client, broker
    ):
        await _fund(client, tokens=10_000, threshold=100)

        heartbeat = await client.post("/api/gateways/broker-gw/heartbeat")

        assert heartbeat.status_code == 200
        updates = heartbeat.json()["config_updates"]
        assert updates[-1]["broker_pools"][0]["provider"] == "openai"
        assert updates[-1]["broker_pools"][0]["status"] == "active"
