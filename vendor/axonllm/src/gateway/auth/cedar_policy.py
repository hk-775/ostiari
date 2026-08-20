"""Cedar-subset policy evaluation for gateway authorization.

Implements the ``PolicyService`` protocol used by ``AuthMiddleware``. Policies
are Cedar ``permit``/``forbid`` statements stored as text (see demo_seed.yaml).
This evaluator supports the practical subset AxonLLM uses:

    permit(principal, action == Action::"read", resource);
    forbid(principal, action, resource) unless { principal.role == "senior" };
    permit(principal, action, resource) when { principal.project == "proj-alpha" };

Semantics follow Cedar's core rules, with default-deny scoped per action:
  * An action is *governed* once some enforcing statement mentions it (by name,
    or by omitting the action clause and so covering every action).
  * Within a governed action: default deny — allowed only if some ``permit``
    matches — and ``forbid`` overrides ``permit``.
  * An action no policy governs is ALLOW; see ``CedarPolicyService`` for why.
  * A statement matches when its principal/action/resource scope matches AND
    its ``when`` condition holds AND its ``unless`` condition does not.

Scope clauses supported: the bare variable (``principal``, ``resource``) or an
action equality (``action == Action::"read"``). Principal and resource
*equalities* are deliberately rejected rather than ignored — a dropped
``principal == User::"alice"`` widens a statement from one user to everyone, and a
dropped ``resource == Resource::"/api/chat"`` widens it from one endpoint to all
of them. Both fail open, so they must not parse.

Conditions support ``principal.<attr> == "value"`` (and its negation) joined by
``&&``. Anything the parser does not understand returns None, and the caller's
options are to reject it (``POST /admin/policies``, with a 400) or skip it with a
warning (startup, so one bad stored policy cannot stop the gateway booting).

This is a pure-Python evaluator — no native Cedar dependency.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from src.gateway.models import RequestContext

logger = logging.getLogger(__name__)

# HTTP method -> Cedar action name. Policies speak in coarse actions ("read",
# "write") rather than raw HTTP verbs.
_METHOD_TO_ACTION = {
    "get": "read",
    "head": "read",
    "options": "read",
    "post": "write",
    "put": "write",
    "patch": "write",
    "delete": "write",
}


class PolicyStoreUnavailable(RuntimeError):
    """The authoritative policy scope could not be initialized or refreshed."""


def _normalize_tenant_id(tenant_id: object, *, source: str) -> str | None:
    """Return a usable tenant id without conflating malformed ids with legacy."""
    if tenant_id is None:
        return None
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError(f"{source} tenant_id must be a non-empty string or None")
    return tenant_id


def _policy_tenant_id(policy: dict) -> str | None:
    """Read a policy's durable tenant scope.

    Missing/None is the explicit legacy scope. Invalid values must not fall back
    to that scope because doing so could broaden a tenant policy fleet-wide.
    """
    return _normalize_tenant_id(policy.get("tenant_id"), source="policy")


def http_method_to_action(method: str) -> str:
    """Map an HTTP method to the coarse Cedar action name."""
    return _METHOD_TO_ACTION.get(method.lower(), method.lower())


@dataclass
class _Condition:
    """A single ``principal.attr == value`` clause, optionally negated."""

    attr: str
    value: str
    negated: bool

    def holds(self, ctx: RequestContext) -> bool:
        if self.attr == "role":
            # Cedar treats role as scalar; a caller may hold several roles, so
            # equality means "has this role".
            equal = self.value in ctx.roles
        else:
            equal = _principal_attr(ctx, self.attr) == self.value
        return (not equal) if self.negated else equal


@dataclass
class _Statement:
    effect: str  # "permit" | "forbid"
    action: str | None  # required action name, or None for "any"
    conditions_when: list[_Condition]
    conditions_unless: list[_Condition]

    def matches(self, ctx: RequestContext, action: str) -> bool:
        if self.action is not None and self.action != action:
            return False
        # when: every clause must hold
        if not all(c.holds(ctx) for c in self.conditions_when):
            return False
        # unless: if every clause holds, the exception fires -> no match
        if self.conditions_unless and all(c.holds(ctx) for c in self.conditions_unless):
            return False
        return True


# Cedar principal attribute -> RequestContext field name.
_ATTR_ALIASES = {"project": "project_id", "tenant": "tenant_id", "user": "user_id"}


def _principal_attr(ctx: RequestContext, attr: str) -> str | None:
    """Resolve ``principal.<attr>`` against the request context.

    ``role`` maps to the caller's roles (Cedar treats role as a scalar here,
    so a match against any held role counts). Other attributes map to the
    same-named context field, via ``_ATTR_ALIASES`` where they differ.
    """
    if attr == "role":
        # Handled specially by _Condition.holds so multi-role callers match.
        return None
    field = _ATTR_ALIASES.get(attr, attr)
    return getattr(ctx, field, None)


_ACTION_RE = re.compile(r'action\s*==\s*Action::"([^"]+)"')
_COND_RE = re.compile(r'principal\.(\w+)\s*(==|!=)\s*"([^"]+)"')
# The scope triple only — everything up to the closing paren of permit(...) /
# forbid(...). A when/unless body follows it and is parsed separately, so the
# rejections below must not see `principal.role == "senior"`.
_SCOPE_RE = re.compile(r"^(?:permit|forbid)\s*\((.*?)\)", re.DOTALL)
# Scope clauses the evaluator cannot honour. Left to the bare-variable path they
# would be dropped, and since each one *narrows* a statement, dropping it widens
# the statement's effect — fail-open. See the module docstring.
_UNSUPPORTED_SCOPE_RES = (
    re.compile(r"\bprincipal\s*(?:==|\bin\b)"),
    re.compile(r"\bresource\s*(?:==|\bin\b)"),
    re.compile(r"\baction\s+in\b"),
)


def _parse_conditions(clause: str) -> list[_Condition] | None:
    """Parse a ``when``/``unless`` body. Returns None if anything is unparseable."""
    conds: list[_Condition] = []
    for part in clause.split("&&"):
        part = part.strip()
        if not part:
            continue
        m = _COND_RE.search(part)
        if not m:
            return None
        attr, op, value = m.group(1), m.group(2), m.group(3)
        conds.append(_Condition(attr=attr, value=value, negated=(op == "!=")))
    return conds


def parse_policy(text: str) -> _Statement | None:
    """Parse a single Cedar statement. Returns None if unsupported."""
    text = text.strip().rstrip(";").strip()
    if text.startswith("permit"):
        effect = "permit"
    elif text.startswith("forbid"):
        effect = "forbid"
    else:
        return None

    scope_match = _SCOPE_RE.match(text)
    if scope_match is None:
        return None
    scope = scope_match.group(1)
    if any(pattern.search(scope) for pattern in _UNSUPPORTED_SCOPE_RES):
        return None

    action_match = _ACTION_RE.search(scope)
    action = action_match.group(1) if action_match else None

    when_when: list[_Condition] = []
    when_unless: list[_Condition] = []
    for keyword, target in (("when", "when"), ("unless", "unless")):
        m = re.search(keyword + r"\s*\{([^}]*)\}", text)
        if m:
            parsed = _parse_conditions(m.group(1))
            if parsed is None:
                return None
            if target == "when":
                when_when = parsed
            else:
                when_unless = parsed

    return _Statement(
        effect=effect,
        action=action,
        conditions_when=when_when,
        conditions_unless=when_unless,
    )


class CedarPolicyService:
    """Evaluates Cedar-subset policies for the AuthMiddleware PolicyService hook.

    Default-deny is scoped to the actions the policy set actually talks about.
    Textbook Cedar denies anything no ``permit`` covers, which is right when the
    whole policy set is authored before deployment. Here it is authored
    incrementally over HTTP, so a global default-deny makes the *first* policy an
    outage: ``permit(… Action::"read" …)`` says nothing about writes, and strict
    default-deny reads that silence as "forbid every write" — including
    ``POST /admin/policies``, so the second policy that would fix it cannot be
    submitted. Restarting is the only way out, and pre-fix policies did not
    survive a restart either.

    So an action is governed only once some enforcing statement mentions it.
    Within a governed action Cedar's rules are unchanged: a matching permit is
    required and a matching forbid overrides it. An ungoverned action falls
    through to the layers that are always on — authentication, admin RBAC, and
    quota enforcement — rather than to an unauthored deny.

    LOG_ONLY statements govern nothing, which is what makes the mode observable:
    ``POST /admin/policies`` defaults to LOG_ONLY, so the documented way to trial
    a policy must not be able to change any decision.
    """

    # How long an instance may keep enforcing a policy set without checking
    # whether the fleet's has moved. The check is one small GetItem on a single
    # counter, not a scan of the policy table, so this can be tight; 5s bounds
    # the window in which two instances disagree about an authorization rule
    # while still collapsing a burst of requests into one read.
    POLICY_SYNC_TTL_SECONDS = 5.0

    def __init__(
        self,
        policies: list[dict],
        persistence=None,
    ) -> None:
        """Build from a list of policy dicts ({name, policy_text, mode, ...}).

        ``persistence`` is optional so single-instance and no-DynamoDB
        deployments construct exactly as before and never poll.
        """
        self._statements: list[tuple[_Statement, dict]] = []
        # Actions some enforcing statement governs; None means "every action",
        # contributed by a statement with no action clause.
        self._governed: set[str | None] = set()
        self._tenant_statements: dict[str, list[tuple[_Statement, dict]]] = {}
        self._tenant_governed: dict[str, set[str | None]] = {}
        self._persistence = persistence
        # The list object the admin API also holds, so a local write and a
        # fleet-wide reload converge on one source rather than two.
        self._policies = policies
        self._tenant_policies: dict[str, list[dict]] = {}
        # The set this process booted with, kept so a fleet reload can merge over
        # it rather than replace it. Seed-file policies are not in DynamoDB — only
        # ones written through POST /admin/policies are — so adopting the stored
        # set wholesale would silently drop every seeded policy, including a
        # seeded forbid. Bootstrap already merges the two by name; this keeps the
        # reload consistent with that.
        self._seeded: list[dict] = []
        self._tenant_seeded: dict[str, list[dict]] = {}
        for policy in policies:
            try:
                tenant_id = _policy_tenant_id(policy)
            except ValueError:
                logger.warning(
                    "Skipping policy %r with an invalid tenant scope",
                    policy.get("name"),
                )
                continue
            target = self._seeded if tenant_id is None else self._tenant_seeded.setdefault(tenant_id, [])
            target.append(dict(policy))

        # The scalar fields remain the explicit tenantless/legacy state. Tenant
        # requests never read or mutate them.
        self._last_version_check = float("-inf")
        self._known_version: int | None = None
        self._refresh_task: asyncio.Task | None = None
        self._tenant_last_version_checks: dict[str, float] = {}
        self._tenant_known_versions: dict[str, int | None] = {}
        self._tenant_refresh_tasks: dict[str, asyncio.Task] = {}
        self._initialized_tenants: set[str] = set()
        self._tenant_sync_successes: dict[str, int] = {}
        self._reload_generation = 0
        self._tenant_reload_generations: dict[str, int] = {}
        self.reload(policies)

    @staticmethod
    def _context_tenant_id(context: RequestContext | None) -> str | None:
        if context is None:
            return None
        return _normalize_tenant_id(
            context.tenant_id,
            source="request context",
        )

    async def refresh_if_stale(
        self,
        context: RequestContext | None = None,
        *,
        require_fresh: bool = False,
    ) -> bool:
        """Re-adopt the fleet's policy set if another instance changed it.

        Returns whether a reload happened. Cheap by design: a version ``GetItem``
        at most once per ``POLICY_SYNC_TTL_SECONDS``, and the policy scan only
        when that number has actually moved.

        Single-flighted for the same reason the admin usage refresh is — the TTL
        check straddles an await, so without it a burst of concurrent requests
        would each pass the check and issue its own scan.
        """
        if self._persistence is None or not self._persistence.enabled:
            return False

        try:
            tenant_id = self._context_tenant_id(context)
        except ValueError:
            logger.error("Refusing Cedar policy refresh for a malformed tenant context")
            return False

        now = time.monotonic()
        if tenant_id is None:
            if now - self._last_version_check < self.POLICY_SYNC_TTL_SECONDS:
                return False
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._refresh(now, tenant_id=None))
            task = self._refresh_task
        else:
            last_check = self._tenant_last_version_checks.get(
                tenant_id,
                float("-inf"),
            )
            if (
                not require_fresh
                and tenant_id in self._initialized_tenants
                and now - last_check < self.POLICY_SYNC_TTL_SECONDS
            ):
                return False
            previous_successes = self._tenant_sync_successes.get(tenant_id, 0)
            task = self._tenant_refresh_tasks.get(tenant_id)
            if task is None or task.done():
                task = asyncio.create_task(self._refresh(now, tenant_id=tenant_id))
                self._tenant_refresh_tasks[tenant_id] = task

        try:
            refreshed = await asyncio.shield(task)
        except Exception:
            logger.warning(
                "Cedar policy refresh failed for tenant=%r",
                tenant_id,
                exc_info=True,
            )
            if tenant_id is not None:
                raise PolicyStoreUnavailable(
                    f"Policy authority is unavailable for tenant {tenant_id!r}"
                ) from None
            return False

        if tenant_id is not None:
            initialized = tenant_id in self._initialized_tenants
            synchronized = (
                self._tenant_sync_successes.get(tenant_id, 0)
                > previous_successes
            )
            if not initialized or not synchronized:
                raise PolicyStoreUnavailable(
                    f"Policy authority is unavailable for tenant {tenant_id!r}"
                )
        return refreshed

    async def _refresh(
        self,
        now: float,
        *,
        tenant_id: str | None,
    ) -> bool:
        if tenant_id is None:
            version_reader = getattr(
                self._persistence,
                "get_policy_version",
                None,
            )
            policy_loader = getattr(
                self._persistence,
                "load_all_cedar_policies_or_none",
                None,
            )
            known_version = self._known_version
            reload_generation = self._reload_generation
        else:
            version_reader = getattr(
                self._persistence,
                "get_tenant_cedar_policy_version",
                None,
            )
            policy_loader = getattr(
                self._persistence,
                "load_tenant_cedar_policies_or_none",
                None,
            )
            known_version = self._tenant_known_versions.get(tenant_id)
            reload_generation = self._tenant_reload_generations.get(
                tenant_id,
                0,
            )

        if not callable(version_reader) or not callable(policy_loader):
            logger.error(
                "Tenant-qualified Cedar persistence is unavailable for tenant=%r",
                tenant_id,
            )
            return False

        if tenant_id is None:
            version = await version_reader()
        else:
            version = await version_reader(tenant_id)
        if version is None:
            # Unreadable, or nothing has ever been written through the API. Keep
            # enforcing what we have and retry on the next request rather than
            # advancing the clock — an outage must not buy a full window of
            # divergence, and it must never look like "no policies".
            return False

        initialized = (
            tenant_id is None or tenant_id in self._initialized_tenants
        )
        if version == known_version and initialized:
            if tenant_id is None:
                self._last_version_check = now
            else:
                self._tenant_last_version_checks[tenant_id] = now
                self._tenant_sync_successes[tenant_id] = (
                    self._tenant_sync_successes.get(tenant_id, 0) + 1
                )
            return False

        if tenant_id is None:
            policies = await policy_loader()
        else:
            policies = await policy_loader(tenant_id)
        if policies is None:
            # The version moved but the scan failed. Do NOT adopt an empty set:
            # that would drop every enforced policy fleet-wide because one scan
            # timed out. Leave _known_version alone so the next request retries.
            logger.error(
                "Policy version moved to %s for tenant=%r but the policy scan "
                "failed; continuing to enforce the previously loaded set",
                version,
                tenant_id,
            )
            return False

        try:
            scoped_policies = self._policies_for_scope(
                policies,
                tenant_id=tenant_id,
                allow_unqualified=tenant_id is not None,
            )
        except ValueError:
            logger.error(
                "Tenant-qualified Cedar persistence returned a policy from "
                "another or malformed tenant for tenant=%r; retaining the "
                "previously compiled set",
                tenant_id,
                exc_info=True,
            )
            return False

        # Merge by name over the seeded set, matching bootstrap's merge and
        # POST /admin/policies' update-by-name identity: a stored policy replaces
        # the seeded one it shares a name with, and seeded policies with no stored
        # counterpart survive. Replacing outright would drop them.
        seeded = self._seeded if tenant_id is None else self._tenant_seeded.get(tenant_id, [])
        by_name = {p["name"]: p for p in seeded}
        by_name.update({p["name"]: p for p in scoped_policies})
        merged = list(by_name.values())

        current_version = self._known_version if tenant_id is None else self._tenant_known_versions.get(tenant_id)
        current_generation = (
            self._reload_generation if tenant_id is None else self._tenant_reload_generations.get(tenant_id, 0)
        )
        if current_version != known_version or current_generation != reload_generation:
            logger.info(
                "Discarding stale Cedar refresh for tenant=%r because a newer local policy set was installed",
                tenant_id,
            )
            return False

        logger.info(
            "Adopting fleet policy set for tenant=%r: version %s -> %s (%d stored, %d effective)",
            tenant_id,
            known_version,
            version,
            len(scoped_policies),
            len(merged),
        )
        if tenant_id is None:
            # Mutated in place, not rebound: the legacy AdminAPI holds a
            # reference to this same list.
            self._policies[:] = merged
            self._install_compiled_policy_set(merged, tenant_id=None)
            self._known_version = version
            self._last_version_check = now
        else:
            self._tenant_policies[tenant_id] = merged
            self._install_compiled_policy_set(
                merged,
                tenant_id=tenant_id,
            )
            self._tenant_known_versions[tenant_id] = version
            self._tenant_last_version_checks[tenant_id] = now
            self._initialized_tenants.add(tenant_id)
            self._tenant_sync_successes[tenant_id] = (
                self._tenant_sync_successes.get(tenant_id, 0) + 1
            )
        return True

    def invalidate_scope(self, tenant_id: str | None = None) -> None:
        """Force the next refresh to re-read the durable version for a scope."""
        tenant_id = _normalize_tenant_id(
            tenant_id,
            source="policy invalidation",
        )
        if tenant_id is None:
            self._last_version_check = float("-inf")
        else:
            self._tenant_last_version_checks[tenant_id] = float("-inf")

    @staticmethod
    def _policies_for_scope(
        policies: list[dict],
        *,
        tenant_id: str | None,
        allow_unqualified: bool,
    ) -> list[dict]:
        """Validate and qualify policies received through one durable scope."""
        scoped: list[dict] = []
        for policy in policies:
            declared_tenant = _policy_tenant_id(policy)
            if tenant_id is None:
                if declared_tenant is None:
                    scoped.append(policy)
                continue
            if declared_tenant is not None and declared_tenant != tenant_id:
                raise ValueError("tenant policy loader returned a cross-tenant policy")
            if declared_tenant is None and not allow_unqualified:
                continue
            qualified = dict(policy)
            qualified["tenant_id"] = tenant_id
            scoped.append(qualified)
        return scoped

    def note_local_version(
        self,
        version: int | None,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Record the version this instance just produced by writing a policy.

        Without this the writing instance would see its own bump as a remote
        change on the next poll and re-scan to learn what it already knows.
        """
        tenant_id = _normalize_tenant_id(
            tenant_id,
            source="local policy version",
        )
        if version is not None:
            if tenant_id is None:
                self._known_version = version
                self._last_version_check = time.monotonic()
            else:
                self._tenant_known_versions[tenant_id] = version
                self._tenant_last_version_checks[tenant_id] = time.monotonic()

    def policies_for_scope(
        self,
        tenant_id: str | None = None,
    ) -> list[dict]:
        """Return a snapshot of the effective policies in one tenant scope."""
        tenant_id = _normalize_tenant_id(
            tenant_id,
            source="policy listing",
        )
        policies = (
            self._policies
            if tenant_id is None
            else self._tenant_policies.get(tenant_id, [])
        )
        return [dict(policy) for policy in policies]

    def reload(
        self,
        policies: list[dict],
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Re-parse the policy set, replacing the compiled statements.

        Policies are parsed once rather than per request, so a policy written
        through ``POST /admin/policies`` would otherwise not take effect until a
        restart. The new lists are built first and swapped in at the end: an
        unparseable statement mid-list must not leave a half-built policy set
        deciding requests, and ``evaluate`` may run concurrently.
        """
        tenant_id = _normalize_tenant_id(
            tenant_id,
            source="policy reload",
        )
        if tenant_id is not None:
            scoped = self._policies_for_scope(
                policies,
                tenant_id=tenant_id,
                allow_unqualified=True,
            )
            self._tenant_policies[tenant_id] = scoped
            self._install_compiled_policy_set(
                scoped,
                tenant_id=tenant_id,
            )
            return

        legacy: list[dict] = []
        tenant_policies: dict[str, list[dict]] = {}
        for policy in policies:
            try:
                policy_tenant = _policy_tenant_id(policy)
            except ValueError:
                logger.warning(
                    "Skipping policy %r with an invalid tenant scope",
                    policy.get("name"),
                )
                continue
            if policy_tenant is None:
                legacy.append(policy)
            else:
                tenant_policies.setdefault(policy_tenant, []).append(policy)

        self._install_compiled_policy_set(legacy, tenant_id=None)
        for policy_tenant, scoped in tenant_policies.items():
            self._tenant_policies[policy_tenant] = list(scoped)
            self._install_compiled_policy_set(
                scoped,
                tenant_id=policy_tenant,
            )

    def _install_compiled_policy_set(
        self,
        policies: list[dict],
        *,
        tenant_id: str | None,
    ) -> None:
        """Compile one tenant scope and atomically replace only that scope."""
        statements: list[tuple[_Statement, dict]] = []
        governed: set[str | None] = set()
        for policy in policies:
            text = policy.get("policy_text", "")
            stmt = parse_policy(text)
            if stmt is None:
                logger.warning(
                    "Skipping unparseable/unsupported policy %r for tenant=%r",
                    policy.get("name"),
                    tenant_id,
                )
                continue
            statements.append((stmt, policy))
            if policy.get("mode", "ENFORCE") != "LOG_ONLY":
                governed.add(stmt.action)

        if tenant_id is None:
            self._statements = statements
            self._governed = governed
            self._reload_generation += 1
        else:
            self._tenant_statements[tenant_id] = statements
            self._tenant_governed[tenant_id] = governed
            self._tenant_reload_generations[tenant_id] = self._tenant_reload_generations.get(tenant_id, 0) + 1

    def governs(
        self,
        cedar_action: str,
        context: RequestContext | None = None,
    ) -> bool:
        """Whether any enforcing statement claims authority over this action."""
        try:
            tenant_id = self._context_tenant_id(context)
        except ValueError:
            return True
        governed = self._governed if tenant_id is None else self._tenant_governed.get(tenant_id, set())
        return None in governed or cedar_action in governed

    async def evaluate(self, context: RequestContext, action: str, resource: str) -> str:
        """Return "ALLOW" or "DENY" for the request.

        ``action`` arrives as an HTTP method from the middleware; it is mapped to
        a Cedar action name before matching. Within a governed action: default
        deny, a permit is required, and any matching forbid overrides.
        """
        try:
            tenant_id = self._context_tenant_id(context)
        except ValueError:
            logger.warning(
                "Denying Cedar evaluation for user=%s with malformed tenant_id",
                context.user_id,
            )
            return "DENY"

        # Existing middleware already owns the tenantless compatibility
        # refresh. It cannot pass RequestContext yet, so tenant-aware evaluation
        # refreshes its own canonical scope here.
        if tenant_id is not None:
            await self.refresh_if_stale(context)

        cedar_action = http_method_to_action(action)
        statements = self._statements if tenant_id is None else self._tenant_statements.get(tenant_id, [])
        governed = self._governed if tenant_id is None else self._tenant_governed.get(tenant_id, set())

        permitted = False
        for stmt, policy in statements:
            if not stmt.matches(context, cedar_action):
                continue
            # A LOG_ONLY policy is evaluated for observability but does not
            # affect the effective decision.
            if policy.get("mode", "ENFORCE") == "LOG_ONLY":
                logger.info(
                    "Policy %r (LOG_ONLY) would %s tenant=%r user=%s action=%s resource=%s",
                    policy.get("name"),
                    stmt.effect,
                    tenant_id,
                    context.user_id,
                    cedar_action,
                    resource,
                )
                continue
            if stmt.effect == "forbid":
                return "DENY"  # forbid always wins
            permitted = True

        if permitted:
            return "ALLOW"
        if None in governed or cedar_action in governed:
            return "DENY"  # authored default-deny
        # Nobody wrote a policy about this action; the other authorization
        # layers still apply.
        logger.debug(
            "No enforcing policy governs action=%s; deferring to auth/RBAC/quota (tenant=%r user=%s resource=%s)",
            cedar_action,
            tenant_id,
            context.user_id,
            resource,
        )
        return "ALLOW"
