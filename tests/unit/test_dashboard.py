"""Unit tests for the Dashboard API."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from ostiari.models import TraceEntry, TraceFilters


def _make_trace(action="test", tier="allow", risk_score=20):
    return TraceEntry(
        trace_id="t-1",
        correlation_id="agent-1",
        timestamp=datetime.now(timezone.utc),
        action=action,
        params={},
        result=None,
        risk_score=risk_score,
        tier=tier,
        duration_ms=5.0,
        signals=[],
        anomalies=[],
        breaker_state=None,
        metadata={},
    )


class TestAsyncStorageWrapper:
    @pytest.mark.asyncio
    async def test_get_traces_delegates(self):
        from ostiari.dashboard.storage_async import AsyncStorageWrapper

        mock_storage = MagicMock()
        mock_storage.get_traces.return_value = [_make_trace()]

        wrapper = AsyncStorageWrapper(mock_storage)
        result = await wrapper.get_traces(TraceFilters(limit=10))

        assert len(result) == 1
        mock_storage.get_traces.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_trace_delegates(self):
        from ostiari.dashboard.storage_async import AsyncStorageWrapper

        mock_storage = MagicMock()
        mock_storage.get_trace.return_value = _make_trace()

        wrapper = AsyncStorageWrapper(mock_storage)
        result = await wrapper.get_trace("t-1")
        assert result is not None

    @pytest.mark.asyncio
    async def test_schema_version(self):
        from ostiari.dashboard.storage_async import AsyncStorageWrapper

        mock_storage = MagicMock()
        mock_storage.schema_version.return_value = 3

        wrapper = AsyncStorageWrapper(mock_storage)
        assert await wrapper.schema_version() == 3


class TestTokenAuthMiddleware:
    @pytest.mark.asyncio
    async def test_exempt_health_route(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ostiari.dashboard.middleware import TokenAuthMiddleware

        async def health(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/api/health", health)])
        app.add_middleware(TokenAuthMiddleware, token="secret123")

        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_rejects_missing_token(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ostiari.dashboard.middleware import TokenAuthMiddleware

        async def api(request):
            return PlainTextResponse("data")

        app = Starlette(routes=[Route("/api/traces", api)])
        app.add_middleware(TokenAuthMiddleware, token="secret123")

        client = TestClient(app)
        resp = client.get("/api/traces")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_accepts_valid_token(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from ostiari.dashboard.middleware import TokenAuthMiddleware

        async def api(request):
            return PlainTextResponse("data")

        app = Starlette(routes=[Route("/api/traces", api)])
        app.add_middleware(TokenAuthMiddleware, token="secret123")

        client = TestClient(app)
        resp = client.get("/api/traces", headers={"Authorization": "Bearer secret123"})
        assert resp.status_code == 200


class TestQueryCache:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        from ostiari.dashboard.cache import QueryCache

        cache = QueryCache()
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            return {"value": 42}

        result1 = await cache.get_or_compute("key1", compute, ttl=60)
        result2 = await cache.get_or_compute("key1", compute, ttl=60)

        assert result1 == result2
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_cache_miss_different_keys(self):
        from ostiari.dashboard.cache import QueryCache

        cache = QueryCache()
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            return {"value": call_count}

        await cache.get_or_compute("key1", compute)
        await cache.get_or_compute("key2", compute)

        assert call_count == 2


class TestWebSocketManager:
    @pytest.mark.asyncio
    async def test_broadcast_without_redis(self):
        from ostiari.dashboard.websocket import WebSocketManager

        manager = WebSocketManager()
        # No connections, should not raise
        await manager.publish_traces([{"trace_id": "t1"}])

    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        from ostiari.dashboard.websocket import WebSocketManager

        manager = WebSocketManager()
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        await manager.connect(mock_ws)
        assert mock_ws in manager._connections

        manager.disconnect(mock_ws)
        assert mock_ws not in manager._connections
