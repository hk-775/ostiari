"""Stats service — aggregate statistics and timeseries computation."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from ostiari.dashboard.storage_async import AsyncStorageWrapper
from ostiari.models import TraceFilters


def _parse_period(period: str) -> timedelta:
    match = re.match(r"^(\d+)([smhd])$", period.strip())
    if not match:
        return timedelta(hours=24)
    amount, unit = int(match.group(1)), match.group(2)
    mapping = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    return timedelta(**{mapping[unit]: amount})


class StatsService:
    """Computes aggregate statistics and timeseries from traces."""

    def __init__(self, storage: AsyncStorageWrapper) -> None:
        self._storage = storage

    async def aggregate(self, period: str) -> dict[str, Any]:
        delta = _parse_period(period)
        end = datetime.now(timezone.utc)
        start = end - delta

        traces = await self._storage.get_traces(
            TraceFilters(start_time=start, end_time=end, limit=1000)
        )

        total = len(traces)
        return {
            "total_actions": total,
            "allowed": sum(1 for t in traces if t.tier == "allow"),
            "blocked": sum(1 for t in traces if t.tier == "block"),
            "intervened": sum(1 for t in traces if t.tier == "intervene"),
            "avg_risk": round(sum(t.risk_score for t in traces) / max(total, 1), 2),
            "unique_agents": len({t.correlation_id for t in traces if t.correlation_id}),
        }

    async def timeseries(self, period: str, bucket: str) -> list[dict[str, Any]]:
        period_delta = _parse_period(period)
        bucket_delta = _parse_period(bucket)
        end = datetime.now(timezone.utc)
        start = end - period_delta

        traces = await self._storage.get_traces(
            TraceFilters(start_time=start, end_time=end, limit=1000)
        )

        buckets: dict[datetime, list] = defaultdict(list)
        current = start
        while current < end:
            buckets[current] = []
            current += bucket_delta

        for t in traces:
            bucket_start = start + ((t.timestamp - start) // bucket_delta) * bucket_delta
            buckets.setdefault(bucket_start, []).append(t)

        result = []
        for ts in sorted(buckets.keys()):
            bucket_traces = buckets[ts]
            total = len(bucket_traces)
            result.append(
                {
                    "timestamp": ts.isoformat(),
                    "total": total,
                    "allowed": sum(1 for t in bucket_traces if t.tier == "allow"),
                    "blocked": sum(1 for t in bucket_traces if t.tier == "block"),
                    "avg_risk": round(sum(t.risk_score for t in bucket_traces) / max(total, 1), 2),
                }
            )

        return result
