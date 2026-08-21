"""Hierarchical policy resolution — org > business_unit > project > environment.

Key invariant: a child node can never exceed its parent's limits.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from src.gateway.models import PolicyNode, ResolvedPolicy
from src.gateway.persistence import PersistenceConflictError

if TYPE_CHECKING:
    from src.gateway.models import Project
    from src.gateway.persistence import DynamoPersistence

NODE_TYPES_ORDERED = ("org", "business_unit", "project", "environment")
REVISION_POLL_INTERVAL_SECONDS = 5.0

logger = logging.getLogger(__name__)


class PolicyHierarchyStoreUnavailable(RuntimeError):
    """Tenant policy state could not be read or durably updated."""


class PolicyHierarchyWriteConflict(PersistenceConflictError):
    """A tenant hierarchy changed before a conditional write committed."""


def _normalize_tenant_id(tenant_id: str | None) -> str | None:
    if tenant_id is None:
        return None
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be None or a non-empty string")
    return tenant_id


def _project_scope(
    tenant_id: str | None,
    project_id: str,
    project: Project | None,
) -> tuple[str | None, int | None]:
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


def _supports_cas_keywords(save: object) -> bool:
    """Return whether a persistence fake exposes the revision-aware contract."""
    try:
        parameters = inspect.signature(save).parameters.values()
    except (TypeError, ValueError):
        return True
    names = {parameter.name for parameter in parameters}
    has_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    return has_kwargs or {
        "expected_revision",
        "create_only",
    }.issubset(names)


def _validate_revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("policy hierarchy revision must be a non-negative integer")
    return value


class PolicyHierarchyResolver:
    """Resolves effective policy by walking from leaf to root.

    At each level, the most restrictive value wins:
    - Numeric limits: min(parent, child)
    - Model lists: intersection
    """

    def __init__(
        self, persistence: DynamoPersistence, cache_ttl_seconds: int = 300
    ) -> None:
        self._persistence = persistence
        self._cache: dict[
            tuple[str | None, str, str],
            tuple[ResolvedPolicy, float],
        ] = {}
        self._cache_ttl = cache_ttl_seconds
        # Kept as the tenantless map for compatibility with legacy bootstrap and
        # policy admin routes. Canonical tenant state is never placed here.
        self._nodes: dict[str, PolicyNode] = {}
        self._tenant_nodes: dict[str, dict[str, PolicyNode]] = {}
        self._legacy_loaded = False
        self._loaded_tenants: set[str] = set()
        self._tenant_revisions: dict[str, int] = {}
        self._tenant_last_revision_check: dict[str, float] = {}
        self._tenant_refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._tenant_generations: dict[str, int] = {}
        self._revision_poll_interval = REVISION_POLL_INTERVAL_SECONDS

    async def load_nodes(self, *, tenant_id: str | None = None) -> None:
        """Load one policy namespace without falling back across tenants."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if tenant_id is None:
            nodes = await self._persistence.load_all_policy_nodes()
            self._nodes = {n.node_id: n for n in nodes}
            self._legacy_loaded = True
            self._invalidate_cache_for(tenant_id=None)
            return

        await self._ensure_tenant_fresh(tenant_id, force=True)

    async def get_nodes(
        self,
        *,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, PolicyNode], int]:
        """Return one loaded namespace and its whole-hierarchy revision."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if tenant_id is None:
            if not self._legacy_loaded and not self._nodes:
                await self.load_nodes()
            return self._nodes, 0

        await self._ensure_tenant_fresh(tenant_id)
        return (
            self._tenant_nodes[tenant_id],
            self._tenant_revisions[tenant_id],
        )

    def known_revision(self, tenant_id: str | None) -> int:
        """Return the revision already adopted by this resolver."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if tenant_id is None:
            return 0
        return self._tenant_revisions.get(tenant_id, 0)

    async def _ensure_tenant_fresh(
        self,
        tenant_id: str,
        *,
        force: bool = False,
    ) -> None:
        """Poll one tenant at most once per interval, sharing concurrent work."""
        now = time.monotonic()
        last_check = self._tenant_last_revision_check.get(tenant_id)
        if (
            not force
            and tenant_id in self._loaded_tenants
            and last_check is not None
            and now - last_check < self._revision_poll_interval
        ):
            return

        task = self._tenant_refresh_tasks.get(tenant_id)
        if task is None:
            task = asyncio.create_task(self._refresh_tenant(tenant_id))
            self._tenant_refresh_tasks[tenant_id] = task
        try:
            await asyncio.shield(task)
        finally:
            if (
                task.done()
                and self._tenant_refresh_tasks.get(tenant_id) is task
            ):
                self._tenant_refresh_tasks.pop(tenant_id, None)

    async def _refresh_tenant(self, tenant_id: str) -> None:
        """Refresh one tenant, retaining a loaded snapshot on later failures."""
        initially_loaded = tenant_id in self._loaded_tenants
        generation = self._tenant_generations.get(tenant_id, 0)
        try:
            if not initially_loaded:
                nodes, revision = await self._load_tenant_snapshot(tenant_id)
            else:
                known_revision = self._tenant_revisions[tenant_id]
                observed_revision = known_revision
                get_revision = getattr(
                    self._persistence,
                    "get_tenant_policy_hierarchy_revision",
                    None,
                )
                if callable(get_revision):
                    revision = _validate_revision(
                        await get_revision(tenant_id)
                    )
                    observed_revision = revision
                    if revision < known_revision:
                        raise RuntimeError(
                            "tenant policy hierarchy revision moved backwards"
                        )
                    if revision == known_revision:
                        self._tenant_last_revision_check[tenant_id] = (
                            time.monotonic()
                        )
                        return
                nodes, revision = await self._load_tenant_snapshot(tenant_id)
                if revision < observed_revision:
                    raise RuntimeError(
                        "tenant policy hierarchy snapshot moved backwards"
                    )
        except Exception as exc:
            if initially_loaded:
                logger.warning(
                    "Failed to refresh tenant policy hierarchy for %s; "
                    "retaining revision %s",
                    tenant_id,
                    self._tenant_revisions.get(tenant_id),
                    exc_info=True,
                )
                return
            if isinstance(exc, PolicyHierarchyStoreUnavailable):
                raise
            raise PolicyHierarchyStoreUnavailable(
                "Tenant policy persistence is unavailable"
            ) from exc

        if generation != self._tenant_generations.get(tenant_id, 0):
            return
        self._tenant_nodes[tenant_id] = {node.node_id: node for node in nodes}
        self._tenant_revisions[tenant_id] = revision
        self._loaded_tenants.add(tenant_id)
        self._tenant_last_revision_check[tenant_id] = time.monotonic()
        self._invalidate_cache_for(tenant_id=tenant_id)

    async def _load_tenant_snapshot(
        self,
        tenant_id: str,
    ) -> tuple[list[PolicyNode], int]:
        """Load a stable snapshot, with a compatibility path for old fakes."""
        load_snapshot = getattr(
            self._persistence,
            "load_tenant_policy_nodes_snapshot",
            None,
        )
        if callable(load_snapshot):
            snapshot = await load_snapshot(tenant_id)
            if (
                not isinstance(snapshot, tuple)
                or len(snapshot) != 2
                or not isinstance(snapshot[0], list)
            ):
                raise RuntimeError(
                    "tenant policy hierarchy snapshot is malformed"
                )
            return snapshot[0], _validate_revision(snapshot[1])

        load = getattr(self._persistence, "load_tenant_policy_nodes", None)
        if not callable(load):
            if getattr(self._persistence, "enabled", True):
                raise PolicyHierarchyStoreUnavailable(
                    "Tenant policy persistence is unavailable"
                )
            return [], self._tenant_revisions.get(tenant_id, 0)

        nodes = await load(tenant_id)
        if nodes is None:
            raise RuntimeError("tenant policy hierarchy load failed")
        if not isinstance(nodes, list):
            nodes = list(nodes)

        get_revision = getattr(
            self._persistence,
            "get_tenant_policy_hierarchy_revision",
            None,
        )
        revision = (
            _validate_revision(await get_revision(tenant_id))
            if callable(get_revision)
            else self._tenant_revisions.get(tenant_id, 0)
        )
        return nodes, revision

    async def resolve(
        self,
        project_id: str,
        environment: str | None = None,
        *,
        tenant_id: str | None = None,
        project: Project | None = None,
    ) -> ResolvedPolicy:
        """Walk from leaf to root, merging limits within one tenant namespace."""
        tenant_id, project_rate_limit = _project_scope(
            tenant_id,
            project_id,
            project,
        )
        if tenant_id is not None:
            await self._ensure_tenant_fresh(tenant_id)
            nodes = self._tenant_nodes[tenant_id]
        else:
            if not self._legacy_loaded and not self._nodes:
                await self.load_nodes()
            nodes = self._nodes
        cache_key = (tenant_id, project_id, environment or "")
        cached = self._cache.get(cache_key)
        if cached:
            policy, ts = cached
            if (time.time() - ts) < self._cache_ttl:
                return self._with_project_rate_limit(policy, project_rate_limit)

        ancestry = self._ancestry_from_nodes(
            nodes,
            project_id,
            environment,
        )
        policy = self._resolve_ancestry(ancestry)

        self._cache[cache_key] = (policy, time.time())
        return self._with_project_rate_limit(policy, project_rate_limit)

    async def get_ancestry(
        self,
        node_id: str,
        environment: str | None = None,
        *,
        tenant_id: str | None = None,
    ) -> list[PolicyNode]:
        """Return ancestry path [root, ..., leaf] (top-down for merge order)."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if tenant_id is None:
            if not self._legacy_loaded and not self._nodes:
                await self.load_nodes()
            nodes = self._nodes
        else:
            await self._ensure_tenant_fresh(tenant_id)
            nodes = self._tenant_nodes[tenant_id]

        return self._ancestry_from_nodes(nodes, node_id, environment)

    @staticmethod
    def _ancestry_from_nodes(
        nodes: dict[str, PolicyNode],
        node_id: str,
        environment: str | None,
    ) -> list[PolicyNode]:
        """Walk one already refreshed namespace from leaf to root."""
        path: list[PolicyNode] = []
        current_id: str | None = node_id

        # If environment specified, look for env node first
        if environment:
            env_id = f"{node_id}:{environment}"
            if env_id in nodes:
                current_id = env_id

        visited: set[str] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            node = nodes.get(current_id)
            if node is None:
                break
            path.append(node)
            current_id = node.parent_id

        path.reverse()  # root first
        return path

    @staticmethod
    def _with_project_rate_limit(
        policy: ResolvedPolicy,
        project_rate_limit: int | None,
    ) -> ResolvedPolicy:
        """Apply canonical project RPM without mutating the cached hierarchy."""
        if project_rate_limit is None:
            return policy
        effective = replace(policy)
        if effective.rate_limit_rpm is None:
            effective.rate_limit_rpm = project_rate_limit
        else:
            effective.rate_limit_rpm = min(
                effective.rate_limit_rpm,
                project_rate_limit,
            )
        return effective

    def _resolve_ancestry(self, ancestry: list[PolicyNode]) -> ResolvedPolicy:
        """Merge limits top-down (root first). Child narrows, never expands."""
        policy = ResolvedPolicy()

        for node in ancestry:
            policy = self._merge(policy, node)

        return policy

    def _merge(self, current: ResolvedPolicy, node: PolicyNode) -> ResolvedPolicy:
        """Merge node limits into current policy (most restrictive wins)."""
        limits = node.limits

        # Rate limit: min
        node_rpm = limits.get("rate_limit_rpm")
        if node_rpm is not None:
            if current.rate_limit_rpm is None:
                current.rate_limit_rpm = node_rpm
            else:
                current.rate_limit_rpm = min(current.rate_limit_rpm, node_rpm)

        # Budget: min
        node_budget = limits.get("budget_limit")
        if node_budget is not None:
            if current.budget_limit is None:
                current.budget_limit = node_budget
            else:
                current.budget_limit = min(current.budget_limit, node_budget)

        # Max tokens: min
        node_max_tokens = limits.get("max_tokens_per_request")
        if node_max_tokens is not None:
            if current.max_tokens_per_request is None:
                current.max_tokens_per_request = node_max_tokens
            else:
                current.max_tokens_per_request = min(
                    current.max_tokens_per_request, node_max_tokens
                )

        # Allowed models: intersection
        node_models = limits.get("allowed_models")
        if node_models is not None:
            if current.allowed_models is None:
                current.allowed_models = list(node_models)
            else:
                current.allowed_models = [
                    m for m in current.allowed_models if m in node_models
                ]

        # Allowed providers: intersection
        node_providers = limits.get("allowed_providers")
        if node_providers is not None:
            if current.allowed_providers is None:
                current.allowed_providers = list(node_providers)
            else:
                current.allowed_providers = [
                    p for p in current.allowed_providers if p in node_providers
                ]

        # PII redaction: once enabled by a parent, children cannot disable
        if limits.get("pii_redaction_enabled"):
            current.pii_redaction_enabled = True

        # PII types: union (child can add types but never remove parent's)
        node_pii_types = limits.get("pii_redact_types")
        if node_pii_types is not None:
            if current.pii_redact_types is None:
                current.pii_redact_types = list(node_pii_types)
            else:
                merged = set(current.pii_redact_types) | set(node_pii_types)
                current.pii_redact_types = sorted(merged)

        # PII reinject: the more private setting wins. Once a parent turns off
        # re-injection (permanent redaction), a child can never turn it back on.
        if limits.get("pii_reinject") is False:
            current.pii_reinject = False

        # Entity detection (names/addresses): same ratchet as pii_redaction_enabled
        # — a parent that turns it on cannot be overridden downward. Separate from
        # pii_redaction_enabled because it calls a paid per-request service, so
        # enabling redaction must never silently enable it.
        if limits.get("pii_ner_enabled"):
            current.pii_ner_enabled = True

        # Entity types: union, matching pii_redact_types. A child can broaden
        # what is detected but never narrow what a parent requires.
        node_ner_types = limits.get("pii_ner_types")
        if node_ner_types is not None:
            if current.pii_ner_types is None:
                current.pii_ner_types = list(node_ner_types)
            else:
                current.pii_ner_types = sorted(
                    set(current.pii_ner_types) | set(node_ner_types))

        return current

    async def set_node(
        self,
        node: PolicyNode,
        *,
        tenant_id: str | None = None,
        create_only: bool | None = None,
    ) -> int:
        """Create or update a node in an explicit policy namespace."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if tenant_id is not None:
            await self._ensure_tenant_fresh(tenant_id)
            validation_nodes = self._tenant_nodes[tenant_id]
        else:
            if not self._legacy_loaded and not self._nodes:
                await self.load_nodes()
            validation_nodes = self._nodes
        violations = self._validate_node_limits_against_nodes(
            node,
            validation_nodes,
        )
        if violations:
            raise ValueError(
                f"Node '{node.node_id}' exceeds parent limits: {violations}"
            )

        if tenant_id is None:
            await self._persistence.save_policy_node(node)
            self._nodes[node.node_id] = node
            self._legacy_loaded = True
            revision = 0
        else:
            scoped_nodes = self._tenant_nodes[tenant_id]
            node_exists = node.node_id in scoped_nodes
            if create_only is None:
                create_only = not node_exists
            elif create_only == node_exists:
                self._tenant_last_revision_check.pop(tenant_id, None)
                raise PolicyHierarchyWriteConflict(
                    "Tenant policy hierarchy changed concurrently"
                )

            save = getattr(self._persistence, "save_tenant_policy_node", None)
            if not callable(save):
                if getattr(self._persistence, "enabled", True):
                    raise PolicyHierarchyStoreUnavailable(
                        "Tenant policy persistence is unavailable"
                    )
                revision = self._tenant_revisions[tenant_id] + 1
            else:
                expected_revision = self._tenant_revisions[tenant_id]
                revision_aware = _supports_cas_keywords(save)
                try:
                    if revision_aware:
                        saved = await save(
                            tenant_id,
                            node,
                            expected_revision=expected_revision,
                            create_only=create_only,
                        )
                    else:
                        saved = await save(tenant_id, node)
                except PersistenceConflictError as exc:
                    self._tenant_last_revision_check.pop(tenant_id, None)
                    raise PolicyHierarchyWriteConflict(
                        "Tenant policy hierarchy changed concurrently"
                    ) from exc
                except Exception as exc:
                    raise PolicyHierarchyStoreUnavailable(
                        "Tenant policy persistence is unavailable"
                    ) from exc
                if revision_aware:
                    try:
                        revision = _validate_revision(saved)
                    except ValueError as exc:
                        raise PolicyHierarchyStoreUnavailable(
                            "Tenant policy update was not persisted"
                        ) from exc
                    if revision != expected_revision + 1:
                        raise PolicyHierarchyStoreUnavailable(
                            "Tenant policy update returned an invalid revision"
                        )
                else:
                    if (
                        getattr(self._persistence, "enabled", True)
                        and saved is not True
                    ):
                        raise PolicyHierarchyStoreUnavailable(
                            "Tenant policy update was not persisted"
                        )
                    revision = expected_revision + 1

            self._tenant_nodes[tenant_id][node.node_id] = node
            self._tenant_revisions[tenant_id] = revision
            self._tenant_generations[tenant_id] = (
                self._tenant_generations.get(tenant_id, 0) + 1
            )
            self._loaded_tenants.add(tenant_id)
            self._tenant_last_revision_check[tenant_id] = time.monotonic()
        self._invalidate_cache_for(tenant_id=tenant_id)
        return revision

    async def validate_node_limits(
        self,
        node: PolicyNode,
        *,
        tenant_id: str | None = None,
    ) -> list[str]:
        """Validate that node limits don't exceed parent. Returns violations."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if tenant_id is None:
            if not self._legacy_loaded and not self._nodes:
                await self.load_nodes()
            nodes = self._nodes
        else:
            await self._ensure_tenant_fresh(tenant_id)
            nodes = self._tenant_nodes[tenant_id]

        return self._validate_node_limits_against_nodes(node, nodes)

    @staticmethod
    def _validate_node_limits_against_nodes(
        node: PolicyNode,
        nodes: dict[str, PolicyNode],
    ) -> list[str]:
        """Validate a node against one already refreshed hierarchy snapshot."""
        if node.parent_id is None:
            return []

        parent = nodes.get(node.parent_id)
        if parent is None:
            return []

        violations: list[str] = []
        parent_limits = parent.limits
        node_limits = node.limits

        # Check numeric limits (child must be <= parent)
        for field in ("rate_limit_rpm", "budget_limit", "max_tokens_per_request"):
            parent_val = parent_limits.get(field)
            node_val = node_limits.get(field)
            if parent_val is not None and node_val is not None:
                if node_val > parent_val:
                    violations.append(
                        f"{field}: {node_val} exceeds parent limit {parent_val}"
                    )

        # Check model list (child must be subset of parent)
        parent_models = parent_limits.get("allowed_models")
        node_models = node_limits.get("allowed_models")
        if parent_models is not None and node_models is not None:
            extra = set(node_models) - set(parent_models)
            if extra:
                violations.append(
                    f"allowed_models: {extra} not in parent's allowed models"
                )

        return violations

    def _invalidate_cache_for(self, *, tenant_id: str | None) -> None:
        """Invalidate all descendants in one isolated policy namespace."""
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if key[0] != tenant_id
        }
