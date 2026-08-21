"""API Key management — issue, validate, revoke, rotate."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.gateway.auth.principal import API_KEY_ISSUER
from src.gateway.models import (
    APIKey,
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

PREFIX = "axon_"


CACHE_TTL_SECONDS = 300

# How often an instance checks whether another instance revoked a key. This is
# the actual upper bound on how long a revoked key keeps working elsewhere;
# CACHE_TTL_SECONDS is only the bound when the epoch check is unavailable (no
# persistence, or the read failed). Five seconds of one small point read per
# instance, against 300 seconds of a revoked credential still being accepted.
REVOCATION_POLL_SECONDS = 5
DEFAULT_TENANT_KEY_TTL = timedelta(days=90)
MAX_TENANT_KEY_TTL = timedelta(days=365)


class APIKeyService:
    """Manages the lifecycle of project-scoped API keys.

    Keys are stored as SHA-256 hashes — the plaintext is returned only once
    at issue time and never persisted.
    """

    def __init__(self, persistence: DynamoPersistence) -> None:
        self._persistence = persistence
        self._cache: dict[str, tuple[APIKey, float]] = {}
        # Fallback store used only when persistence is disabled (local dev and
        # demo mode). Without it every DynamoPersistence write is a no-op and
        # every read returns empty, so a key could be issued but never listed,
        # revoked, or rotated. When persistence is enabled this stays empty and
        # DynamoDB remains the single source of truth.
        self._memory_store: dict[str, APIKey] = {}
        # Last revocation epoch this instance saw, and when it last looked. None
        # means "never successfully read", which is why the first check adopts
        # whatever it finds rather than treating a non-zero epoch as a change and
        # clearing a cache it only just built.
        self._revocation_epoch: int | None = None
        self._revocation_checked_at: float = 0.0
        self._tenant_revocation_epochs: dict[str, int] = {}
        self._tenant_revocation_checked_at: dict[str, float] = {}
        # Keys cached immediately after issuance can predate this replica's
        # first revocation-epoch read. Only those entries need a strong reread
        # after the baseline is established.
        self._requires_epoch_baseline: set[str] = set()

    @property
    def persistence(self) -> DynamoPersistence:
        """The authoritative store shared with lifecycle audit recording."""
        return self._persistence

    @property
    def _in_memory(self) -> bool:
        return not self._persistence.enabled

    @staticmethod
    def generate_raw_key() -> str:
        return PREFIX + secrets.token_hex(32)

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def _resolve_expiry(
        expires_at: datetime | None,
        tenant_id: str | None,
        now: datetime,
    ) -> datetime | None:
        if tenant_id is None:
            return expires_at
        resolved = expires_at or now + DEFAULT_TENANT_KEY_TTL
        if resolved.tzinfo is None or resolved.utcoffset() is None:
            raise ValueError("tenant API-key expiry must include a timezone")
        resolved = resolved.astimezone(timezone.utc)
        if resolved <= now:
            raise ValueError("tenant API-key expiry must be in the future")
        if resolved - now > MAX_TENANT_KEY_TTL:
            raise ValueError("tenant API-key expiry cannot exceed 365 days")
        return resolved

    @staticmethod
    def _principal_for_key(key: APIKey) -> Principal:
        if key.tenant_id is None:
            raise ValueError("canonical API-key principals require tenant_id")
        return Principal(
            principal_id=f"apikey:{key.key_id}",
            tenant_id=key.tenant_id,
            subject=key.key_id,
            issuer=API_KEY_ISSUER,
            roles=frozenset({TenantRole.SERVICE}),
            auth_method=AuthMethod.API_KEY,
            membership_status=MembershipStatus.ACTIVE,
            project_ids=frozenset({key.project_id}),
            scopes=frozenset(key.scopes),
            authorization_version=1,
            credential_id=key.key_id,
        )

    def _cache_issued_key(self, key: APIKey) -> None:
        self._cache[key.key_hash] = (key, time.time())
        if self._in_memory:
            return
        baseline = (
            self._revocation_epoch
            if key.tenant_id is None
            else self._tenant_revocation_epochs.get(key.tenant_id)
        )
        if baseline is None:
            self._requires_epoch_baseline.add(key.key_hash)

    async def issue_key(
        self,
        project_id: str,
        name: str,
        scopes: list[str],
        created_by: str,
        expires_at: datetime | None = None,
        tenant_id: str | None = None,
    ) -> tuple[APIKey, str]:
        """Issue a new API key. Returns (key_record, raw_key_one_time)."""
        now = datetime.now(timezone.utc)
        raw_key = self.generate_raw_key()
        key_hash = self.hash_key(raw_key)
        key_id = f"axk_{secrets.token_hex(12)}"

        key = APIKey(
            key_id=key_id,
            key_hash=key_hash,
            project_id=project_id,
            name=name,
            scopes=scopes,
            created_by=created_by,
            tenant_id=tenant_id,
            created_at=now,
            expires_at=self._resolve_expiry(expires_at, tenant_id, now),
        )

        # Real persistence treats this as one conditional transaction. Do not
        # expose or cache the plaintext credential until all durable rows exist.
        save_with_principal = getattr(
            self._persistence,
            "save_api_key_with_principal",
            None,
        )
        if tenant_id is not None and callable(save_with_principal):
            await save_with_principal(key, self._principal_for_key(key))
        else:
            await self._persistence.save_api_key(key)
        if self._in_memory:
            self._memory_store[key_id] = key
        self._cache_issued_key(key)
        return key, raw_key

    async def _check_revocations(self, tenant_id: str | None) -> bool:
        """Drop the local cache if another instance has revoked a key.

        ``revoke_key`` clears the cache on the instance that served the request,
        which is the only instance that needs no help. Every other instance kept
        serving the revoked key until its own entry aged out — up to
        ``CACHE_TTL_SECONDS`` of a credential an operator had deliberately
        revoked. With the shipped ``desired_count=2`` that was the common case,
        not an edge one.

        Polled here rather than pushed because there is no bus between instances,
        and checked at most every ``REVOCATION_POLL_SECONDS`` so the cost is one
        small point read per interval instead of one per request — the cache
        exists to keep DynamoDB off the hot path, and reading the epoch every
        request would give that back.

        A failed read leaves the epoch untouched, so behaviour degrades to the
        TTL rather than to clearing the cache on every request.

        Returns True when a key loaded before this check must be re-read: either
        this call established the first epoch baseline or it observed an epoch
        change. In both cases, a second strong key read orders the cached value
        after the epoch observation.
        """
        if self._in_memory:
            return False
        now = time.time()
        if tenant_id is None:
            checked_at = self._revocation_checked_at
        else:
            checked_at = self._tenant_revocation_checked_at.get(tenant_id, 0.0)
        if now - checked_at < REVOCATION_POLL_SECONDS:
            return False

        if tenant_id is None:
            self._revocation_checked_at = now
            epoch = await self._persistence.get_revocation_epoch()
            previous = self._revocation_epoch
        else:
            self._tenant_revocation_checked_at[tenant_id] = now
            epoch = await self._persistence.get_revocation_epoch(tenant_id)
            previous = self._tenant_revocation_epochs.get(tenant_id)
        if epoch is None:
            return False
        if previous is None:
            if tenant_id is None:
                self._revocation_epoch = epoch
            else:
                self._tenant_revocation_epochs[tenant_id] = epoch
            return True
        if epoch != previous:
            if tenant_id is None:
                self._revocation_epoch = epoch
            else:
                self._tenant_revocation_epochs[tenant_id] = epoch
            self._cache = {
                key_hash: entry
                for key_hash, entry in self._cache.items()
                if entry[0].tenant_id != tenant_id
            }
            self._requires_epoch_baseline.intersection_update(self._cache)
            return True
        return False

    async def _load_key_by_hash(self, key_hash: str) -> APIKey | None:
        if self._in_memory:
            return next(
                (k for k in self._memory_store.values() if k.key_hash == key_hash),
                None,
            )
        return await self._persistence.get_api_key_by_hash(key_hash)

    @staticmethod
    def _is_usable(key: APIKey) -> bool:
        if key.revoked:
            return False
        return not (
            key.expires_at and key.expires_at < datetime.now(timezone.utc)
        )

    def _evict_cached_key(self, key: APIKey) -> None:
        """Remove local state that could still authenticate this key."""
        self._cache.pop(key.key_hash, None)
        self._requires_epoch_baseline.discard(key.key_hash)

    async def _load_cacheable_key(self, key_hash: str) -> APIKey | None:
        key = await self._load_key_by_hash(key_hash)
        if key is None or not self._is_usable(key):
            return None

        epoch_requires_reread = await self._check_revocations(key.tenant_id)
        if epoch_requires_reread and not self._in_memory:
            # The first read may have raced a revocation that committed before
            # either a newly established baseline or an observed epoch change.
            # A strongly consistent second read orders the cached value after
            # that epoch observation.
            key = await self._load_key_by_hash(key_hash)
            if key is None or not self._is_usable(key):
                return None
        return key

    async def _check_cached_revocations(
        self,
        key_hash: str,
    ) -> tuple[APIKey, float] | None:
        entry = self._cache.get(key_hash)
        if entry is None:
            return None
        await self._check_revocations(entry[0].tenant_id)
        tenant_id = entry[0].tenant_id
        baseline_known = (
            self._revocation_epoch is not None
            if tenant_id is None
            else tenant_id in self._tenant_revocation_epochs
        )
        if (
            key_hash in self._requires_epoch_baseline
            and baseline_known
            and not self._in_memory
        ):
            # A key issued by this replica is already cached before its tenant
            # epoch has a baseline. Another replica may revoke it before the
            # first validation. Order a strong read after that baseline rather
            # than accepting the stale issued object until its TTL expires.
            key = await self._load_key_by_hash(key_hash)
            self._requires_epoch_baseline.discard(key_hash)
            if key is None or not self._is_usable(key):
                self._cache.pop(key_hash, None)
                return None
            self._cache[key_hash] = (key, entry[1])
        return self._cache.get(key_hash)

    async def validate_key(self, raw_key: str) -> APIKey | None:
        """Validate a raw API key. Returns the key record or None."""
        if not raw_key.startswith(PREFIX):
            return None

        key_hash = self.hash_key(raw_key)

        entry = await self._check_cached_revocations(key_hash)
        if entry is not None:
            cached, cached_at = entry
            if (
                (time.time() - cached_at) < CACHE_TTL_SECONDS
                and self._is_usable(cached)
            ):
                cached.last_used_at = datetime.now(timezone.utc)
                return cached
            else:
                del self._cache[key_hash]
                self._requires_epoch_baseline.discard(key_hash)

        key = await self._load_cacheable_key(key_hash)
        if key is None:
            return None

        key.last_used_at = datetime.now(timezone.utc)
        self._cache[key_hash] = (key, time.time())
        return key

    async def revoke_key(
        self,
        key_id: str,
        tenant_id: str | None = None,
        *,
        revoked_by: str | None = None,
    ) -> bool:
        """Revoke a key by ID. Returns True if found and revoked."""
        key = await self._get_key(key_id, tenant_id)
        if key is None:
            return False
        if key.revoked:
            self._evict_cached_key(key)
            return False
        if key.tenant_id is not None and (
            not isinstance(revoked_by, str) or not revoked_by.strip()
        ):
            raise ValueError(
                "canonical API-key revocation requires actor attribution"
            )

        revoked_key = replace(
            key,
            revoked=True,
            revoked_at=datetime.now(timezone.utc),
            revoked_by=revoked_by or "system",
        )

        if self._in_memory:
            self._memory_store[key_id] = revoked_key
        else:
            canonical_revoke = getattr(
                self._persistence,
                "revoke_api_key_with_principal",
                None,
            )
            atomic_revoke = getattr(self._persistence, "revoke_api_key", None)
            if key.tenant_id is not None and callable(canonical_revoke):
                if not await canonical_revoke(revoked_key):
                    self._evict_cached_key(key)
                    return False
            elif atomic_revoke is not None:
                if not await atomic_revoke(revoked_key):
                    self._evict_cached_key(key)
                    return False
            else:
                # Compatibility path for pre-transaction test doubles. A
                # production DynamoPersistence always provides atomic_revoke.
                await self._persistence.update_api_key(revoked_key)
                if tenant_id is None:
                    await self._persistence.bump_revocation_epoch()
                else:
                    await self._persistence.bump_revocation_epoch(tenant_id)

        # Mutate process-local state only after the durable operation succeeds.
        self._evict_cached_key(key)
        return True

    def invalidate_cache(self) -> None:
        """Clear the key cache (e.g., after receiving a revocation from another instance)."""
        self._cache.clear()
        self._requires_epoch_baseline.clear()

    async def rotate_key(
        self,
        key_id: str,
        rotated_by: str,
        tenant_id: str | None = None,
    ) -> tuple[APIKey, str] | None:
        """Revoke old key, then issue a replacement.

        This is deliberately not described as atomic. If replacement creation
        fails, the old credential remains safely revoked and the failure reaches
        the caller.
        """
        old_key = await self._get_key(key_id, tenant_id)
        if old_key is None:
            return None

        atomic_rotate = getattr(
            self._persistence,
            "rotate_api_key_with_principal",
            None,
        )
        if (
            old_key.tenant_id is not None
            and not self._in_memory
            and callable(atomic_rotate)
        ):
            now = datetime.now(timezone.utc)
            raw_key = self.generate_raw_key()
            replacement = APIKey(
                key_id=f"axk_{secrets.token_hex(12)}",
                key_hash=self.hash_key(raw_key),
                project_id=old_key.project_id,
                name=old_key.name,
                scopes=list(old_key.scopes),
                created_by=rotated_by,
                tenant_id=old_key.tenant_id,
                created_at=now,
                expires_at=self._resolve_expiry(
                    old_key.expires_at,
                    old_key.tenant_id,
                    now,
                ),
            )
            revoked_key = replace(
                old_key,
                revoked=True,
                revoked_at=now,
                revoked_by=rotated_by,
            )
            if not await atomic_rotate(
                revoked_key,
                replacement,
                self._principal_for_key(replacement),
            ):
                self._evict_cached_key(old_key)
                return None
            self._evict_cached_key(old_key)
            self._cache_issued_key(replacement)
            return replacement, raw_key

        if not await self.revoke_key(
            key_id,
            old_key.tenant_id,
            revoked_by=rotated_by,
        ):
            return None

        return await self.issue_key(
            project_id=old_key.project_id,
            name=old_key.name,
            scopes=old_key.scopes,
            created_by=rotated_by,
            expires_at=old_key.expires_at,
            tenant_id=old_key.tenant_id,
        )

    async def list_keys(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> list[APIKey]:
        """List all keys for a project (excludes raw key values)."""
        if self._in_memory:
            return [
                key
                for key in self._memory_store.values()
                if key.project_id == project_id and key.tenant_id == tenant_id
            ]
        if tenant_id is None:
            return await self._persistence.list_api_keys_for_project(project_id)
        return await self._persistence.list_api_keys_for_project(
            project_id,
            tenant_id,
        )

    async def _get_key(
        self,
        key_id: str,
        tenant_id: str | None = None,
    ) -> APIKey | None:
        """Fetch a key by ID from whichever store is authoritative."""
        if self._in_memory:
            key = self._memory_store.get(key_id)
            if key is None or key.tenant_id != tenant_id:
                return None
            return key
        if tenant_id is None:
            return await self._persistence.get_api_key(key_id)
        return await self._persistence.get_api_key(key_id, tenant_id)

    async def get_key(
        self,
        key_id: str,
        tenant_id: str | None = None,
    ) -> APIKey | None:
        """Return credential metadata from the authoritative tenant namespace."""
        return await self._get_key(key_id, tenant_id)
