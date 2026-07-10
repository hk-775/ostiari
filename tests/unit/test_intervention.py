"""Unit tests for the InterventionBroker."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ostiari.dashboard.intervention import InterventionBroker


class TestInterventionBroker:
    @pytest.mark.asyncio
    async def test_respond_acquires_lock(self):
        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        mock_redis.publish = AsyncMock()

        broker = InterventionBroker(mock_redis)
        result = await broker.respond("req-1", approved=True)

        assert result is True
        mock_redis.set.assert_called_once()
        mock_redis.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_respond_already_handled(self):
        mock_redis = AsyncMock()
        mock_redis.set.return_value = False  # Lock not acquired

        broker = InterventionBroker(mock_redis)
        result = await broker.respond("req-1", approved=True)

        assert result is False
        mock_redis.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_respond_publishes_correct_data(self):
        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        mock_redis.publish = AsyncMock()

        broker = InterventionBroker(mock_redis)
        await broker.respond("req-42", approved=False)

        call_args = mock_redis.publish.call_args
        channel = call_args[0][0]
        data = json.loads(call_args[0][1])

        assert channel == "ostiari:intervention:response"
        assert data["request_id"] == "req-42"
        assert data["approved"] is False

    @pytest.mark.asyncio
    async def test_request_publishes_to_channel(self):
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        mock_pubsub = MagicMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()

        async def listen_gen():
            yield {
                "type": "message",
                "data": json.dumps({"request_id": "will-match", "approved": True}),
            }

        mock_pubsub.listen = MagicMock(return_value=listen_gen())
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        broker = InterventionBroker(mock_redis)

        mock_uuid = MagicMock()
        mock_uuid.__str__ = lambda s: "will-match"
        with patch("ostiari.dashboard.intervention.uuid.uuid4", return_value=mock_uuid):
            result = await broker.request_intervention("test", 80.0, "Allow?", timeout=5.0)

        mock_redis.publish.assert_called_once()
        assert result is True
