"""Tenant-aware local and fleet-wide rate limiting."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.gateway.models import RateLimitConfig, RateLimitResult
from src.gateway.striped_lock import StripedLock

if TYPE_CHECKING:
    from src.gateway.models import Project
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)

RATE_LIMIT_NAMESPACE = "gateway"


def _normalize_tenant_id(tenant_id: str | None) -> str | None:
    """Keep ``None`` as explicit legacy mode and reject ambiguous empty IDs."""
    if tenant_id is None:
        return None
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be None or a non-empty string")
    return tenant_id


def _tenant_scoped_id(
    tenant_id: str | None,
    resource_type: str,
    resource_id: str,
) -> str:
    """Return a collision-safe opaque key while preserving legacy raw IDs."""
    if tenant_id is None:
        return resource_id
    return (
        f"tenant:{len(tenant_id)}:{tenant_id}:"
        f"{resource_type}:{len(resource_id)}:{resource_id}"
    )


def _project_scope(
    tenant_id: str | None,
    project_id: str,
    project: Project | None,
) -> tuple[str | None, int | None]:
    """Validate an optional canonical project and derive its tenant/RPM."""
    tenant_id = _normalize_tenant_id(tenant_id)
    if project is None:
        return tenant_id, None
    if project.project_id != project_id:
        raise ValueError("project does not match project_id")

    project_tenant_id = _normalize_tenant_id(project.tenant_id)
    if tenant_id is None:
        tenant_id = project_tenant_id
    elif project_tenant_id != tenant_id:
        raise ValueError("project tenant_id does not match tenant_id")
    return tenant_id, project.rate_limit_rpm


def _fixed_window_reset(now: datetime, window_seconds: int) -> datetime:
    window_start = int(now.timestamp()) // window_seconds * window_seconds
    return datetime.fromtimestamp(window_start + window_seconds, tz=timezone.utc)


def _closed_result(
    *,
    user_limit: int | None,
    project_limit: int,
    window_seconds: int,
    now: datetime,
) -> RateLimitResult:
    """Produce stable deny metadata when shared enforcement is unavailable."""
    applicable_limits = [
        limit for limit in (user_limit, project_limit) if limit is not None
    ]
    limit = min(applicable_limits)
    reset_at = _fixed_window_reset(now, window_seconds)
    retry_after = max(1, math.ceil((reset_at - now).total_seconds()))
    return RateLimitResult(
        allowed=False,
        limit=limit,
        remaining=0,
        reset_at=reset_at,
        retry_after_seconds=retry_after,
    )


def _valid_shared_result(result: object) -> bool:
    """Reject malformed backend output instead of accidentally failing open."""
    if not isinstance(result, RateLimitResult):
        return False
    if (
        not isinstance(result.allowed, bool)
        or not isinstance(result.limit, int)
        or result.limit < 0
        or not isinstance(result.remaining, int)
        or result.remaining < 0
        or result.remaining > result.limit
        or not isinstance(result.reset_at, datetime)
        or result.reset_at.tzinfo is None
    ):
        return False
    if not result.allowed and (
        not isinstance(result.retry_after_seconds, int)
        or result.retry_after_seconds < 1
    ):
        return False
    return True


async def consume_shared_rate_limit(
    persistence: DynamoPersistence,
    *,
    namespace: str,
    tenant_id: str | None,
    user_id: str | None,
    project_id: str,
    user_limit: int | None,
    project_limit: int,
    window_seconds: int,
    now: datetime,
) -> RateLimitResult:
    """Consume one fleet-wide fixed-window slot, failing closed on outages.

    ``DynamoPersistence.consume_rate_limit_window`` must atomically increment
    every applicable counter only when all remain below their limits. It returns
    a complete ``RateLimitResult`` or ``None`` when enforcement is unavailable.
    """
    fallback = _closed_result(
        user_limit=user_limit,
        project_limit=project_limit,
        window_seconds=window_seconds,
        now=now,
    )
    consume = getattr(persistence, "consume_rate_limit_window", None)
    if not callable(consume):
        logger.error("Shared rate limiter method is unavailable")
        return fallback

    try:
        result = await consume(
            namespace=namespace,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            user_limit=user_limit,
            project_limit=project_limit,
            window_seconds=window_seconds,
            now=now,
        )
    except Exception:
        logger.exception("Shared rate limit enforcement failed")
        return fallback

    if not _valid_shared_result(result):
        logger.error("Shared rate limiter returned no usable decision")
        return fallback
    return result


class SlidingWindowRateLimiter:
    """Sliding-window local limiter with fixed-window fleet enforcement.

    Without persistence, this preserves the original in-process sliding window.
    With enabled persistence, one atomic shared fixed-window operation enforces
    the user and project limits across all gateway instances. Shared enforcement
    fails closed; silently falling back to local state would multiply limits by
    the replica count.

    ``tenant_id=None`` is the explicit legacy namespace. Supplying a tenant
    qualifies user and project state, so identical IDs in different tenants do
    not share capacity.
    """

    def __init__(
        self,
        config: RateLimitConfig,
        persistence: DynamoPersistence | None = None,
    ):
        self.config = config
        self._persistence = persistence
        # Separate timestamp stores: key -> list of datetime
        self._user_requests: dict[str, list[datetime]] = {}
        self._project_requests: dict[str, list[datetime]] = {}
        # Per-key locks instead of one global lock. A check touches both a user
        # bucket and a project bucket, so we lock BOTH keys (namespaced so a user
        # id can't collide with a project id) via multi() in canonical order —
        # different (user, project) pairs proceed concurrently, deadlock-free.
        self._locks = StripedLock()

    def _cleanup(self, timestamps: list[datetime], cutoff: datetime) -> list[datetime]:
        """Remove timestamps older than the cutoff."""
        return [ts for ts in timestamps if ts > cutoff]

    async def check_rate_limit(
        self,
        user_id: str,
        project_id: str,
        *,
        tenant_id: str | None = None,
        project: Project | None = None,
    ) -> RateLimitResult:
        """Check if request is within rate limits.

        Checks both user-level and project-level limits. A canonical project's
        ``rate_limit_rpm`` replaces the static project default when supplied.
        Returns allow/deny with limit, remaining, and reset metadata.
        """
        tenant_id, canonical_project_limit = _project_scope(
            tenant_id,
            project_id,
            project,
        )
        project_limit = (
            canonical_project_limit
            if canonical_project_limit is not None
            else self.config.project_rpm
        )
        now = datetime.now(timezone.utc)

        if self._persistence is not None and self._persistence.enabled:
            return await consume_shared_rate_limit(
                self._persistence,
                namespace=RATE_LIMIT_NAMESPACE,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=project_id,
                user_limit=self.config.user_rpm,
                project_limit=project_limit,
                window_seconds=self.config.window_seconds,
                now=now,
            )

        user_key = _tenant_scoped_id(tenant_id, "user", user_id)
        project_key = _tenant_scoped_id(tenant_id, "project", project_id)
        async with self._locks.multi(f"u:{user_key}", f"p:{project_key}"):
            window = timedelta(seconds=self.config.window_seconds)
            cutoff = now - window

            # Clean up and count user requests in the window
            user_ts = self._user_requests.get(user_key, [])
            user_ts = self._cleanup(user_ts, cutoff)
            if user_ts:
                self._user_requests[user_key] = user_ts
            else:
                self._user_requests.pop(user_key, None)
            user_count = len(user_ts)
            user_remaining = max(0, self.config.user_rpm - user_count)

            # Clean up and count project requests in the window
            project_ts = self._project_requests.get(project_key, [])
            project_ts = self._cleanup(project_ts, cutoff)
            if project_ts:
                self._project_requests[project_key] = project_ts
            else:
                self._project_requests.pop(project_key, None)
            project_count = len(project_ts)
            project_remaining = max(0, project_limit - project_count)

            # Determine if either limit is exceeded
            user_exceeded = user_count >= self.config.user_rpm
            project_exceeded = project_count >= project_limit
            allowed = not user_exceeded and not project_exceeded

            if allowed:
                # Record the request timestamp for both user and project
                self._user_requests.setdefault(user_key, []).append(now)
                self._project_requests.setdefault(project_key, []).append(now)
                # After recording, remaining decreases by 1
                user_remaining = max(0, self.config.user_rpm - (user_count + 1))
                project_remaining = max(0, project_limit - (project_count + 1))

            # Pick the more restrictive result
            remaining = min(user_remaining, project_remaining)

            # Determine which limit applies (more restrictive)
            if user_remaining <= project_remaining:
                limit = self.config.user_rpm
            else:
                limit = project_limit

            # Calculate reset_at: when the oldest request in the window expires
            oldest_user = user_ts[0] if user_ts else now
            oldest_project = project_ts[0] if project_ts else now
            # Use the earliest expiry among the two
            oldest = min(oldest_user, oldest_project)
            reset_at = oldest + window

            # retry_after_seconds only when denied
            retry_after_seconds = None
            if not allowed:
                # Find the oldest timestamp from the exceeded limit(s)
                candidates = []
                if user_exceeded and user_ts:
                    candidates.append(user_ts[0])
                if project_exceeded and project_ts:
                    candidates.append(project_ts[0])
                if candidates:
                    earliest_expiry = min(candidates) + window
                    retry_after_seconds = max(1, int((earliest_expiry - now).total_seconds() + 0.999))

            return RateLimitResult(
                allowed=allowed,
                limit=limit,
                remaining=remaining,
                reset_at=reset_at,
                retry_after_seconds=retry_after_seconds,
            )
