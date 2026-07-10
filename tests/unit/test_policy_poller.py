"""Unit tests for the PolicyPoller module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ostiari.policy.fetcher import PolicySource
from ostiari.policy.poller import PolicyPoller


class TestPolicyPoller:
    def test_start_stop_lifecycle(self):
        mock_engine = MagicMock()
        source = PolicySource(url="file:///tmp/policy.yaml", poll_interval=1)

        with patch("ostiari.policy.poller.get_fetcher") as mock_get:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch.return_value = b"rules: []"
            mock_get.return_value = mock_fetcher

            poller = PolicyPoller(source=source, engine=mock_engine)
            poller.start()
            assert poller.is_running
            poller.stop()
            assert not poller.is_running

    def test_hash_change_triggers_reload(self):
        mock_engine = MagicMock()
        source = PolicySource(url="file:///tmp/policy.yaml", poll_interval=60)

        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = b"rules:\n  - type: block"

        with patch("ostiari.policy.poller.get_fetcher", return_value=mock_fetcher):
            poller = PolicyPoller(source=source, engine=mock_engine)
            poller.poll_once()
            mock_engine.reload_from_content.assert_called_once()
            assert poller.current_hash is not None

    def test_no_change_skips_reload(self):
        mock_engine = MagicMock()
        source = PolicySource(url="file:///tmp/policy.yaml", poll_interval=60)

        content = b"rules: []"
        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = content

        with patch("ostiari.policy.poller.get_fetcher", return_value=mock_fetcher):
            poller = PolicyPoller(source=source, engine=mock_engine)

            import hashlib

            poller._current_hash = hashlib.sha256(content).hexdigest()[:8]
            poller.poll_once()
            mock_engine.reload_from_content.assert_not_called()

    def test_fetch_failure_increases_backoff(self):
        mock_engine = MagicMock()
        source = PolicySource(url="file:///tmp/policy.yaml", poll_interval=60)

        mock_fetcher = MagicMock()
        mock_fetcher.fetch.side_effect = OSError("connection refused")

        with patch("ostiari.policy.poller.get_fetcher", return_value=mock_fetcher):
            poller = PolicyPoller(source=source, engine=mock_engine)
            initial_backoff = poller._backoff

            poller.poll_once()
            assert poller._backoff > initial_backoff

    def test_success_resets_backoff(self):
        mock_engine = MagicMock()
        source = PolicySource(url="file:///tmp/policy.yaml", poll_interval=60)

        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = b"rules: []"

        with patch("ostiari.policy.poller.get_fetcher", return_value=mock_fetcher):
            poller = PolicyPoller(source=source, engine=mock_engine)
            poller._backoff = 240

            poller.poll_once()
            assert poller._backoff == source.poll_interval
