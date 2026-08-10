"""Authenticated, retry-safe delivery of gateway cost records."""

from __future__ import annotations

import httpx
import pytest

from ostiari_gateway.modules.llm_gateway.cost_reporter import CostReporter

pytestmark = pytest.mark.anyio


async def _buffer_one(reporter: CostReporter) -> None:
    await reporter.report(
        model="gpt-4o-mini",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        agent_id="billing-test",
        action="invoke",
    )


class _Client:
    def __init__(self, outcomes: list[int | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []

    async def post(self, url: str, *, json: list[dict], headers: dict[str, str]):
        self.calls.append({"url": url, "json": json, "headers": headers})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=httpx.Request("POST", url))

    async def aclose(self) -> None:
        return None


class TestCostReporterDelivery:
    async def test_sends_service_key_and_clears_confirmed_batch(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "service-secret")
        reporter = CostReporter("http://cp.local", "crm-agent")
        client = _Client([200])
        reporter._client = client  # type: ignore[assignment]
        await _buffer_one(reporter)

        await reporter.flush()

        assert client.calls[0]["headers"] == {
            "X-Ostiari-Service-Key": "service-secret"
        }
        assert client.calls[0]["url"] == "http://cp.local/api/costs/record/batch"
        assert reporter._buffer == []

    async def test_http_failure_retains_and_retries_same_batch(self):
        reporter = CostReporter("http://cp.local", "crm-agent")
        client = _Client([401, 200])
        reporter._client = client  # type: ignore[assignment]
        await _buffer_one(reporter)

        await reporter.flush()
        assert len(reporter._buffer) == 1

        await reporter.flush()

        assert reporter._buffer == []
        assert len(client.calls) == 2
        assert client.calls[0]["json"] == client.calls[1]["json"]

    async def test_transport_failure_retains_batch(self):
        reporter = CostReporter("http://cp.local", "crm-agent")
        client = _Client([httpx.ConnectError("control plane unavailable")])
        reporter._client = client  # type: ignore[assignment]
        await _buffer_one(reporter)

        await reporter.flush()

        assert len(reporter._buffer) == 1
