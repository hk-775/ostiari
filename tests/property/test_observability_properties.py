"""Property-based tests for the observability layer."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from ostiari.models import TraceEntry, TraceFilters
from ostiari.report import ReportGenerator

safe_text = st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz.")
tier_st = st.sampled_from(["allow", "block", "intervene"])
risk_st = st.integers(min_value=0, max_value=100)


def _trace_entry(action: str, tier: str, risk_score: int) -> TraceEntry:
    return TraceEntry(
        trace_id=f"t-{action}-{tier}-{risk_score}",
        correlation_id="agent-prop",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params={},
        result=None,
        risk_score=risk_score,
        tier=tier,
        duration_ms=1.0,
        signals=[],
        anomalies=[],
        breaker_state=None,
        metadata={},
    )


class FakeStorage:
    """Minimal storage that returns configured traces."""

    def __init__(self, traces: list[TraceEntry]) -> None:
        self._traces = traces
        self._call_count = 0

    def get_traces(self, filters: TraceFilters) -> list[TraceEntry]:
        if self._call_count > 0:
            return []
        self._call_count += 1
        return self._traces


class TestStatsAggregationProperties:
    @given(actions=st.lists(st.tuples(safe_text, tier_st, risk_st), min_size=1, max_size=50))
    @settings(max_examples=50)
    def test_totals_match_trace_count(self, actions):
        traces = [_trace_entry(a, t, r) for a, t, r in actions]
        storage = FakeStorage(traces)
        gen = ReportGenerator(storage)
        import json

        data = gen.generate(period_days=1, format="json")
        report = json.loads(data)

        stats = report["stats"]
        assert stats["total_actions"] == len(actions)
        assert stats["allowed"] + stats["blocked"] + stats["intervened"] == len(actions)

    @given(actions=st.lists(st.tuples(safe_text, tier_st, risk_st), min_size=1, max_size=50))
    @settings(max_examples=50)
    def test_avg_risk_within_bounds(self, actions):
        traces = [_trace_entry(a, t, r) for a, t, r in actions]
        storage = FakeStorage(traces)
        gen = ReportGenerator(storage)
        import json

        data = gen.generate(period_days=1, format="json")
        report = json.loads(data)

        avg = report["stats"]["avg_risk_score"]
        assert 0 <= avg <= 100


class TestCSVProperties:
    @given(actions=st.lists(st.tuples(safe_text, tier_st, risk_st), min_size=1, max_size=30))
    @settings(max_examples=30)
    def test_csv_row_count_matches_traces(self, actions):
        traces = [_trace_entry(a, t, r) for a, t, r in actions]
        storage = FakeStorage(traces)
        gen = ReportGenerator(storage)

        data = gen.generate(period_days=1, format="csv")
        text = data.decode("utf-8")
        lines = [l for l in text.strip().split("\n") if l]

        assert lines[0].startswith("trace_id")
        assert len(lines) == len(actions) + 1  # header + data rows

    @given(actions=st.lists(st.tuples(safe_text, tier_st, risk_st), min_size=1, max_size=20))
    @settings(max_examples=30)
    def test_streaming_csv_matches_batch(self, actions):
        traces = [_trace_entry(a, t, r) for a, t, r in actions]

        storage_batch = FakeStorage(traces)
        storage_stream = FakeStorage(traces)

        gen_batch = ReportGenerator(storage_batch)
        gen_stream = ReportGenerator(storage_stream)

        batch_data = gen_batch.generate(period_days=1, format="csv").decode("utf-8")
        stream_rows = list(gen_stream.generate_csv_rows(period_days=1))

        batch_lines = [l for l in batch_data.strip().split("\n") if l]
        stream_lines = [l.strip() for l in stream_rows if l.strip()]

        assert len(batch_lines) == len(stream_lines)


class TestCacheProperties:
    @given(ttl=st.integers(min_value=1, max_value=300))
    @settings(max_examples=20)
    def test_cache_returns_same_value_within_ttl(self, ttl):
        from ostiari.dashboard.cache import QueryCache

        cache = QueryCache(default_ttl=ttl)
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            return {"counter": call_count}

        async def run():
            r1 = await cache.get_or_compute("key", compute, ttl=ttl)
            r2 = await cache.get_or_compute("key", compute, ttl=ttl)
            return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1 == r2
        assert call_count == 1


class TestTimeseriesProperties:
    @given(actions=st.lists(st.tuples(tier_st, risk_st), min_size=1, max_size=30))
    @settings(max_examples=30)
    def test_timeseries_buckets_cover_period(self, actions):
        from unittest.mock import AsyncMock, MagicMock

        from ostiari.dashboard.services.stats import StatsService

        traces = []
        now = datetime.now(timezone.utc)
        for i, (tier, risk) in enumerate(actions):
            t = TraceEntry(
                trace_id=f"t-{i}",
                correlation_id="a",
                timestamp=now - timedelta(minutes=i),
                action="op",
                params={},
                result=None,
                risk_score=risk,
                tier=tier,
                duration_ms=1.0,
                signals=[],
                anomalies=[],
                breaker_state=None,
                metadata={},
            )
            traces.append(t)

        mock_storage = MagicMock()
        mock_storage.get_traces = AsyncMock(return_value=traces)

        service = StatsService(mock_storage)

        async def run():
            return await service.timeseries(period="1h", bucket="10m")

        result = asyncio.run(run())
        assert len(result) >= 1
        total_in_buckets = sum(b["total"] for b in result)
        assert total_in_buckets == len(actions)
