"""PolicyPoller — background daemon thread for remote policy refresh."""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import TYPE_CHECKING

from ostiari.policy.fetcher import PolicySource, get_fetcher

if TYPE_CHECKING:
    from ostiari.policy.engine import PolicyEngine

logger = logging.getLogger("ostiari")


class PolicyPoller:
    """Polls a remote policy source and hot-reloads on change."""

    def __init__(self, source: PolicySource, engine: PolicyEngine) -> None:
        self._source = source
        self._engine = engine
        self._current_hash: str | None = None
        self._backoff = source.poll_interval
        self._max_backoff = 300
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fetcher = get_fetcher(source.url)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ostiari-policy-poller"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._backoff):
            self.poll_once()

    def poll_once(self) -> None:
        """Execute a single poll cycle. Exposed for testing."""
        try:
            content = self._fetcher.fetch(self._source)
            new_hash = hashlib.sha256(content).hexdigest()[:8]
            if new_hash != self._current_hash:
                self._engine.reload_from_content(content, source=self._source.url)
                self._current_hash = new_hash
                logger.info("Policy reloaded from %s (hash=%s)", self._source.url, new_hash)
            self._backoff = self._source.poll_interval
        except Exception as e:
            logger.warning("Policy fetch failed (%s): %s", self._source.url, e)
            self._backoff = min(self._backoff * 2, self._max_backoff)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_hash(self) -> str | None:
        return self._current_hash
