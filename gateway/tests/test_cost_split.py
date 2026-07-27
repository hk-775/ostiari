"""Tests for accurate input/output token split (budget-accuracy fix)."""

from __future__ import annotations

from ostiari_gateway.modules.llm_gateway.providers import LLMResponse


class TestLLMResponseSplit:
    def test_real_split_preserved(self):
        r = LLMResponse(content="x", model="m", input_tokens=900, output_tokens=100)
        assert r.input_tokens == 900
        assert r.output_tokens == 100
        assert r.tokens_used == 1000

    def test_output_heavy_not_halved(self):
        # the case the 50/50 estimate got wrong: cheap input, expensive output
        r = LLMResponse(content="x", model="m", input_tokens=100, output_tokens=900)
        assert r.output_tokens == 900   # not 500

    def test_estimate_fallback_from_sum(self):
        # legacy path: only a summed tokens_used known -> half/half
        r = LLMResponse(content="x", model="m", tokens_used=1000)
        assert r.input_tokens + r.output_tokens == 1000
        assert r.tokens_used == 1000

    def test_cost_reflects_split(self):
        # a real split priced with output > input costs more than a 50/50 estimate
        from ostiari_gateway.quota_enforcer import QuotaEnforcer
        q = QuotaEnforcer()  # uses DEFAULT_PRICING (claude-sonnet-4-6: in 0.003, out 0.015)
        real = q.calculate_cost("claude-sonnet-4-6", 100, 900)      # output-heavy
        estimate = q.calculate_cost("claude-sonnet-4-6", 500, 500)  # the old 50/50
        assert real > estimate                       # 50/50 under-billed this call


class TestCostReportPayload:
    """The reported record must satisfy the control plane's UsageRecordCreate.

    The gateway calls its own identity sidecar_id, but /api/costs/record/batch
    requires gateway_id — sending the wrong key 422s the whole batch and the
    reporter swallows it at debug level, so LLM spend silently never lands.
    """

    async def _buffer_one(self):
        from ostiari_gateway.modules.llm_gateway.cost_reporter import CostReporter

        reporter = CostReporter(control_plane_url="http://cp.invalid", sidecar_id="crm-agent")
        await reporter.report(
            model="claude-sonnet-4-6", input_tokens=100, output_tokens=50,
            total_tokens=150, agent_id="planner-bot", action="llm.invoke",
        )
        return reporter._buffer[0]

    async def test_record_uses_gateway_id_not_sidecar_id(self):
        record = await self._buffer_one()
        assert record["gateway_id"] == "crm-agent"
        assert "sidecar_id" not in record

    async def test_record_validates_against_control_plane_schema(self):
        # Import the real schema so the two sides can't drift again unnoticed.
        import pathlib
        import sys

        backend = pathlib.Path(__file__).resolve().parents[2] / "control-plane" / "backend"
        if not (backend / "control_plane").is_dir():
            import pytest
            pytest.skip("control-plane backend not present")
        sys.path.insert(0, str(backend))
        try:
            from control_plane.models.schemas import UsageRecordCreate
        except ImportError:
            import pytest
            pytest.skip("control_plane not importable")
        finally:
            sys.path.remove(str(backend))

        parsed = UsageRecordCreate(**await self._buffer_one())
        assert parsed.gateway_id == "crm-agent"
        assert parsed.agent_id == "planner-bot"
        assert parsed.total_tokens == 150
