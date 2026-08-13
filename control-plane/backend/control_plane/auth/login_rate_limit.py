"""Durable local-login throttling backed by the control-plane database."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.models import LoginAttemptWindow
from control_plane.env import env_flag, is_production

_WINDOW_SECONDS = 60


def _configured_limit(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1 or value > 10_000:
        raise RuntimeError(f"{name} must be between 1 and 10000")
    return value


def _digest(kind: str, value: str) -> str:
    normalized = value.strip().lower()
    return hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()


def _client_source(request: Request) -> str:
    # Trust the direct peer only. Deployments that need the original address
    # must terminate traffic at a trusted proxy that preserves source identity.
    return request.client.host if request.client else "unknown"


async def _consume(
    db: AsyncSession,
    key_digest: str,
    limit: int,
) -> tuple[bool, int]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_WINDOW_SECONDS)
    dialect = db.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    statement = insert_fn(LoginAttemptWindow).values(
        key_digest=key_digest,
        attempts=1,
        window_started_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[LoginAttemptWindow.key_digest],
        set_={
            "attempts": case(
                (LoginAttemptWindow.window_started_at <= cutoff, 1),
                else_=LoginAttemptWindow.attempts + 1,
            ),
            "window_started_at": case(
                (LoginAttemptWindow.window_started_at <= cutoff, now),
                else_=LoginAttemptWindow.window_started_at,
            ),
        },
    ).returning(
        LoginAttemptWindow.attempts,
        LoginAttemptWindow.window_started_at,
    )
    attempts, window_started_at = (await db.execute(statement)).one()
    if window_started_at.tzinfo is None:
        window_started_at = window_started_at.replace(tzinfo=timezone.utc)
    retry_after = max(
        1,
        int(
            (
                window_started_at
                + timedelta(seconds=_WINDOW_SECONDS)
                - datetime.now(timezone.utc)
            ).total_seconds()
        ),
    )
    return int(attempts) <= limit, retry_after


async def enforce_login_rate_limit(
    request: Request,
    email: str,
    db: AsyncSession,
) -> str | None:
    """Consume source/account buckets and return the account key when enabled."""
    if not (is_production() or env_flag("OSTIARI_LOGIN_RATE_LIMIT")):
        return None

    buckets = (
        (
            _digest("login-source", _client_source(request)),
            _configured_limit("OSTIARI_LOGIN_SOURCE_ATTEMPTS_PER_MINUTE", 120),
        ),
        (
            _digest("login-account", email),
            _configured_limit("OSTIARI_LOGIN_ATTEMPTS_PER_MINUTE", 10),
        ),
    )
    account_key = buckets[1][0]
    for key_digest, limit in buckets:
        allowed, retry_after = await _consume(db, key_digest, limit)
        if not allowed:
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts",
                headers={"Retry-After": str(retry_after)},
            )

    # Failed authentication raises after this dependency returns. Commit the
    # attempt now so the request rollback cannot erase the throttle record.
    await db.commit()
    return account_key


async def clear_login_account_window(
    db: AsyncSession,
    account_key: str | None,
) -> None:
    """Clear account failures after successful authentication."""
    if account_key is None:
        return
    await db.execute(
        delete(LoginAttemptWindow).where(
            LoginAttemptWindow.key_digest == account_key
        )
    )
