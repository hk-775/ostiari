"""Feedback tracker for smart routing decisions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.gateway.models import FeedbackRecord

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)


class FeedbackTracker:
    """Stores smart routing decisions for observability and feedback.

    Maintains an in-memory list of FeedbackRecord entries with configurable
    retention period and maximum record count.
    """

    def __init__(
        self,
        retention_hours: int = 24,
        max_records: int = 10000,
        persistence: DynamoPersistence | None = None,
    ) -> None:
        self._records: list[FeedbackRecord] = []
        self._retention_hours = retention_hours
        self._max_records = max_records
        self._persistence = persistence

    async def record_async(self, feedback: FeedbackRecord) -> None:
        """Store a feedback record with optional DynamoDB persistence."""
        self._records.append(feedback)
        self._prune()

        if self._persistence is not None and self._persistence.enabled:
            try:
                await self._persistence.save_feedback_record(feedback)
            except Exception:
                logger.warning(
                    "Failed to persist feedback record %s to DynamoDB",
                    feedback.request_id,
                    exc_info=True,
                )

    def record(self, feedback: FeedbackRecord) -> None:
        """Store a feedback record, pruning old entries afterward."""
        self._records.append(feedback)
        self._prune()

    def get_records(
        self,
        task_type: str | None = None,
        model_name: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        """Retrieve records filtered by task type and/or model name.

        Returns up to `limit` most recent matching records.
        """
        filtered = self._records

        if task_type is not None:
            filtered = [r for r in filtered if r.task_type == task_type]

        if model_name is not None:
            filtered = [r for r in filtered if r.selected_model == model_name]

        # Return the most recent records up to limit
        return filtered[-limit:]

    @staticmethod
    def _as_aware(ts: datetime) -> datetime:
        """Coerce a timestamp to tz-aware UTC so comparisons never mix naive/aware.

        Records may arrive with naive timestamps from older callers; the cutoff is
        always aware. Comparing the two raises TypeError, so normalize before comparing.
        """
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts

    def _prune(self) -> None:
        """Remove records older than retention period and trim to max_records."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._retention_hours)
        self._records = [r for r in self._records if self._as_aware(r.timestamp) >= cutoff]

        # Trim oldest records if over max
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records:]
