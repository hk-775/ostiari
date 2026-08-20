"""Fleet-wide convergence for request-governing configuration.

Projects, user restrictions, and the model registry are hydrated at startup.
Without refresh, a write is visible only to the task that served it. Behind the
shipped multi-replica topology that makes request enforcement and routing depend
on which task receives the next request:

* ``GatewayAgent`` resolves ``self._projects[project_id]`` to find the project's
  budget limit, allowed-models list, guardrails, and rate limit. An unresolved
  project is not an error; it means *no gate at all*.
* ``self._user_configs[user_id]["allowed_models"]`` is the per-user model
  restriction, and ``cost_tracker._user_budgets`` is armed from the same row.

So a restriction or model mapping an operator sets is enforced by the task that
took the PUT and ignored by the others, chosen per request by the load balancer.

    in the store: {'alice': {'allowed_models': ['claude-haiku']}}
    task A, alice asks for claude-opus: 403 model_not_allowed
    task B, alice asks for claude-opus: 200 routed

Projects and users use the same version-counter mechanism as
``CedarPolicyService.refresh_if_stale``. The model registry and region topology
are each one revisioned document, so they are polled directly on the same
bounded interval. Steady-state work is a few small ``GetItem`` calls per
instance per window, not table scans per request.

This keeps model routing and data-residency rules converged without coupling
either document write to a second counter write.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.gateway.multi_region.region_config import apply_persisted_topology
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_config_signing import (
    RoutingConfigRollbackError,
    RoutingConfigSignatureError,
)

logger = logging.getLogger(__name__)


class RegionTopologyUnavailable(RuntimeError):
    """The authoritative topology could not be checked safely."""


class ConfigSyncService:
    """Re-adopts fleet-wide request configuration on a version change.

    Holds the *same* dict objects ``AdminAPI`` and ``GatewayAgent`` hold and
    mutates them in place. Rebinding would put this service's converged view in
    an object nobody reads — the failure mode that made ``x or {}`` a fleet-wide
    bug in the first place.
    """

    # Matches CedarPolicyService.POLICY_SYNC_TTL_SECONDS deliberately. Both bound
    # the window in which two tasks disagree about an *enforcement* rule rather
    # than about a displayed number, so they should not be tuned apart: an
    # operator who has waited out one has waited out the other. The check is a
    # single counter GetItem, so 5s is affordable per request.
    CONFIG_SYNC_TTL_SECONDS = 5.0

    def __init__(
        self,
        projects: dict,
        user_configs: dict[str, dict],
        cost_tracker,
        persistence=None,
        model_registry=None,
        policy_resolver=None,
        region_config=None,
        health_monitor=None,
        region_lock: asyncio.Lock | None = None,
    ) -> None:
        self._projects = projects
        self._user_configs = user_configs
        self._cost_tracker = cost_tracker
        self._persistence = persistence
        self._model_registry = model_registry
        self._policy_resolver = policy_resolver
        self._region_config = region_config
        self._health_monitor = health_monitor
        self._region_lock = region_lock or asyncio.Lock()
        # Negative infinity, not 0: time.monotonic() has an arbitrary origin and
        # can legitimately be near 0 early in the process, which with a 0 sentinel
        # would skip the first check.
        self._last_version_check = float("-inf")
        self._known_version: int | None = None
        self._refresh_task: asyncio.Task | None = None
        self._local_generation = 0
        self._last_model_check = float("-inf")
        self._known_model_revision = (
            getattr(model_registry, "revision", 0)
            if model_registry is not None
            else None
        )
        persisted_snapshot = getattr(
            persistence,
            "authenticated_routing_snapshot",
            None,
        )
        self._last_good_routing_snapshot = persisted_snapshot or (
            RoutingConfigSnapshot.from_registry(model_registry)
            if model_registry is not None
            else None
        )
        self._routing_sync_error: str | None = None
        self._model_refresh_task: asyncio.Task | None = None
        self._model_generation = 0
        self._last_region_check = float("-inf")
        self._known_region_revision = (
            getattr(region_config, "revision", 0)
            if region_config is not None
            else None
        )
        self._region_refresh_task: asyncio.Task | None = None

    @property
    def region_lock(self) -> asyncio.Lock:
        """Lock shared with the local topology writer."""
        return self._region_lock

    @property
    def active_routing_snapshot(
        self,
    ) -> RoutingConfigSnapshot | None:
        """The last fully validated routing configuration adopted here."""
        return self._last_good_routing_snapshot

    @property
    def routing_config_status(self) -> dict[str, object]:
        """Return sanitized last-known-good and synchronization state."""
        snapshot = self._last_good_routing_snapshot
        return {
            "status": (
                "degraded"
                if self._routing_sync_error is not None
                else "synchronized"
            ),
            "revision": (
                snapshot.revision if snapshot is not None else None
            ),
            "sha256": snapshot.sha256 if snapshot is not None else None,
            "signed": snapshot.is_signed if snapshot is not None else False,
            "error": self._routing_sync_error,
        }

    @staticmethod
    def _routing_error_category(exc: Exception) -> str:
        if isinstance(exc, RoutingConfigRollbackError):
            return "rollback_rejected"
        if isinstance(exc, RoutingConfigSignatureError):
            return "signature_verification_failed"
        return "snapshot_unavailable"

    async def refresh_if_stale(self) -> bool:
        """Adopt fleet config if another instance changed it. Returns whether it did.

        Single-flighted for the same reason the other two refreshes are: the TTL
        check straddles an await, so without it every request in a concurrent
        burst passes the check and issues its own pair of scans.
        """
        if self._persistence is None or not self._persistence.enabled:
            return False

        model_refreshed = await self.refresh_routing_if_stale()
        region_refreshed = await self._refresh_region_if_stale()
        now = time.monotonic()
        if now - self._last_version_check < self.CONFIG_SYNC_TTL_SECONDS:
            return model_refreshed or region_refreshed

        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh(now))
        try:
            config_refreshed = await asyncio.shield(self._refresh_task)
            return (
                model_refreshed
                or region_refreshed
                or config_refreshed
            )
        except Exception:
            logger.warning("Config refresh failed", exc_info=True)
            return model_refreshed or region_refreshed

    async def refresh_routing_if_stale(self) -> bool:
        """Poll only the signed routing document for readiness and requests."""
        if self._persistence is None or not self._persistence.enabled:
            return False
        return await self._refresh_model_registry_if_stale()

    async def _refresh_model_registry_if_stale(self) -> bool:
        """Adopt a newer complete registry document, at most once per TTL."""
        if self._model_registry is None:
            return False
        loader = getattr(
            self._persistence,
            "load_model_registry_snapshot",
            None,
        )
        if not callable(loader):
            logger.error("Model registry loading is not configured")
            return False
        now = time.monotonic()
        if (
            now - self._last_model_check
            < self.CONFIG_SYNC_TTL_SECONDS
        ):
            return False
        if (
            self._model_refresh_task is None
            or self._model_refresh_task.done()
        ):
            self._model_refresh_task = asyncio.create_task(
                self._refresh_model_registry(now)
            )
        try:
            return await asyncio.shield(self._model_refresh_task)
        except Exception as exc:
            # Keep routing with the last fully validated snapshot. Do not move
            # the check clock: the next request retries instead of treating an
            # outage or malformed document as a successful refresh.
            self._routing_sync_error = self._routing_error_category(exc)
            logger.warning("Model registry refresh failed", exc_info=True)
            return False

    async def _refresh_model_registry(self, now: float) -> bool:
        generation = self._model_generation
        snapshot = (
            await self._persistence.load_model_registry_snapshot(
                after_revision=getattr(
                    self._model_registry,
                    "revision",
                    0,
                )
            )
        )
        if generation != self._model_generation:
            return False
        if snapshot is None:
            if getattr(self._model_registry, "revision", 0) != 0:
                logger.error(
                    "The durable model registry disappeared; retaining "
                    "revision %s",
                    getattr(self._model_registry, "revision", 0),
                )
                self._routing_sync_error = "snapshot_missing"
            else:
                self._routing_sync_error = None
            self._last_model_check = now
            return False

        revision = snapshot.revision
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise RuntimeError(
                "The durable model registry revision is invalid"
            )
        live_revision = getattr(self._model_registry, "revision", 0)
        if revision <= live_revision:
            self._known_model_revision = live_revision
            self._routing_sync_error = None
            self._last_model_check = now
            return False

        # Applying assigns the complete parsed model map in one operation. A
        # malformed or unavailable refresh leaves the previous snapshot active.
        snapshot.apply(self._model_registry)
        logger.info(
            "Adopting model registry revision %s -> %s (%d models)",
            live_revision,
            revision,
            len(self._model_registry.models),
        )
        self._last_good_routing_snapshot = snapshot
        self._known_model_revision = revision
        self._routing_sync_error = None
        self._last_model_check = now
        return True

    async def _refresh_region_if_stale(self) -> bool:
        if self._region_config is None:
            return False
        loader = getattr(
            self._persistence,
            "load_region_topology_snapshot",
            None,
        )
        if loader is None:
            raise RegionTopologyUnavailable(
                "Region topology loading is not configured"
            )
        now = time.monotonic()
        if (
            now - self._last_region_check
            < self.CONFIG_SYNC_TTL_SECONDS
        ):
            return False
        if (
            self._region_refresh_task is None
            or self._region_refresh_task.done()
        ):
            self._region_refresh_task = asyncio.create_task(
                self._refresh_region(now)
            )
        try:
            return await asyncio.shield(self._region_refresh_task)
        except RegionTopologyUnavailable:
            raise
        except Exception as exc:
            logger.error("Region topology refresh failed", exc_info=True)
            raise RegionTopologyUnavailable(
                "Region topology is temporarily unavailable"
            ) from exc

    async def _refresh_region(self, now: float) -> bool:
        async with self._region_lock:
            snapshot = (
                await self._persistence.load_region_topology_snapshot()
            )
            if snapshot is None:
                self._known_region_revision = getattr(
                    self._region_config,
                    "revision",
                    0,
                )
                await self._reconcile_health_monitor()
                self._last_region_check = now
                return False

            revision = snapshot.get("revision")
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
            ):
                raise RegionTopologyUnavailable(
                    "Region topology revision is invalid"
                )
            live_revision = getattr(self._region_config, "revision", 0)
            if revision <= live_revision:
                # A delayed read must never roll back a local commit or a newer
                # refresh. Track the live revision, not the stale poller's prior
                # observation, so the next check starts from reality.
                self._known_region_revision = live_revision
                await self._reconcile_health_monitor()
                self._last_region_check = now
                return False

            apply_persisted_topology(
                self._region_config,
                snapshot,
                preserve_health=True,
            )
            logger.info(
                "Adopting region topology revision %s -> %s",
                live_revision,
                revision,
            )
            self._known_region_revision = revision
            await self._reconcile_health_monitor()
            self._last_region_check = now
            return True

    async def _reconcile_health_monitor(self) -> None:
        reconcile = getattr(self._health_monitor, "reconcile", None)
        if callable(reconcile):
            await reconcile()

    async def _refresh(self, now: float) -> bool:
        generation = self._local_generation
        version = await self._persistence.get_config_version()
        if version is None:
            # Unreadable. Keep the config we have and retry on the next request
            # rather than advancing the clock — an outage must not buy a full
            # window of divergence.
            return False

        if version == self._known_version:
            self._last_version_check = now
            return False

        projects, configs = await asyncio.gather(
            self._persistence.load_projects_or_none(),
            self._persistence.load_user_configs_or_none(),
        )
        if projects is None or configs is None:
            # The version moved but a scan failed. Do NOT adopt the empty result:
            # that would clear every budget limit and model restriction in the
            # fleet because one scan timed out — a read failure turned into a
            # fleet-wide enforcement bypass. Leave _known_version alone so the
            # next request retries.
            logger.error(
                "Config version moved to %s but a config scan failed "
                "(projects=%s, user_configs=%s); continuing with the loaded config",
                version,
                "ok" if projects is not None else "failed",
                "ok" if configs is not None else "failed",
            )
            return False

        confirmed_version = await self._persistence.get_config_version()
        if confirmed_version is None or confirmed_version != version:
            # A write landed while the two scans were in flight. Mixing rows
            # from before and after that write would acknowledge a snapshot that
            # never existed, so retry the whole read on the next request.
            return False
        if generation != self._local_generation:
            # This process committed a local mutation while the scan was
            # running. Publishing the older scan would roll that mutation back.
            return False

        # Stored entries win; entries this instance knows and the store does not
        # survive. Same merge as bootstrap's and as the Cedar refresh's: seed-file
        # projects and users are not in DynamoDB, so replacing outright would
        # silently drop every seeded one.
        self._projects.update(projects)
        for user_id, config in configs.items():
            self._user_configs[user_id] = config

        # Adopting the dicts is not the same as arming enforcement — the same
        # distinction #89 fixed for the restart path. Limits live in
        # cost_tracker._budgets / ._user_budgets, which no dict update touches, so
        # without this the refreshed limit would be displayed and enforced by
        # nothing.
        self._register_budgets(projects, configs)

        logger.info(
            "Adopting fleet config: version %s -> %s (%d projects, %d user configs)",
            self._known_version, version, len(projects), len(configs),
        )
        self._known_version = version
        self._last_version_check = now
        return True

    def _register_budgets(self, projects: dict, configs: dict[str, dict]) -> None:
        """Arm enforcement for the limits just adopted.

        Deliberately does not touch the spend counters. Limits and spend have
        different owners: ``_bump_spend_fleet_wide`` keeps the counters fleet-wide
        already, and writing to them from a config refresh is how a read path
        reopens a closed budget gate.
        """
        for project in projects.values():
            if project.budget_limit is None and project.alert_threshold is None:
                continue
            self._cost_tracker.register_project(
                project.project_id,
                budget_limit=project.budget_limit,
                alert_threshold=project.alert_threshold,
                tenant_id=project.tenant_id,
            )
            # Only where no node exists, matching _register_persisted_budgets: a
            # real org -> team -> project hierarchy carries a tighter parent cap
            # that a flat per-project node would flatten away.
            if (
                self._policy_resolver is not None
                and project.budget_limit is not None
                and project.project_id not in self._policy_resolver._nodes
            ):
                from src.gateway.models import PolicyNode

                self._policy_resolver._nodes[project.project_id] = PolicyNode(
                    node_id=project.project_id,
                    node_type="project",
                    parent_id=None,
                    display_name=project.name,
                    limits={"budget_limit": project.budget_limit},
                )

        for user_id, config in configs.items():
            # Registered even when both limits are None, matching bootstrap:
            # clearing a limit is a deliberate act, and a config row exists
            # because someone configured the user.
            self._cost_tracker.register_user(
                user_id,
                budget_limit=config.get("budget_limit"),
                alert_threshold=config.get("alert_threshold"),
            )

    def note_local_version(self, version: int | None) -> None:
        """Record a version this instance produced by writing config itself.

        Without this the writing instance sees its own bump as a remote change on
        the next poll and re-scans to learn what it already knows.
        """
        if version is not None:
            self._known_version = version
            self._last_version_check = time.monotonic()

    def invalidate_local_config(self) -> None:
        """Force a verified snapshot after this process commits a local write."""
        self._local_generation += 1
        self._last_version_check = float("-inf")

    def note_local_model_snapshot(
        self,
        snapshot: RoutingConfigSnapshot,
    ) -> None:
        """Record an authenticated registry snapshot published locally."""
        revision = snapshot.revision
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise ValueError(
                "model registry revision must be a positive integer"
            )
        # Invalidates a snapshot whose read was already in flight before the
        # local CAS committed, preventing it from publishing an older revision.
        self._model_generation += 1
        self._known_model_revision = revision
        self._last_good_routing_snapshot = snapshot
        self._routing_sync_error = None
        self._last_model_check = time.monotonic()

    def note_local_model_revision(self, revision: int) -> None:
        """Compatibility wrapper for unsigned local-development publication."""
        if self._model_registry is None:
            raise RuntimeError("model registry is not configured")
        snapshot = RoutingConfigSnapshot.from_registry(
            self._model_registry
        )
        if snapshot.revision != revision:
            raise ValueError(
                "model registry revision does not match the live snapshot"
            )
        self.note_local_model_snapshot(snapshot)

    def note_local_region_revision(self, revision: int) -> None:
        """Record a topology revision this instance already published."""
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ValueError(
                "topology revision must be a non-negative integer"
            )
        self._known_region_revision = revision
        self._last_region_check = time.monotonic()
