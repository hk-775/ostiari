"""Admin API endpoints for the LLM-Router service.

Provides Starlette routes for:
- Overview dashboard (total requests, cost, active projects, active users)
- Project CRUD with hot-reload
- Aggregated usage queries with filters
- Cedar policy management
- Health status for providers and runtime
- Admin dashboard SPA
"""

from __future__ import annotations

import asyncio
import csv
import heapq
import io
import logging
import pathlib
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.routing import Route

_STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent

# What ``AdminAPI.site_asset`` will serve out of site/, and as what. Anything
# else 404s — notably site/infra/, which is the CDK app rather than a public
# asset. The auth middleware reads these suffixes to decide the same set of
# paths is anonymous, so the two can't drift into a page that renders with a
# 401 on the SVG it needs.
SITE_ASSET_TYPES = {
    ".html": "text/html",
    # The request-flow player is shared by the landing and architecture pages.
    # Keeping it as a static pair avoids copying scenario data between two flat
    # HTML files while preserving the no-build-step site deployment.
    ".css": "text/css",
    ".js": "text/javascript",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".drawio": "application/xml",
    # The product demo the landing page's ribbon opens. Served out of site/
    # rather than embedded in the page so the gateway and the S3 bucket hand out
    # the same one asset, and so a visitor who never opens it never downloads it.
    ".mp4": "video/mp4",
    # Its captions. A <track> whose src 404s fails silently — the video plays
    # with the CC button doing nothing — so this belongs next to the .mp4 rather
    # than being noticed later by someone watching muted.
    ".vtt": "text/vtt",
    # The architecture walkthrough: the MP3s Polly generated and the JSON the
    # page reads its transcript and durations from.
    ".mp3": "audio/mpeg",
    ".json": "application/json",
}

# Subdirectories of site/ that ``site_asset`` will serve into, named rather than
# reached by relaxing the depth check: site/infra/ is the CDK app, and any rule
# phrased as "one level down is fine" would have handed it out.
SITE_ASSET_DIRS = frozenset({"narration"})


def _parse_byte_range(header: str | None, size: int) -> tuple[int | None, int | None]:
    """Parse a Range header into inclusive (start, end), or (None, None).

    Returns (None, None) for anything not understood, which the caller answers
    with a plain 200 — the spec's own advice, and the behaviour a media element
    copes with. Only a single ``bytes=`` range is handled: multipart/byteranges
    exists for image tiles and PDF viewers, and no browser asks for it when
    seeking audio.

    A range that starts past the end is also treated as unparseable rather than
    answered with 416. The caller holds the whole file in memory, so the honest
    response to "give me byte 900000 of an 800000-byte file" is the file.
    """
    if not header or not header.startswith("bytes=") or size == 0:
        return None, None
    spec = header[6:].strip()
    if "," in spec:
        return None, None
    first, _, last = spec.partition("-")
    try:
        if not first:
            # "bytes=-500" — the trailing N bytes.
            if not last:
                return None, None
            start, end = max(0, size - int(last)), size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return None, None
    end = min(end, size - 1)
    if start > end or start >= size or start < 0:
        return None, None
    return start, end


def _is_servable_site_path(inside: pathlib.PurePath) -> bool:
    """Whether ``inside`` — a path already proven to be under site/ — is public.

    Shared with the auth middleware, which must exempt exactly the set this
    admits. Written against a relative path rather than a URL so the traversal
    check stays where it belongs: the caller resolves first, this only decides.
    """
    if inside.suffix.lower() not in SITE_ASSET_TYPES:
        return False
    if len(inside.parts) == 1:
        return True
    return len(inside.parts) == 2 and inside.parts[0] in SITE_ASSET_DIRS


from src.gateway.admin.catalog_drift import audit_catalog, render_catalog_drift_page
from src.gateway.admin.pricing_drift import audit_pricing, render_drift_page
from src.gateway.admin.production_checklist import render_checklist_page, run_checklist
from src.gateway.auth.cedar_policy import CedarPolicyService, parse_policy
from src.gateway.config import AppConfig
from src.gateway.cost_tracker import _EPOCH, CostTracker
from src.gateway.efficiency_analyzer import EfficiencyAnalyzer
from src.gateway.export_jobs import (
    ExportJobError,
    ExportJobNotFound,
    ExportJobNotReady,
    ExportJobService,
    ExportKind,
    export_job_public,
    request_export_identity,
)
from src.gateway.health_tracker import ProviderHealthTracker
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import GuardrailRule, Project, UsageFilters
from src.gateway.persistence import (
    CanonicalMembershipConflictError,
    CanonicalMembershipNotFoundError,
    PersistenceConflictError,
)
from src.gateway.provider_config import ProviderConfig
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.security.audit_trail import LEGACY_TENANT_ID
from src.gateway.semantic_efficiency import SemanticEfficiencyEngine
from .page_style import (
    BASE_STYLE,
    EMBED_STYLE as PAGE_EMBED_STYLE,
    FAVICON as PAGE_FAVICON,
    SURFACE as PAGE_SURFACE,
    ribbon as page_ribbon,
)

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)


def _request_tenant_id(request: Request) -> str | None:
    if request is None:
        return None
    state = getattr(request, "state", None)
    context = getattr(state, "context", None)
    tenant_id = getattr(context, "tenant_id", None)
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


def _user_config_key(
    tenant_id: str | None,
    user_id: str,
) -> str:
    if tenant_id is None:
        return user_id
    return (
        f"tenant:{len(tenant_id)}:{tenant_id}:"
        f"user:{len(user_id)}:{user_id}"
    )


def _revision_headers(revision: int) -> dict[str, str]:
    return {"ETag": f'"{revision}"'}


def _parse_if_match_revision(
    request: Request,
    current_revision: int,
) -> tuple[int | None, JSONResponse | None]:
    """Return the requested CAS revision, defaulting to the loaded revision."""
    headers = getattr(request, "headers", {})
    raw = headers.get("if-match") if headers is not None else None
    if raw is None or raw.strip() == "*":
        return current_revision, None

    value = raw.strip()
    if (
        "," in value
        or value.startswith("W/")
        or (value.startswith('"') != value.endswith('"'))
    ):
        return None, JSONResponse(
            {
                "error": {
                    "type": "invalid_request",
                    "code": "invalid_revision",
                    "message": "If-Match must contain one strong revision ETag",
                }
            },
            status_code=400,
        )
    if value.startswith('"'):
        value = value[1:-1]
    try:
        revision = int(value)
    except (TypeError, ValueError):
        revision = -1
    if revision < 0:
        return None, JSONResponse(
            {
                "error": {
                    "type": "invalid_request",
                    "code": "invalid_revision",
                    "message": "If-Match revision must be a non-negative integer",
                }
            },
            status_code=400,
        )
    return revision, None


def _staged_project(project: Project, **changes) -> Project:
    """Detach every mutable project field before applying a candidate change."""
    detached = {
        "allowed_models": deepcopy(project.allowed_models),
        "guardrail_rules": deepcopy(project.guardrail_rules),
        "members": deepcopy(project.members),
    }
    detached.update(changes)
    return replace(project, **detached)


def _next_revision(value: object, expected: int, resource: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value != expected + 1
    ):
        raise RuntimeError(
            f"{resource} persistence returned an invalid revision"
        )
    return value


def _parse_semantic_threshold(raw) -> tuple[float | None, str | None]:
    """Validate a semantic_cache_threshold from a request body.

    Returns ``(value, error)``; exactly one is non-None, except for a valid
    ``null`` which returns ``(None, None)`` and means "use the gateway default".

    Validated rather than trusted because the damaging value here is a
    plausible-looking one. 0, a negative number, or a percentage typed as 95
    all make every cached entry a match, so the project starts answering
    unrelated questions with whatever it cached first — which presents as a
    working cache with a suspiciously good hit rate, not as an error.
    """
    if raw is None:
        return None, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, (
            "semantic_cache_threshold must be a number in (0.0, 1.0], "
            "or null for the default"
        )
    if not 0.0 < value <= 1.0:
        return None, (
            f"semantic_cache_threshold {value} is outside (0.0, 1.0]; "
            "null selects the default"
        )
    return value, None


PROVIDER_MODEL_CATALOG = {
    "openai": {
        "display_name": "OpenAI",
        "auth_type": "api_key",
        "models": [
            {"model_id": "gpt-4o", "name": "GPT-4o", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gpt-4o-mini", "name": "GPT-4o Mini", "capabilities": ["chat", "streaming"]},
            {"model_id": "gpt-4-turbo", "name": "GPT-4 Turbo", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "o3", "name": "o3 (Reasoning)", "capabilities": ["chat", "reasoning"]},
            {"model_id": "o4-mini", "name": "o4 Mini (Reasoning)", "capabilities": ["chat", "reasoning"]},
        ],
    },
    "anthropic": {
        "display_name": "Anthropic",
        "auth_type": "api_key",
        "models": [
            {"model_id": "claude-opus-4-20250514", "name": "Claude Opus 4", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku", "capabilities": ["chat", "streaming"]},
        ],
    },
    "bedrock": {
        "display_name": "AWS Bedrock",
        "auth_type": "aws_credentials",
        "models": [
            {"model_id": "us.anthropic.claude-opus-4-6-v1", "name": "Claude Opus 4.6", "capabilities": ["chat", "vision"]},
            {"model_id": "us.anthropic.claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "capabilities": ["chat", "vision"]},
            {"model_id": "us.amazon.nova-pro-v1:0", "name": "Amazon Nova Pro", "capabilities": ["chat", "vision"]},
            {"model_id": "us.amazon.nova-lite-v1:0", "name": "Amazon Nova Lite", "capabilities": ["chat"]},
            {"model_id": "us.amazon.nova-micro-v1:0", "name": "Amazon Nova Micro", "capabilities": ["chat"]},
            {"model_id": "us.deepseek.r1-v1:0", "name": "DeepSeek R1", "capabilities": ["chat", "reasoning"]},
            {"model_id": "ai21.jamba-1-5-large-v1:0", "name": "AI21 Jamba 1.5 Large", "capabilities": ["chat"]},
            {"model_id": "ai21.jamba-1-5-mini-v1:0", "name": "AI21 Jamba 1.5 Mini", "capabilities": ["chat"]},
        ],
    },
    "azure_openai": {
        "display_name": "Azure OpenAI",
        "auth_type": "azure_key",
        "models": [
            {"model_id": "gpt-4o", "name": "GPT-4o", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gpt-4o-mini", "name": "GPT-4o Mini", "capabilities": ["chat", "streaming"]},
            {"model_id": "gpt-4-turbo", "name": "GPT-4 Turbo", "capabilities": ["chat", "vision", "streaming"]},
        ],
    },
    "vertex_ai": {
        "display_name": "Google Vertex AI",
        "auth_type": "gcp_service_account",
        "models": [
            {"model_id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "capabilities": ["chat", "vision", "streaming"]},
            {"model_id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash", "capabilities": ["chat", "vision", "streaming"]},
        ],
    },
}


class AdminAPI:
    """Holds references to gateway components and exposes admin route handlers."""

    def __init__(
        self,
        cost_tracker: CostTracker,
        health_tracker: ProviderHealthTracker,
        model_registry: ModelRegistry,
        projects: dict[str, Project] | None = None,
        policies: list[dict] | None = None,
        user_configs: dict[str, dict] | None = None,
        config_path: str = "config/models.yaml",
        persistence: DynamoPersistence | None = None,
        catalog: dict | None = None,
        efficiency_analyzer: EfficiencyAnalyzer | None = None,
        semantic_engine: SemanticEfficiencyEngine | None = None,
        pricing_path: str = "config/pricing.yaml",
        catalog_path: str = "config/catalog.yaml",
        app_config: AppConfig | None = None,
        provider_configs: dict[str, ProviderConfig] | None = None,
        api_key_service: object | None = None,
        semantic_cache: object | None = None,
        policy_service: CedarPolicyService | None = None,
        config_sync: object | None = None,
        export_jobs: ExportJobService | None = None,
    ) -> None:
        self.cost_tracker = cost_tracker
        self.health_tracker = health_tracker
        self.model_registry = model_registry
        # `x if x is not None`, not `x or {}`: an empty container is falsy, so the
        # latter quietly substituted a *different* object whenever the caller
        # passed an empty one — which is every gateway that boots without demo
        # seed data, i.e. the production path. These three are all shared with
        # something else (the projects dict with GatewayAgent, the policy list
        # with CedarPolicyService, user_configs with the agent), so a broken
        # reference means an admin write updates a copy nobody reads: a project
        # created through the API stayed invisible to the request path until
        # restart.
        self.projects: dict[str, Project] = projects if projects is not None else {}
        self.policies: list[dict] = policies if policies is not None else []
        # The live evaluator, so POST /admin/policies can recompile it. None when
        # no Cedar service is wired (auth middleware built without one).
        self._policy_service = policy_service
        # The fleet config sync, so a write here records the version it produced
        # rather than rediscovering its own change on the next poll, and so the
        # list endpoints can converge before answering. None when not wired.
        self._config_sync = config_sync
        self._user_configs: dict[str, dict] = (
            user_configs if user_configs is not None else {})
        self._config_path = config_path
        self._persistence = persistence
        self._catalog = catalog if catalog is not None else PROVIDER_MODEL_CATALOG
        self._efficiency_analyzer = efficiency_analyzer
        self._semantic_engine = semantic_engine
        # Shown on the pricing-coverage page so the operator knows which file to
        # edit; the table itself comes from the cost tracker, already loaded.
        self._pricing_path = pricing_path
        # Same idea for the catalogue-coverage page. Both files are named on the
        # page rather than assumed, because the drift it reports is fixed by
        # editing one of the two and it is not obvious from a finding which.
        self._catalog_path = catalog_path
        # For the production checklist. The typed AppConfig is threaded through
        # rather than read from the environment in the render path, so the page
        # reports the settings this process actually booted with — a later
        # os.environ mutation cannot make the checklist disagree with the running
        # gateway. Defaults to a fresh AppConfig so existing callers keep working;
        # that carries the fail-closed defaults (ENFORCE, no demo data), which is
        # the safe direction for a checklist to assume.
        self._app_config = app_config if app_config is not None else AppConfig()
        # Providers with credentials actually loaded. load_provider_configs drops
        # the rest, so this doubles as the credential check's input.
        self._provider_configs: dict[str, ProviderConfig] = provider_configs or {}
        # Read-only, and only by the production checklist, to report the scopes
        # and expiry of issued keys. Optional: when absent that check reports
        # UNKNOWN rather than passing, since unverified is not the same as clean.
        self._api_key_service = api_key_service
        # The same instance the gateway agent holds, so the stats reported are
        # the live counters rather than a second cache's. None when the caller
        # did not wire one; the endpoint then reports it as unavailable, which is
        # distinguishable from a wired cache with no hits.
        self._semantic_cache = semantic_cache
        self._export_jobs = export_jobs

    # ------------------------------------------------------------------
    # Fleet-wide read helpers
    # ------------------------------------------------------------------

    async def _synced_records(self) -> list:
        """The usage records for an aggregate, refreshed at most once per TTL.

        Every aggregate below sums the cost tracker's record list, which holds
        only what this process served. Behind a load balancer that makes each
        answer a function of which task replied. This funnels those reads through
        one refresh so they describe the fleet instead.

        The refresh itself lives on ``CostTracker`` because ``GET /api/users``
        needs the same thing, and two copies would mean two clocks: the chat
        selector and the dashboard could then refresh on different beats and
        disagree about who exists, and a burst across both would issue two scans
        where one would do. See ``CostTracker.USAGE_SYNC_TTL_SECONDS`` for why it
        is rate-limited at all.
        """
        return await self.cost_tracker.synced_records()

    async def _tenant_records(self, request: Request) -> list:
        """Return the synced usage view constrained to the caller's tenant."""
        tenant_id = _request_tenant_id(request)
        records = await self._synced_records()
        if tenant_id is None:
            return records
        return [
            record
            for record in records
            if record.tenant_id == tenant_id
        ]

    async def _fleet_spend(
        self,
        scope: str,
        ident: str,
        local: float,
        *,
        tenant_id: str | None = None,
    ) -> float:
        """Exact fleet-wide spend, falling back to the local figure.

        Kept separate from ``_synced_records`` because money has a cheaper and
        better source than the record list: a shared counter read per call, which
        is neither TTL-stale nor subject to record trimming. Where a page shows
        both, the cost is exact and the counts are up to one TTL behind.
        """
        total = await self.cost_tracker.fleet_spend(
            scope,
            ident,
            tenant_id=tenant_id,
        )
        return local if total is None else total

    async def _note_config_write(self) -> None:
        """Invalidate any in-flight snapshot after a local config commit."""
        if self._config_sync is None:
            return
        invalidate = getattr(
            self._config_sync,
            "invalidate_local_config",
            None,
        )
        if callable(invalidate):
            invalidate()

    async def _refresh_config(self) -> None:
        """Converge on the fleet's config before listing or reading it.

        The list endpoints have no id to read through on, so unlike
        ``_get_project`` they cannot recover from a miss — an empty local dict is
        indistinguishable from an empty table. This is also what arms enforcement
        for limits another instance set, so a read here is not merely cosmetic.
        """
        if self._config_sync is None:
            return
        try:
            await self._config_sync.refresh_if_stale()
        except Exception:
            logger.warning(
                "Config refresh failed; answering from this instance's config",
                exc_info=True,
            )

    async def _get_user_config(
        self,
        user_id: str,
        tenant_id: str | None,
    ) -> dict:
        """Read a user config without falling across tenant namespaces."""
        key = _user_config_key(tenant_id, user_id)
        if tenant_id is None:
            return self._user_configs.get(key, {})
        if self._persistence is None or not self._persistence.enabled:
            return self._user_configs.get(key, {})
        loader = getattr(
            self._persistence,
            "get_tenant_user_config",
            None,
        )
        if not callable(loader):
            raise RuntimeError("tenant user config loading is unavailable")
        config = await loader(tenant_id, user_id)
        if config is None:
            self._user_configs.pop(key, None)
            return {}
        self._user_configs[key] = config
        return config

    async def _save_user_config(
        self,
        user_id: str,
        tenant_id: str | None,
        config: dict,
        *,
        expected_revision: int,
    ) -> dict:
        """Commit one detached config, then publish its new revision."""
        key = _user_config_key(tenant_id, user_id)
        staged = deepcopy(config)
        if self._persistence is not None and self._persistence.enabled:
            if tenant_id is None:
                revision = await self._persistence.save_user_config(
                    user_id,
                    staged,
                    expected_revision=expected_revision,
                )
            else:
                saver = getattr(
                    self._persistence,
                    "save_tenant_user_config",
                    None,
                )
                if not callable(saver):
                    raise RuntimeError(
                        "tenant user config persistence is unavailable"
                    )
                revision = await saver(
                    tenant_id,
                    user_id,
                    staged,
                    expected_revision=expected_revision,
                )
            revision = _next_revision(
                revision,
                expected_revision,
                "user config",
            )
            await self._note_config_write()
        else:
            revision = expected_revision + 1
        committed = {**staged, "revision": revision}
        self._user_configs[key] = committed
        return committed

    async def _reload_user_config(
        self,
        user_id: str,
        tenant_id: str | None,
    ) -> dict | None:
        """Adopt the authoritative config after rejecting a stale writer."""
        if self._persistence is None or not self._persistence.enabled:
            return None
        key = _user_config_key(tenant_id, user_id)
        try:
            if tenant_id is not None:
                loader = getattr(
                    self._persistence,
                    "get_tenant_user_config",
                    None,
                )
                if not callable(loader):
                    return None
                config = await loader(tenant_id, user_id)
            else:
                loader = getattr(
                    self._persistence,
                    "load_user_configs_or_none",
                    None,
                )
                if callable(loader):
                    configs = await loader()
                else:
                    configs = await self._persistence.load_user_configs()
                if configs is None:
                    return None
                config = configs.get(user_id)
        except Exception:
            logger.warning(
                "Failed to reload user config for %s after a conflict",
                user_id,
                exc_info=True,
            )
            return None

        if config is None:
            self._user_configs.pop(key, None)
            return None
        committed = deepcopy(config)
        self._user_configs[key] = committed
        self.cost_tracker.register_user(
            user_id,
            budget_limit=committed.get("budget_limit"),
            alert_threshold=committed.get("alert_threshold"),
            tenant_id=tenant_id,
        )
        return committed

    @staticmethod
    def _user_config_write_conflict(
        revision: int | None = None,
    ) -> JSONResponse:
        error: dict[str, object] = {
            "type": "write_conflict",
            "code": "user_config_write_conflict",
            "message": "User configuration changed concurrently; reload and retry",
        }
        headers = None
        if revision is not None:
            error["revision"] = revision
            headers = _revision_headers(revision)
        return JSONResponse(
            {"error": error},
            status_code=409,
            headers=headers,
        )

    @staticmethod
    def _user_config_store_unavailable() -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "type": "service_unavailable",
                    "message": "User configuration persistence is unavailable",
                }
            },
            status_code=503,
        )

    # ------------------------------------------------------------------
    # GET /admin/overview
    # ------------------------------------------------------------------

    async def overview(self, request: Request) -> JSONResponse:
        """Total requests, cost, active projects, and active users."""
        tenant_id = _request_tenant_id(request)
        records = [
            record
            for record in await self._synced_records()
            if tenant_id is None or record.tenant_id == tenant_id
        ]
        total_requests = len(records)
        total_cost = sum(r.cost for r in records)
        active_projects = len({r.project_id for r in records})
        active_users = len({r.user_id for r in records})

        total_cached_tokens = sum(r.cached_tokens for r in records)
        total_cache_creation_tokens = sum(r.cache_creation_tokens for r in records)
        denom = total_cached_tokens + total_cache_creation_tokens
        cache_hit_rate = total_cached_tokens / denom if denom > 0 else 0.0

        return JSONResponse({
            "total_requests": total_requests,
            "total_cost": total_cost,
            "active_projects": active_projects,
            "active_users": active_users,
            "total_cached_tokens": total_cached_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "cache_hit_rate": cache_hit_rate,
        })

    # ------------------------------------------------------------------
    # GET /admin/projects
    # ------------------------------------------------------------------

    async def list_projects(self, request: Request) -> JSONResponse:
        """List all projects with spend, budget utilization, and request counts."""
        tenant_id = _request_tenant_id(request)
        result = []
        # One refresh for the whole list, not one per project: the scan returns
        # every project's records at once, so filtering the synced list in the
        # loop costs nothing extra.
        synced = [
            record
            for record in await self._synced_records()
            if tenant_id is None or record.tenant_id == tenant_id
        ]
        projects = list((await self._all_projects(tenant_id)).values())

        # Counter reads concurrently rather than one per iteration. Each is a
        # separate GetItem in a thread, so sequentially they add up: 300 projects
        # at ~8ms is nearly 3s of an operator staring at a spinner, for reads that
        # do not depend on each other.
        statuses = await asyncio.gather(*(
            self.cost_tracker.check_budget(
                p.project_id,
                tenant_id=p.tenant_id,
            )
            for p in projects
        ))
        spends = await asyncio.gather(*(
            self._fleet_spend(
                "project",
                p.project_id,
                s.current_spend,
                tenant_id=p.tenant_id,
            )
            for p, s in zip(projects, statuses)
        ))

        for project, current_spend in zip(projects, spends):
            records = [
                r for r in synced
                if r.project_id == project.project_id
            ]
            utilization = (
                (current_spend / project.budget_limit * 100)
                if project.budget_limit
                else None
            )
            result.append({
                "project_id": project.project_id,
                "name": project.name,
                "revision": project.revision,
                "current_spend": current_spend,
                "budget_limit": project.budget_limit,
                "budget_utilization_pct": utilization,
                "request_count": len(records),
            })
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/projects/{id}
    # ------------------------------------------------------------------

    async def get_project(self, request: Request) -> JSONResponse:
        """Project detail with users, usage breakdown, and config."""
        project_id = request.path_params["id"]
        tenant_id = _request_tenant_id(request)
        project = await self._get_project(project_id, tenant_id)
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        records = [
            r for r in await self._synced_records()
            if r.project_id == project_id
            and (tenant_id is None or r.tenant_id == tenant_id)
        ]
        users = list({r.user_id for r in records})

        # Usage breakdown by model
        model_breakdown: dict[str, dict] = {}
        for r in records:
            entry = model_breakdown.setdefault(r.model, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        # Usage breakdown by provider
        provider_breakdown: dict[str, dict] = {}
        for r in records:
            entry = provider_breakdown.setdefault(r.provider, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        # Usage breakdown by user
        user_breakdown: dict[str, dict] = {}
        for r in records:
            entry = user_breakdown.setdefault(r.user_id, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        # Cached token metrics for this project
        total_cached_tokens = sum(r.cached_tokens for r in records)
        total_cache_creation_tokens = sum(r.cache_creation_tokens for r in records)
        denom = total_cached_tokens + total_cache_creation_tokens
        cache_hit_rate = total_cached_tokens / denom if denom > 0 else 0.0

        return JSONResponse({
            "project_id": project.project_id,
            "name": project.name,
            "revision": project.revision,
            "budget_limit": project.budget_limit,
            "alert_threshold": project.alert_threshold,
            "allowed_models": project.allowed_models,
            "guardrail_rules": [
                {"name": g.name, "rule_type": g.rule_type, "pattern": g.pattern, "action": g.action, "applies_to": g.applies_to}
                for g in project.guardrail_rules
            ],
            "cache_enabled": project.cache_enabled,
            "cache_ttl_seconds": project.cache_ttl_seconds,
            "semantic_cache_enabled": project.semantic_cache_enabled,
            "semantic_cache_threshold": project.semantic_cache_threshold,
            "log_level": project.log_level,
            "prompt_caching_enabled": project.prompt_caching_enabled,
            "users": users,
            "members": project.members,
            "usage_by_model": model_breakdown,
            "usage_by_provider": provider_breakdown,
            "usage_by_user": user_breakdown,
            "total_cached_tokens": total_cached_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "cache_hit_rate": cache_hit_rate,
        }, headers=_revision_headers(project.revision))

    # ------------------------------------------------------------------
    # POST /admin/projects
    # ------------------------------------------------------------------

    async def create_project(self, request: Request) -> JSONResponse:
        """Create a new project from JSON body."""
        body = await request.json()

        project_id = body.get("project_id", str(uuid.uuid4()))
        tenant_id = _request_tenant_id(request)
        if (
            tenant_id is not None
            and self._app_config.canonical_identity_required
            and body.get("members")
        ):
            return JSONResponse(
                {
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            "Canonical project members must be granted "
                            "through the project member endpoint"
                        ),
                    }
                },
                status_code=400,
            )
        name = body.get("name")
        if not name:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'name' is required"}},
                status_code=400,
            )

        guardrail_rules = [
            GuardrailRule(
                name=g["name"],
                rule_type=g.get("rule_type", "keyword_block"),
                pattern=g.get("pattern"),
                action=g.get("action", "block"),
                applies_to=g.get("applies_to", "both"),
            )
            for g in body.get("guardrail_rules", [])
        ]

        semantic_threshold, threshold_err = _parse_semantic_threshold(
            body.get("semantic_cache_threshold")
        )
        if threshold_err is not None:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": threshold_err}},
                status_code=400,
            )

        project = Project(
            project_id=project_id,
            name=name,
            tenant_id=tenant_id,
            budget_limit=body.get("budget_limit"),
            alert_threshold=body.get("alert_threshold"),
            allowed_models=body.get("allowed_models"),
            guardrail_rules=guardrail_rules,
            cache_enabled=body.get("cache_enabled", False),
            cache_ttl_seconds=body.get("cache_ttl_seconds", 300),
            semantic_cache_enabled=body.get("semantic_cache_enabled", False),
            semantic_cache_threshold=semantic_threshold,
            log_level=body.get("log_level", "INFO"),
            log_destination=body.get("log_destination"),
            prompt_caching_enabled=body.get("prompt_caching_enabled", False),
            rate_limit_rpm=body.get("rate_limit_rpm"),
            members=body.get("members", []),
        )

        revision = 1
        if self._persistence is not None and self._persistence.enabled:
            try:
                create = getattr(self._persistence, "create_project", None)
                if callable(create):
                    revision = await create(project)
                else:
                    revision = await self._persistence.save_project(
                        project,
                        expected_revision=0,
                    )
                revision = _next_revision(revision, 0, "project")
                await self._note_config_write()
            except (PersistenceConflictError, ValueError):
                latest = await self._reload_project(project_id, tenant_id)
                return self._project_write_conflict(
                    latest.revision if latest is not None else None
                )
            except Exception:
                logger.warning(
                    "Failed to persist project %s to DynamoDB",
                    project_id,
                    exc_info=True,
                )
                return JSONResponse(
                    {
                        "error": {
                            "type": "service_unavailable",
                            "message": "Project persistence is unavailable",
                        }
                    },
                    status_code=503,
                )

        project = replace(project, revision=revision)
        if tenant_id is None:
            self.projects[project_id] = project

        # Register budget with cost tracker if configured
        if project.budget_limit is not None or project.alert_threshold is not None:
            self.cost_tracker.register_project(
                project_id,
                budget_limit=project.budget_limit,
                alert_threshold=project.alert_threshold,
                tenant_id=tenant_id,
            )

        return JSONResponse(
            {
                "project_id": project_id,
                "name": project.name,
                "revision": project.revision,
                "status": "created",
            },
            status_code=201,
            headers=_revision_headers(project.revision),
        )

    # ------------------------------------------------------------------
    # PUT /admin/projects/{id}
    # ------------------------------------------------------------------

    async def update_project(self, request: Request) -> JSONResponse:
        """Update project config (hot-reload, no restart)."""
        project_id = request.path_params["id"]
        project = await self._get_project(
            project_id,
            _request_tenant_id(request),
        )
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        body = await request.json()
        if (
            project.tenant_id is not None
            and self._app_config.canonical_identity_required
            and "members" in body
        ):
            return JSONResponse(
                {
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            "Canonical project members must be changed "
                            "through the project member endpoint"
                        ),
                    }
                },
                status_code=400,
            )

        expected_revision, error = _parse_if_match_revision(
            request,
            project.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        if expected_revision != project.revision:
            latest = await self._reload_project(
                project_id,
                project.tenant_id,
            )
            return self._project_write_conflict(
                latest.revision if latest is not None else None
            )

        updates: dict[str, object] = {}
        if "name" in body:
            updates["name"] = body["name"]
        if "budget_limit" in body:
            updates["budget_limit"] = body["budget_limit"]
        if "alert_threshold" in body:
            updates["alert_threshold"] = body["alert_threshold"]
        if "allowed_models" in body:
            updates["allowed_models"] = deepcopy(body["allowed_models"])
        if "cache_enabled" in body:
            updates["cache_enabled"] = body["cache_enabled"]
        if "cache_ttl_seconds" in body:
            updates["cache_ttl_seconds"] = body["cache_ttl_seconds"]
        if "semantic_cache_enabled" in body:
            updates["semantic_cache_enabled"] = body[
                "semantic_cache_enabled"
            ]
        if "semantic_cache_threshold" in body:
            value, err = _parse_semantic_threshold(body["semantic_cache_threshold"])
            if err is not None:
                return JSONResponse(
                    {"error": {"type": "invalid_request", "message": err}},
                    status_code=400,
                )
            updates["semantic_cache_threshold"] = value
        if "log_level" in body:
            updates["log_level"] = body["log_level"]
        if "log_destination" in body:
            updates["log_destination"] = body["log_destination"]
        if "rate_limit_rpm" in body:
            updates["rate_limit_rpm"] = body["rate_limit_rpm"]
        if "members" in body:
            updates["members"] = deepcopy(body["members"])
        if "prompt_caching_enabled" in body:
            updates["prompt_caching_enabled"] = body[
                "prompt_caching_enabled"
            ]
        if "guardrail_rules" in body:
            updates["guardrail_rules"] = [
                GuardrailRule(
                    name=g["name"],
                    rule_type=g.get("rule_type", "keyword_block"),
                    pattern=g.get("pattern"),
                    action=g.get("action", "block"),
                    applies_to=g.get("applies_to", "both"),
                )
                for g in body["guardrail_rules"]
            ]
        staged = _staged_project(project, **updates)
        committed, error = await self._commit_project(
            staged,
            expected_revision=expected_revision,
        )
        if error is not None:
            return error
        assert committed is not None
        return JSONResponse(
            {
                "project_id": project_id,
                "revision": committed.revision,
                "status": "updated",
            },
            headers=_revision_headers(committed.revision),
        )

    # ------------------------------------------------------------------
    # POST /admin/projects/{id}/members
    # ------------------------------------------------------------------

    async def _get_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ):
        """Resolve a project by id, reading through to DynamoDB on a miss.

        ``self.projects`` is hydrated once at startup and is per-process, so with
        more than one instance behind a load balancer — which is the shipped
        default, ``desired_count=2`` in ``infra/stack.py``, auto-scaling to 10 —
        a project created through ``POST /admin/projects`` on one task is
        invisible to every other task until it restarts. Verified against a live
        two-task deployment: ten identical authenticated ``GET`` requests
        returned the project six times and ``[]`` four times, decided purely by
        which task the ALB picked.

        The read-through mirrors what ``APIKeyService`` already does for keys,
        which is why authentication never exhibited this bug while project reads
        did.

        Legacy projects are cached in ``self.projects``. Writers stage detached
        copies and replace that entry only after their conditional write commits.
        """
        if tenant_id is None:
            project = self.projects.get(project_id)
            if project is not None:
                return project
        if self._persistence is None or not self._persistence.enabled:
            return None
        if tenant_id is None:
            project = await self._persistence.get_project(project_id)
        else:
            project = await self._persistence.get_project(
                project_id,
                tenant_id,
            )
        if project is not None and tenant_id is None:
            self.projects[project_id] = project
        return project

    async def _all_projects(
        self,
        tenant_id: str | None = None,
    ) -> dict:
        """Every project, including ones created or changed by another instance.

        The list endpoint cannot read through on a miss the way ``_get_project``
        does — there is no id to miss on, and an empty local dict is
        indistinguishable from an empty table.

        Prefers the config sync where one is wired. That is strictly better than
        the scan below on three counts: it is version-gated rather than
        per-request, it adopts *updates* and not just additions, and it arms
        enforcement for the limits it adopts. The scan remains as the fallback for
        callers that construct ``AdminAPI`` without a sync, where the old
        behaviour is better than none.

        In the fallback, locally-known objects win over the scanned copy. The
        fleet sync uses the shared version signal to adopt later revisions.
        """
        if tenant_id is not None:
            if self._persistence is None or not self._persistence.enabled:
                return {
                    project.project_id: project
                    for project in self.projects.values()
                    if project.tenant_id == tenant_id
                }
            list_tenant_projects = getattr(
                self._persistence,
                "list_tenant_projects",
                None,
            )
            if not callable(list_tenant_projects):
                return {}
            projects = await list_tenant_projects(tenant_id)
            return {
                project.project_id: project
                for project in projects
            }
        if self._config_sync is not None:
            await self._refresh_config()
            return self.projects
        if self._persistence is None or not self._persistence.enabled:
            return self.projects
        try:
            stored = await self._persistence.load_projects()
        except Exception:
            logger.warning("Failed to list projects from DynamoDB", exc_info=True)
            return self.projects
        for project_id, project in stored.items():
            self.projects.setdefault(project_id, project)
        return self.projects

    @staticmethod
    def _project_store_unavailable() -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "type": "service_unavailable",
                    "message": "Project persistence is unavailable",
                }
            },
            status_code=503,
        )

    @staticmethod
    def _project_write_conflict(
        revision: int | None = None,
    ) -> JSONResponse:
        error: dict[str, object] = {
            "type": "write_conflict",
            "code": "project_write_conflict",
            "message": "Project changed concurrently; reload and retry",
        }
        headers = None
        if revision is not None:
            error["revision"] = revision
            headers = _revision_headers(revision)
        return JSONResponse(
            {"error": error},
            status_code=409,
            headers=headers,
        )

    def _publish_project(self, project: Project) -> None:
        """Replace shared legacy state only after a durable commit."""
        if project.tenant_id is None:
            self.projects[project.project_id] = project

    def _register_project_budget(self, project: Project) -> None:
        self.cost_tracker.register_project(
            project.project_id,
            budget_limit=project.budget_limit,
            alert_threshold=project.alert_threshold,
            tenant_id=project.tenant_id,
        )

    async def _reload_project(
        self,
        project_id: str,
        tenant_id: str | None,
    ) -> Project | None:
        """Adopt the authoritative project after rejecting a stale writer."""
        if self._persistence is None or not self._persistence.enabled:
            return None
        try:
            if tenant_id is None:
                project = await self._persistence.get_project(project_id)
            else:
                project = await self._persistence.get_project(
                    project_id,
                    tenant_id,
                )
        except Exception:
            logger.warning(
                "Failed to reload project %s after a conflict",
                project_id,
                exc_info=True,
            )
            return None
        if project is not None:
            self._publish_project(project)
            self._register_project_budget(project)
        return project

    async def _commit_project(
        self,
        staged: Project,
        *,
        expected_revision: int,
    ) -> tuple[Project | None, JSONResponse | None]:
        """CAS a detached candidate, then publish and arm its budget."""
        if self._persistence is not None and self._persistence.enabled:
            try:
                revision = await self._persistence.save_project(
                    staged,
                    expected_revision=expected_revision,
                )
                revision = _next_revision(
                    revision,
                    expected_revision,
                    "project",
                )
                await self._note_config_write()
            except PersistenceConflictError:
                latest = await self._reload_project(
                    staged.project_id,
                    staged.tenant_id,
                )
                return None, self._project_write_conflict(
                    latest.revision if latest is not None else None
                )
            except Exception:
                logger.warning(
                    "Failed to persist project %s to DynamoDB",
                    staged.project_id,
                    exc_info=True,
                )
                return None, self._project_store_unavailable()
        else:
            revision = expected_revision + 1

        committed = replace(staged, revision=revision)
        self._publish_project(committed)
        self._register_project_budget(committed)
        return committed, None

    async def _set_canonical_project_membership(
        self,
        project: Project,
        user_id: object,
        *,
        granted: bool,
    ) -> tuple[Project | None, bool, JSONResponse | None]:
        """Apply one tenant grant through the authoritative transaction."""
        if not isinstance(user_id, str) or not user_id.strip():
            return None, False, JSONResponse(
                {
                    "error": {
                        "type": "invalid_request",
                        "message": "user_id must be a non-empty SCIM user id",
                    }
                },
                status_code=400,
            )
        scim_user_id = user_id.removeprefix("scim:")
        setter = getattr(
            self._persistence,
            "set_tenant_project_membership",
            None,
        )
        if project.tenant_id is None or not callable(setter):
            return None, False, JSONResponse(
                {
                    "error": {
                        "type": "service_unavailable",
                        "message": (
                            "Canonical project membership is unavailable"
                        ),
                    }
                },
                status_code=503,
            )
        try:
            updated, changed = await setter(
                project.tenant_id,
                project.project_id,
                scim_user_id,
                granted=granted,
            )
        except CanonicalMembershipNotFoundError as exc:
            return None, False, JSONResponse(
                {
                    "error": {
                        "type": "not_found",
                        "message": str(exc),
                    }
                },
                status_code=404,
            )
        except CanonicalMembershipConflictError:
            latest = await self._reload_project(
                project.project_id,
                project.tenant_id,
            )
            return None, False, self._project_write_conflict(
                latest.revision if latest is not None else None
            )
        except ValueError as exc:
            return None, False, JSONResponse(
                {
                    "error": {
                        "type": "invalid_request",
                        "message": str(exc),
                    }
                },
                status_code=400,
            )
        except RuntimeError:
            logger.exception("Canonical project membership write failed")
            return None, False, JSONResponse(
                {
                    "error": {
                        "type": "service_unavailable",
                        "message": (
                            "Canonical project membership is unavailable"
                        ),
                    }
                },
                status_code=503,
            )
        self._publish_project(updated)
        self._register_project_budget(updated)
        return updated, changed, None

    async def add_member(self, request: Request) -> JSONResponse:
        """Add a user to a project."""
        project_id = request.path_params["id"]
        project = await self._get_project(
            project_id,
            _request_tenant_id(request),
        )
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )
        body = await request.json()
        user_id = body.get("user_id")
        if not user_id:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'user_id' is required"}},
                status_code=400,
            )
        if (
            project.tenant_id is not None
            and self._app_config.canonical_identity_required
        ):
            updated, _changed, error = (
                await self._set_canonical_project_membership(
                    project,
                    user_id,
                    granted=True,
                )
            )
            if error is not None:
                return error
            assert updated is not None
            normalized = f"scim:{user_id.removeprefix('scim:')}"
            return JSONResponse(
                {
                    "project_id": project_id,
                    "user_id": normalized,
                    "revision": updated.revision,
                    "status": "added",
                },
                headers=_revision_headers(updated.revision),
            )

        expected_revision, error = _parse_if_match_revision(
            request,
            project.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        if expected_revision != project.revision:
            latest = await self._reload_project(
                project_id,
                project.tenant_id,
            )
            return self._project_write_conflict(
                latest.revision if latest is not None else None
            )

        committed = project
        if user_id not in project.members:
            staged = _staged_project(
                project,
                members=[*project.members, user_id],
            )
            committed, error = await self._commit_project(
                staged,
                expected_revision=expected_revision,
            )
            if error is not None:
                return error
            assert committed is not None
        return JSONResponse(
            {
                "project_id": project_id,
                "user_id": user_id,
                "revision": committed.revision,
                "status": "added",
            },
            headers=_revision_headers(committed.revision),
        )

    # ------------------------------------------------------------------
    # DELETE /admin/projects/{id}/members/{user_id}
    # ------------------------------------------------------------------

    async def remove_member(self, request: Request) -> JSONResponse:
        """Remove a user from a project."""
        project_id = request.path_params["id"]
        user_id = request.path_params["user_id"]
        project = await self._get_project(
            project_id,
            _request_tenant_id(request),
        )
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )
        if (
            project.tenant_id is not None
            and self._app_config.canonical_identity_required
        ):
            updated, changed, error = (
                await self._set_canonical_project_membership(
                    project,
                    user_id,
                    granted=False,
                )
            )
            if error is not None:
                return error
            assert updated is not None
            normalized = f"scim:{user_id.removeprefix('scim:')}"
            if not changed:
                return JSONResponse(
                    {
                        "error": {
                            "type": "not_found",
                            "message": (
                                f"User '{normalized}' is not a member of "
                                f"project '{project_id}'"
                            ),
                        }
                    },
                    status_code=404,
                )
            return JSONResponse(
                {
                    "project_id": project_id,
                    "user_id": normalized,
                    "revision": updated.revision,
                    "status": "removed",
                },
                headers=_revision_headers(updated.revision),
            )

        expected_revision, error = _parse_if_match_revision(
            request,
            project.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        if expected_revision != project.revision:
            latest = await self._reload_project(
                project_id,
                project.tenant_id,
            )
            return self._project_write_conflict(
                latest.revision if latest is not None else None
            )

        if user_id in project.members:
            staged = _staged_project(
                project,
                members=[
                    member
                    for member in project.members
                    if member != user_id
                ],
            )
            committed, error = await self._commit_project(
                staged,
                expected_revision=expected_revision,
            )
            if error is not None:
                return error
            assert committed is not None
            return JSONResponse(
                {
                    "project_id": project_id,
                    "user_id": user_id,
                    "revision": committed.revision,
                    "status": "removed",
                },
                headers=_revision_headers(committed.revision),
            )
        return JSONResponse(
            {"error": {"type": "not_found", "message": f"User '{user_id}' is not a member of project '{project_id}'"}},
            status_code=404,
        )

    # ------------------------------------------------------------------
    # GET /admin/usage
    # ------------------------------------------------------------------

    async def usage(self, request: Request) -> JSONResponse:
        """Aggregated usage with query-param filters."""
        params = request.query_params

        filters = UsageFilters(
            start_time=_parse_datetime(params.get("start_time")),
            end_time=_parse_datetime(params.get("end_time")),
            provider=params.get("provider"),
            model=params.get("model"),
            project_id=params.get("project_id"),
            user_id=params.get("user_id"),
            tenant_id=_request_tenant_id(request),
        )

        # Before aggregating, not after: get_aggregated_usage and _apply_filters
        # both read the record list, so the refresh has to land first or the
        # report describes one instance.
        await self._synced_records()
        report = await self.cost_tracker.get_aggregated_usage(filters)

        total_cached_tokens = sum(
            r.cached_tokens for r in self.cost_tracker._apply_filters(filters)
        )
        total_cache_creation_tokens = sum(
            r.cache_creation_tokens for r in self.cost_tracker._apply_filters(filters)
        )

        return JSONResponse({
            "total_requests": report.total_requests,
            "total_tokens": report.total_tokens,
            "total_cost": report.total_cost,
            "total_cached_tokens": total_cached_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "breakdown": [
                {
                    "group_key": b.group_key,
                    "group_by": b.group_by,
                    "requests": b.requests,
                    "tokens": b.tokens,
                    "cost": b.cost,
                }
                for b in report.breakdown
            ],
        })

    # ------------------------------------------------------------------
    # GET /admin/usage/export
    # ------------------------------------------------------------------

    async def usage_export(self, request: Request):
        """Export usage for chargeback/FinOps.

        Same filters as /admin/usage, plus:
          - format=csv (default) | json
          - level=records (default, one row per request) | breakdown (aggregated)

        `records` is the detail an owner needs to attribute spend per request;
        `breakdown` is the aggregated summary (by provider/model/project/user).
        CSV streams as a file attachment.
        """
        params = request.query_params
        filters = UsageFilters(
            start_time=_parse_datetime(params.get("start_time")),
            end_time=_parse_datetime(params.get("end_time")),
            provider=params.get("provider"),
            model=params.get("model"),
            project_id=params.get("project_id"),
            user_id=params.get("user_id"),
            tenant_id=_request_tenant_id(request),
        )
        fmt = (params.get("format") or "csv").lower()
        level = (params.get("level") or "records").lower()
        if fmt not in ("csv", "json"):
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "format must be csv or json"}},
                status_code=400,
            )
        if level not in ("records", "breakdown"):
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "level must be records or breakdown"}},
                status_code=400,
            )
        if self._export_jobs is not None:
            tenant_id = _request_tenant_id(request) or LEGACY_TENANT_ID
            try:
                job = await self._export_jobs.create_usage(
                    tenant_id=tenant_id,
                    requested_by=request_export_identity(request),
                    format=fmt,
                    level=level,
                    filters={
                        "start_time": params.get("start_time"),
                        "end_time": params.get("end_time"),
                        "provider": params.get("provider"),
                        "model": params.get("model"),
                        "project_id": params.get("project_id"),
                        "user_id": params.get("user_id"),
                    },
                )
            except ValueError:
                return JSONResponse(
                    {
                        "error": {
                            "type": "invalid_request",
                            "message": "Usage export filters are invalid.",
                        }
                    },
                    status_code=400,
                )
            except ExportJobError:
                logger.error(
                    "Unable to create usage export",
                    exc_info=True,
                )
                return JSONResponse(
                    {
                        "error": {
                            "type": "export_unavailable",
                            "message": "Usage export could not be queued.",
                        }
                    },
                    status_code=503,
                )
            body = export_job_public(job)
            body["statusUrl"] = f"/admin/usage/exports/{job.job_id}"
            return JSONResponse(
                body,
                status_code=202,
                headers={"Retry-After": "2"},
            )

        # A chargeback export that silently covered one task of N would allocate
        # cost to the wrong owners, so this refresh matters more here than on any
        # dashboard panel. After validation, so a bad request fails without paying
        # for a scan.
        await self._synced_records()

        if level == "records":
            columns = [
                "request_id", "timestamp", "project_id", "user_id", "provider", "model",
                "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
                "cost", "latency_ms", "status", "routing_strategy",
            ]
            rows = [
                {
                    "request_id": r.request_id,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else "",
                    "project_id": r.project_id,
                    "user_id": r.user_id,
                    "provider": r.provider,
                    "model": r.model,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.total_tokens,
                    "cached_tokens": getattr(r, "cached_tokens", 0),
                    "cost": r.cost,
                    "latency_ms": getattr(r, "latency_ms", 0),
                    "status": getattr(r, "status", "success"),
                    "routing_strategy": getattr(r, "routing_strategy", ""),
                }
                for r in self.cost_tracker._apply_filters(filters)
            ]
        else:
            report = await self.cost_tracker.get_aggregated_usage(filters)
            columns = ["group_by", "group_key", "requests", "tokens", "cost"]
            rows = [
                {
                    "group_by": b.group_by,
                    "group_key": b.group_key,
                    "requests": b.requests,
                    "tokens": b.tokens,
                    "cost": b.cost,
                }
                for b in report.breakdown
            ]

        if fmt == "json":
            return JSONResponse({"level": level, "rows": rows})

        # CSV as a streamed file attachment
        def _csv_iter():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=columns)
            writer.writeheader()
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for row in rows:
                writer.writerow(row)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        filename = f"axonllm-usage-{level}.csv"
        return StreamingResponse(
            _csv_iter(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def usage_export_status(self, request: Request) -> JSONResponse:
        """Return one requester-owned asynchronous usage export."""

        if self._export_jobs is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export jobs are not enabled."}},
                status_code=404,
            )
        try:
            job = await self._export_jobs.get(
                tenant_id=_request_tenant_id(request) or LEGACY_TENANT_ID,
                requested_by=request_export_identity(request),
                job_id=request.path_params["job_id"],
                kind=ExportKind.USAGE,
            )
        except ExportJobNotFound:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export job was not found."}},
                status_code=404,
            )
        except ExportJobError:
            logger.error(
                "Unable to read usage export status",
                exc_info=True,
            )
            return JSONResponse(
                {
                    "error": {
                        "type": "export_unavailable",
                        "message": "Usage export status is temporarily unavailable.",
                    }
                },
                status_code=503,
            )
        body = export_job_public(job)
        if job.status.value == "complete":
            body["downloadUrl"] = (
                f"/admin/usage/exports/{job.job_id}/download"
            )
        return JSONResponse(body)

    async def usage_export_download(self, request: Request):
        """Redirect an authorized requester to a short-lived S3 URL."""

        if self._export_jobs is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export jobs are not enabled."}},
                status_code=404,
            )
        try:
            url = await self._export_jobs.download_url(
                tenant_id=_request_tenant_id(request) or LEGACY_TENANT_ID,
                requested_by=request_export_identity(request),
                job_id=request.path_params["job_id"],
                kind=ExportKind.USAGE,
            )
        except ExportJobNotFound:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export job was not found."}},
                status_code=404,
            )
        except ExportJobNotReady:
            return JSONResponse(
                {
                    "error": {
                        "type": "export_not_ready",
                        "message": "Export job is not complete.",
                    }
                },
                status_code=409,
                headers={"Retry-After": "2"},
            )
        except ExportJobError:
            logger.error(
                "Unable to authorize usage export download",
                exc_info=True,
            )
            return JSONResponse(
                {
                    "error": {
                        "type": "export_unavailable",
                        "message": "Usage export download is temporarily unavailable.",
                    }
                },
                status_code=503,
            )
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------------
    # GET /admin/policies
    # ------------------------------------------------------------------

    async def list_policies(self, request: Request) -> JSONResponse:
        """Return stored Cedar policies, including any another instance wrote.

        Reads through the evaluator's refresh rather than answering from the
        local list: a policy created on the other task was invisible here, so an
        operator checking their work saw it missing and reasonably concluded the
        write had failed.
        """
        tenant_id = _request_tenant_id(request)
        state = getattr(request, "state", None) if request is not None else None
        context = getattr(state, "context", None)
        if self._policy_service is not None:
            refresh = getattr(self._policy_service, "refresh_if_stale", None)
            if refresh is not None:
                try:
                    await refresh(
                        context if tenant_id is not None else None,
                    )
                except Exception:
                    if tenant_id is not None:
                        logger.error(
                            "Tenant policy listing refresh failed",
                            exc_info=True,
                        )
                        return JSONResponse(
                            {
                                "error": {
                                    "type": "service_unavailable",
                                    "message": (
                                        "Tenant policy listing is unavailable."
                                    ),
                                }
                            },
                            status_code=503,
                        )
                    logger.warning(
                        "Policy refresh failed; listing this instance's set",
                        exc_info=True,
                    )
            if tenant_id is not None:
                scoped = getattr(
                    self._policy_service,
                    "policies_for_scope",
                    None,
                )
                if not callable(scoped):
                    return JSONResponse(
                        {
                            "error": {
                                "type": "service_unavailable",
                                "message": (
                                    "Tenant policy listing is unavailable."
                                ),
                            }
                        },
                        status_code=503,
                    )
                return JSONResponse(scoped(tenant_id))
        elif tenant_id is not None:
            return JSONResponse(
                {
                    "error": {
                        "type": "service_unavailable",
                        "message": "Tenant policy listing is unavailable.",
                    }
                },
                status_code=503,
            )
        return JSONResponse(self.policies)

    # ------------------------------------------------------------------
    # POST /admin/policies
    # ------------------------------------------------------------------

    async def create_policy(self, request: Request) -> JSONResponse:
        """Store a new Cedar policy, persist it, and apply it to live traffic."""
        body = await request.json()
        tenant_id = _request_tenant_id(request)

        name = body.get("name")
        if not name:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'name' is required"}},
                status_code=400,
            )

        policy_text = body.get("policy_text", "")
        # Reject text the evaluator cannot parse instead of accepting it and
        # skipping it with a log line at startup. A policy silently dropped is
        # indistinguishable from one that permits everything, and the operator
        # who wrote it has already moved on.
        if policy_text and parse_policy(policy_text) is None:
            return JSONResponse(
                {"error": {
                    "type": "invalid_request",
                    "message": (
                        "Field 'policy_text' is not a supported Cedar statement. "
                        "Expected permit(...) or forbid(...), optionally with a "
                        "when/unless clause whose conditions compare principal "
                        "attributes (role, project, tenant, user) to a quoted "
                        'string, e.g. permit(principal, action == Action::"read", '
                        "resource); or forbid(principal, action, resource) unless "
                        '{ principal.role == "senior" };'
                    ),
                }},
                status_code=400,
            )

        policy = {
            "name": name,
            "description": body.get("description", ""),
            "policy_text": policy_text,
            "mode": body.get("mode", "LOG_ONLY"),
        }
        if tenant_id is not None:
            policy["tenant_id"] = tenant_id

        if tenant_id is not None:
            if self._policy_service is None:
                return JSONResponse(
                    {
                        "error": {
                            "type": "service_unavailable",
                            "message": "Tenant policy service is unavailable.",
                        }
                    },
                    status_code=503,
                )
            state = getattr(request, "state", None)
            context = getattr(state, "context", None)
            try:
                await self._policy_service.refresh_if_stale(context)
            except Exception:
                logger.error(
                    "Tenant policy initialization failed",
                    exc_info=True,
                )
                return JSONResponse(
                    {
                        "error": {
                            "type": "service_unavailable",
                            "message": "Tenant policy service is unavailable.",
                        }
                    },
                    status_code=503,
                )
            current_policies = self._policy_service.policies_for_scope(
                tenant_id
            )
        else:
            current_policies = list(self.policies)

        # Update existing policy with the same name, or append
        status, code = "created", 201
        for i, existing in enumerate(current_policies):
            if existing["name"] == name:
                current_policies[i] = policy
                status, code = "updated", 200
                break
        else:
            current_policies.append(policy)

        version = None
        if self._persistence is not None:
            if tenant_id is None:
                await self._persistence.save_cedar_policy(policy)
                # The write bumped the shared version; read it back so this
                # instance does not re-scan to learn what it already knows.
                version = await self._persistence.get_policy_version()
            else:
                save = getattr(
                    self._persistence,
                    "save_tenant_cedar_policy",
                    None,
                )
                if not callable(save):
                    return JSONResponse(
                        {
                            "error": {
                                "type": "service_unavailable",
                                "message": (
                                    "Tenant policy persistence is unavailable."
                                ),
                            }
                        },
                        status_code=503,
                )
                try:
                    await save(tenant_id, policy)
                except Exception:
                    logger.error(
                        "Failed to persist tenant Cedar policy %s/%s",
                        tenant_id,
                        name,
                        exc_info=True,
                    )
                    return JSONResponse(
                        {
                            "error": {
                                "type": "service_unavailable",
                                "message": (
                                    "Tenant policy persistence is unavailable."
                                ),
                            }
                        },
                        status_code=503,
                    )
        elif tenant_id is not None:
            return JSONResponse(
                {
                    "error": {
                        "type": "service_unavailable",
                        "message": "Tenant policy persistence is unavailable.",
                    }
                },
                status_code=503,
            )
        # Statements are compiled once, so without this the policy sits in the
        # list without affecting a single request until the process restarts.
        if self._policy_service is not None:
            if tenant_id is None:
                self.policies[:] = current_policies
                self._policy_service.reload(
                    current_policies,
                    tenant_id=None,
                )
                note = getattr(
                    self._policy_service,
                    "note_local_version",
                    None,
                )
                if note is not None:
                    note(version, tenant_id=None)
            else:
                # A local pre-write snapshot may be missing a concurrent policy
                # written by another task. Reload the authoritative tenant set
                # after the transaction instead of associating that stale
                # snapshot with the latest fleet version.
                invalidate = getattr(
                    self._policy_service,
                    "invalidate_scope",
                    None,
                )
                if callable(invalidate):
                    invalidate(tenant_id)
                try:
                    await self._policy_service.refresh_if_stale(
                        context,
                        require_fresh=True,
                    )
                except Exception:
                    logger.error(
                        "Tenant policy persisted but authoritative refresh failed",
                        exc_info=True,
                    )
                    return JSONResponse(
                        {
                            "error": {
                                "type": "service_unavailable",
                                "message": (
                                    "Tenant policy service is unavailable."
                                ),
                            }
                        },
                        status_code=503,
                    )
        elif tenant_id is None:
            self.policies[:] = current_policies

        return JSONResponse({"name": name, "status": status}, status_code=code)

    # ------------------------------------------------------------------
    # GET /admin/health
    # ------------------------------------------------------------------

    async def health(self, request: Request) -> JSONResponse:
        """Per-provider health status and runtime agent status."""
        await self._refresh_config()
        models = self.model_registry.list_models()
        providers: set[str] = set()
        for m in models:
            for p in m.providers:
                providers.add(p.provider)

        provider_health: dict[str, str] = {}
        for provider in sorted(providers):
            provider_health[provider] = (
                "healthy" if self.health_tracker.is_healthy(provider) else "unhealthy"
            )

        # Surface persistence reachability so a misconfigured/missing DynamoDB
        # table or IAM denial is visible here instead of silently dropping writes.
        persistence_health = None
        overall = "ok"
        if self._persistence is not None:
            persistence_health = await self._persistence.health_status()
            if persistence_health.get("enabled") and persistence_health.get("reachable") is False:
                overall = "degraded"
        routing_health = getattr(
            self._config_sync,
            "routing_config_status",
            None,
        )
        if (
            isinstance(routing_health, dict)
            and routing_health.get("status") == "degraded"
        ):
            overall = "degraded"

        return JSONResponse({
            "status": overall,
            "providers": provider_health,
            "persistence": persistence_health,
            "routing_configuration": routing_health,
            "runtime": "running",
        })

    # ------------------------------------------------------------------
    # GET /admin/users
    # ------------------------------------------------------------------

    async def list_users(self, request: Request) -> JSONResponse:
        """List all users with aggregated usage stats and budget info."""
        # Budgets come from cost_tracker._user_budgets, which is armed from the
        # user config dict — so without this the limits shown were whichever ones
        # this task happened to be told about.
        await self._refresh_config()
        tenant_id = _request_tenant_id(request)
        records = [
            record
            for record in await self._synced_records()
            if tenant_id is None or record.tenant_id == tenant_id
        ]
        user_data: dict[str, dict] = {}
        for r in records:
            entry = user_data.setdefault(r.user_id, {
                "user_id": r.user_id,
                "projects": set(),
                "requests": 0,
                "total_tokens": 0,
                "cost": 0.0,
            })
            entry["projects"].add(r.project_id)
            entry["requests"] += 1
            entry["total_tokens"] += r.total_tokens
            entry["cost"] += r.cost

        # Exact from the shared counter where there is one; the summed cost is the
        # fallback. Utilization is what a user gets throttled on, so it should not
        # disagree with enforcement. Gathered rather than awaited per user: this
        # loop is the worst offender, since a busy deployment has far more users
        # than projects.
        users = list(user_data.values())
        try:
            configs = await asyncio.gather(*(
                self._get_user_config(user["user_id"], tenant_id)
                for user in users
            ))
        except Exception:
            logger.error(
                "Tenant user configuration listing failed",
                exc_info=True,
            )
            return JSONResponse(
                {
                    "error": {
                        "type": "service_unavailable",
                        "message": "Tenant user configuration is unavailable",
                    }
                },
                status_code=503,
            )
        for user, config in zip(users, configs):
            self.cost_tracker.register_user(
                user["user_id"],
                budget_limit=config.get("budget_limit"),
                alert_threshold=config.get("alert_threshold"),
                tenant_id=tenant_id,
            )
        spends = await asyncio.gather(*(
            self._fleet_spend(
                "user",
                u["user_id"],
                u["cost"],
                tenant_id=tenant_id,
            )
            for u in users
        ))

        result = []
        for u, config, current_spend in zip(users, configs, spends):
            budget = self.cost_tracker.get_user_budget(
                u["user_id"],
                tenant_id=tenant_id,
            )
            budget_limit = budget.get("budget_limit")
            utilization = (current_spend / budget_limit * 100) if budget_limit else None
            result.append({
                "user_id": u["user_id"],
                "revision": config.get("revision", 0),
                "projects": sorted(u["projects"]),
                "requests": u["requests"],
                "total_tokens": u["total_tokens"],
                "cost": current_spend,
                "budget_limit": budget_limit,
                "alert_threshold": budget.get("alert_threshold"),
                "budget_utilization_pct": utilization,
            })
        result.sort(key=lambda x: x["cost"], reverse=True)
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/users/{id}
    # ------------------------------------------------------------------

    async def get_user(self, request: Request) -> JSONResponse:
        """User detail with per-project and per-model breakdown, plus budget info."""
        user_id = request.path_params["id"]
        tenant_id = _request_tenant_id(request)
        # Same reason as list_users: the budget and allowed-models this reports
        # are per-instance config unless the sync has run.
        await self._refresh_config()
        records = [
            record
            for record in await self._synced_records()
            if record.user_id == user_id
            and (tenant_id is None or record.tenant_id == tenant_id)
        ]
        if not records:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"User '{user_id}' not found"}},
                status_code=404,
            )

        total_requests = len(records)
        total_tokens = sum(r.total_tokens for r in records)
        total_cost = await self._fleet_spend(
            "user",
            user_id,
            sum(r.cost for r in records),
            tenant_id=tenant_id,
        )
        projects = sorted({r.project_id for r in records})

        model_breakdown: dict[str, dict] = {}
        for r in records:
            entry = model_breakdown.setdefault(r.model, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        provider_breakdown: dict[str, dict] = {}
        for r in records:
            entry = provider_breakdown.setdefault(r.provider, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        project_breakdown: dict[str, dict] = {}
        for r in records:
            entry = project_breakdown.setdefault(r.project_id, {"requests": 0, "tokens": 0, "cost": 0.0})
            entry["requests"] += 1
            entry["tokens"] += r.total_tokens
            entry["cost"] += r.cost

        try:
            user_config = await self._get_user_config(
                user_id,
                tenant_id,
            )
        except Exception:
            return JSONResponse(
                {
                    "error": {
                        "type": "service_unavailable",
                        "message": "Tenant user configuration is unavailable",
                    }
                },
                status_code=503,
            )
        self.cost_tracker.register_user(
            user_id,
            budget_limit=user_config.get("budget_limit"),
            alert_threshold=user_config.get("alert_threshold"),
            tenant_id=tenant_id,
        )
        budget = self.cost_tracker.get_user_budget(
            user_id,
            tenant_id=tenant_id,
        )

        revision = user_config.get("revision", 0)
        return JSONResponse({
            "user_id": user_id,
            "revision": revision,
            "projects": projects,
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "budget_limit": budget.get("budget_limit"),
            "alert_threshold": budget.get("alert_threshold"),
            "allowed_models": user_config.get("allowed_models"),
            "usage_by_model": model_breakdown,
            "usage_by_provider": provider_breakdown,
            "usage_by_project": project_breakdown,
        }, headers=_revision_headers(revision))

    async def set_user_budget(self, request: Request) -> JSONResponse:
        """Set or update budget for a user."""
        user_id = request.path_params["id"]
        tenant_id = _request_tenant_id(request)
        try:
            body = await request.json()
        except Exception:
            body = {}

        budget_limit = body.get("budget_limit")
        alert_threshold = body.get("alert_threshold")
        try:
            current = deepcopy(
                await self._get_user_config(user_id, tenant_id)
            )
        except Exception:
            return self._user_config_store_unavailable()

        current_revision = current.get("revision", 0)
        expected_revision, error = _parse_if_match_revision(
            request,
            current_revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        if expected_revision != current_revision:
            latest = await self._reload_user_config(user_id, tenant_id)
            return self._user_config_write_conflict(
                latest.get("revision", 0)
                if latest is not None
                else None
            )

        staged = {
            **current,
            "budget_limit": budget_limit,
            "alert_threshold": alert_threshold,
        }

        try:
            committed = await self._save_user_config(
                user_id,
                tenant_id,
                staged,
                expected_revision=expected_revision,
            )
        except PersistenceConflictError:
            latest = await self._reload_user_config(user_id, tenant_id)
            return self._user_config_write_conflict(
                latest.get("revision", 0)
                if latest is not None
                else None
            )
        except Exception:
            logger.warning(
                "Failed to persist user config for %s",
                user_id,
                exc_info=True,
            )
            return self._user_config_store_unavailable()

        self.cost_tracker.register_user(
            user_id=user_id,
            budget_limit=budget_limit,
            alert_threshold=alert_threshold,
            tenant_id=tenant_id,
        )

        return JSONResponse({
            "user_id": user_id,
            "budget_limit": budget_limit,
            "alert_threshold": alert_threshold,
            "revision": committed["revision"],
            "status": "updated",
        }, headers=_revision_headers(committed["revision"]))

    # ------------------------------------------------------------------
    # PUT /admin/users/{id}/allowed-models
    # ------------------------------------------------------------------

    async def set_user_allowed_models(self, request: Request) -> JSONResponse:
        """Set or update allowed models for a user."""
        user_id = request.path_params["id"]
        tenant_id = _request_tenant_id(request)
        body = await request.json()
        allowed_models = body.get("allowed_models")
        try:
            current = deepcopy(
                await self._get_user_config(user_id, tenant_id)
            )
        except Exception:
            logger.warning(
                "Failed to load user config for %s",
                user_id,
                exc_info=True,
            )
            return self._user_config_store_unavailable()

        current_revision = current.get("revision", 0)
        expected_revision, error = _parse_if_match_revision(
            request,
            current_revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        if expected_revision != current_revision:
            latest = await self._reload_user_config(user_id, tenant_id)
            return self._user_config_write_conflict(
                latest.get("revision", 0)
                if latest is not None
                else None
            )

        staged = {
            **current,
            "allowed_models": deepcopy(allowed_models),
        }
        try:
            committed = await self._save_user_config(
                user_id,
                tenant_id,
                staged,
                expected_revision=expected_revision,
            )
        except PersistenceConflictError:
            latest = await self._reload_user_config(user_id, tenant_id)
            return self._user_config_write_conflict(
                latest.get("revision", 0)
                if latest is not None
                else None
            )
        except Exception:
            logger.warning(
                "Failed to persist user config for %s",
                user_id,
                exc_info=True,
            )
            return self._user_config_store_unavailable()

        return JSONResponse({
            "user_id": user_id,
            "allowed_models": allowed_models,
            "revision": committed["revision"],
            "status": "updated",
        }, headers=_revision_headers(committed["revision"]))


    # ------------------------------------------------------------------
    # GET /admin/projects/{id}/models
    # ------------------------------------------------------------------

    async def list_project_models(self, request: Request) -> JSONResponse:
        """Return the allowed models for a project."""
        project_id = request.path_params["id"]
        project = await self._get_project(
            project_id,
            _request_tenant_id(request),
        )
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )
        return JSONResponse({
            "project_id": project_id,
            "revision": project.revision,
            "allowed_models": project.allowed_models if project.allowed_models is not None else [],
        }, headers=_revision_headers(project.revision))

    # ------------------------------------------------------------------
    # POST /admin/projects/{id}/models
    # ------------------------------------------------------------------

    async def add_project_model(self, request: Request) -> JSONResponse:
        """Add a model to a project's allowed_models list."""
        project_id = request.path_params["id"]
        project = await self._get_project(
            project_id,
            _request_tenant_id(request),
        )
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        body = await request.json()
        model = body.get("model")
        if not model:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'model' is required"}},
                status_code=400,
            )

        expected_revision, error = _parse_if_match_revision(
            request,
            project.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        if expected_revision != project.revision:
            latest = await self._reload_project(
                project_id,
                project.tenant_id,
            )
            return self._project_write_conflict(
                latest.revision if latest is not None else None
            )

        committed = project
        allowed_models = list(project.allowed_models or [])
        if model not in allowed_models:
            allowed_models.append(model)
            staged = _staged_project(
                project,
                allowed_models=allowed_models,
            )
            committed, error = await self._commit_project(
                staged,
                expected_revision=expected_revision,
            )
            if error is not None:
                return error
            assert committed is not None

        return JSONResponse({
            "project_id": project_id,
            "model": model,
            "revision": committed.revision,
            "allowed_models": committed.allowed_models,
            "status": "added",
        }, headers=_revision_headers(committed.revision))

    # ------------------------------------------------------------------
    # DELETE /admin/projects/{id}/models/{model_name}
    # ------------------------------------------------------------------

    async def remove_project_model(self, request: Request) -> JSONResponse:
        """Remove a model from a project's allowed_models list."""
        project_id = request.path_params["id"]
        model_name = request.path_params["model_name"]
        project = await self._get_project(
            project_id,
            _request_tenant_id(request),
        )
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        if project.allowed_models is None or model_name not in project.allowed_models:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Model '{model_name}' is not in project's allowed models"}},
                status_code=404,
            )

        expected_revision, error = _parse_if_match_revision(
            request,
            project.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        if expected_revision != project.revision:
            latest = await self._reload_project(
                project_id,
                project.tenant_id,
            )
            return self._project_write_conflict(
                latest.revision if latest is not None else None
            )

        staged = _staged_project(
            project,
            allowed_models=[
                model
                for model in project.allowed_models
                if model != model_name
            ],
        )
        committed, error = await self._commit_project(
            staged,
            expected_revision=expected_revision,
        )
        if error is not None:
            return error
        assert committed is not None

        return JSONResponse({
            "project_id": project_id,
            "model": model_name,
            "revision": committed.revision,
            "allowed_models": committed.allowed_models,
            "status": "removed",
        }, headers=_revision_headers(committed.revision))

    # ------------------------------------------------------------------
    # GET /admin/catalog
    # ------------------------------------------------------------------

    async def catalog(self, request: Request) -> JSONResponse:
        """Return the provider/model catalog."""
        return JSONResponse(self._catalog)

    # ------------------------------------------------------------------
    # GET /admin/models
    # ------------------------------------------------------------------

    async def list_models(self, request: Request) -> JSONResponse:
        """List all models with usage stats."""
        await self._refresh_config()
        models = self.model_registry.list_models()
        # No shared counter is keyed by model, so per-model cost has only the
        # record list as a source — the refresh is the whole fix here.
        records = await self._tenant_records(request)

        result = []
        for m in models:
            # Match by model name OR any provider model_id
            model_ids = {m.name} | {p.model_id for p in m.providers}
            model_records = [r for r in records if r.model in model_ids]
            total_requests = len(model_records)
            total_tokens = sum(r.total_tokens for r in model_records)
            total_cost = sum(r.cost for r in model_records)

            result.append({
                "name": m.name,
                "description": m.description,
                "routing_strategy": m.routing_strategy.value,
                "capabilities": m.capabilities or [],
                "providers": [
                    {"provider": p.provider, "model_id": p.model_id, "weight": p.weight, "fallback_order": p.fallback_order}
                    for p in m.providers
                ],
                "total_requests": total_requests,
                "total_tokens": total_tokens,
                "total_cost": total_cost,
            })
        return JSONResponse(
            result,
            headers=_revision_headers(self.model_registry.revision),
        )

    async def runtime_config(self, request: Request) -> JSONResponse:
        """Return the versioned, credential-free router configuration."""
        await self._refresh_config()
        active_snapshot = getattr(
            self._config_sync,
            "active_routing_snapshot",
            None,
        )
        snapshot = (
            active_snapshot
            if (
                isinstance(active_snapshot, RoutingConfigSnapshot)
                and active_snapshot.revision
                == self.model_registry.revision
            )
            else RoutingConfigSnapshot.from_registry(
                self.model_registry
            )
        )
        return JSONResponse(
            snapshot.as_dict(),
            headers={
                **_revision_headers(snapshot.revision),
                "Cache-Control": "no-store",
                "X-Axon-Config-SHA256": snapshot.sha256,
            },
        )

    @staticmethod
    def _model_store_unavailable() -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "type": "service_unavailable",
                    "code": "model_registry_unavailable",
                    "message": "Model registry persistence is unavailable",
                }
            },
            status_code=503,
        )

    @staticmethod
    def _model_write_conflict(
        revision: int | None = None,
    ) -> JSONResponse:
        error: dict[str, object] = {
            "type": "write_conflict",
            "code": "model_registry_write_conflict",
            "message": "Model registry changed concurrently; reload and retry",
        }
        headers = None
        if revision is not None:
            error["revision"] = revision
            headers = _revision_headers(revision)
        return JSONResponse(
            {"error": error},
            status_code=409,
            headers=headers,
        )

    def _publish_model_registry(
        self,
        snapshot: RoutingConfigSnapshot,
    ) -> None:
        """Publish a complete committed snapshot to every local route reader."""
        snapshot.apply(self.model_registry)
        note_snapshot = getattr(
            self._config_sync,
            "note_local_model_snapshot",
            None,
        )
        if callable(note_snapshot):
            note_snapshot(snapshot)
            return
        note_revision = getattr(
            self._config_sync,
            "note_local_model_revision",
            None,
        )
        if callable(note_revision):
            note_revision(snapshot.revision)

    async def _reload_model_registry(self) -> int | None:
        if self._persistence is None or not self._persistence.enabled:
            return None
        loader = getattr(
            self._persistence,
            "load_model_registry_snapshot",
            None,
        )
        if not callable(loader):
            return None
        try:
            snapshot = await loader()
            if snapshot is None:
                return None
            self._publish_model_registry(snapshot)
            return snapshot.revision
        except Exception:
            logger.warning(
                "Failed to reload the model registry after a conflict",
                exc_info=True,
            )
            return None

    async def _commit_model_registry(
        self,
        config: dict,
        *,
        expected_revision: int,
    ) -> tuple[int | None, JSONResponse | None]:
        """CAS a detached registry snapshot, then publish it locally."""
        if self._persistence is not None and self._persistence.enabled:
            saver = getattr(
                self._persistence,
                "save_model_registry",
                None,
            )
            if not callable(saver):
                return None, self._model_store_unavailable()
            try:
                snapshot = await saver(
                    config,
                    expected_revision=expected_revision,
                )
                revision = snapshot.revision
                revision = _next_revision(
                    revision,
                    expected_revision,
                    "model registry",
                )
            except PersistenceConflictError:
                latest_revision = await self._reload_model_registry()
                return None, self._model_write_conflict(latest_revision)
            except Exception:
                logger.warning(
                    "Failed to persist the model registry",
                    exc_info=True,
                )
                return None, self._model_store_unavailable()
        else:
            # Local development keeps hot editing useful without ever mutating
            # the checked-in bootstrap file. Production startup already
            # requires durable persistence.
            revision = expected_revision + 1
            snapshot = RoutingConfigSnapshot.from_config(
                config,
                revision=revision,
            )

        self._publish_model_registry(snapshot)
        return revision, None

    @staticmethod
    def _invalid_model_candidate(
        registry: ModelRegistry,
        candidate: dict,
    ) -> JSONResponse | None:
        errors = registry.validate(candidate)
        if errors:
            return JSONResponse(
                {
                    "errors": [
                        {"field": error.field, "message": error.message}
                        for error in errors
                    ]
                },
                status_code=400,
            )
        try:
            # Parsing before persistence catches malformed numeric/pricing
            # fields that structural validation alone cannot safely publish.
            ModelRegistry.from_config(candidate)
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse(
                {
                    "errors": [
                        {
                            "field": "models",
                            "message": f"Invalid model configuration: {exc}",
                        }
                    ]
                },
                status_code=400,
            )
        return None

    # ------------------------------------------------------------------
    # POST /admin/models
    # ------------------------------------------------------------------

    async def create_model(self, request: Request) -> JSONResponse:
        """Create a new model."""
        await self._refresh_config()
        body = await request.json()

        name = body.get("name")
        if not name:
            return JSONResponse(
                {"error": {"type": "invalid_request", "message": "Field 'name' is required"}},
                status_code=400,
            )

        new_entry: dict = {
            "name": name,
            "description": body.get("description", ""),
            "routing_strategy": body.get("routing_strategy", "round-robin"),
            "providers": body.get("providers", []),
        }
        capabilities = body.get("capabilities")
        if capabilities is not None:
            new_entry["capabilities"] = capabilities
        if "max_context_tokens" in body:
            new_entry["max_context_tokens"] = body[
                "max_context_tokens"
            ]

        candidate = self.model_registry.to_config()
        candidate["models"].append(new_entry)
        error = self._invalid_model_candidate(
            self.model_registry,
            candidate,
        )
        if error is not None:
            return error

        expected_revision, error = _parse_if_match_revision(
            request,
            self.model_registry.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        revision, error = await self._commit_model_registry(
            candidate,
            expected_revision=expected_revision,
        )
        if error is not None:
            return error
        assert revision is not None
        return JSONResponse(
            {
                "name": name,
                "revision": revision,
                "status": "created",
            },
            status_code=201,
            headers=_revision_headers(revision),
        )

    # ------------------------------------------------------------------
    # PUT /admin/models/{name}
    # ------------------------------------------------------------------

    async def update_model(self, request: Request) -> JSONResponse:
        """Update a model's configuration."""
        await self._refresh_config()
        model_name = request.path_params["name"]
        model_config = self.model_registry.models.get(model_name)
        if model_config is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Model '{model_name}' not found"}},
                status_code=404,
            )

        body = await request.json()

        candidate = self.model_registry.to_config()
        updated_entry = next(
            entry
            for entry in candidate["models"]
            if entry["name"] == model_name
        )
        for field in ("description", "routing_strategy", "providers"):
            if field in body:
                updated_entry[field] = body[field]
        for optional_field in ("capabilities", "max_context_tokens"):
            if optional_field not in body:
                continue
            value = body[optional_field]
            if value is None:
                updated_entry.pop(optional_field, None)
            else:
                updated_entry[optional_field] = value

        error = self._invalid_model_candidate(
            self.model_registry,
            candidate,
        )
        if error is not None:
            return error

        expected_revision, error = _parse_if_match_revision(
            request,
            self.model_registry.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        revision, error = await self._commit_model_registry(
            candidate,
            expected_revision=expected_revision,
        )
        if error is not None:
            return error
        assert revision is not None
        return JSONResponse(
            {
                "name": model_name,
                "revision": revision,
                "status": "updated",
            },
            headers=_revision_headers(revision),
        )

    # ------------------------------------------------------------------
    # DELETE /admin/models/{name}
    # ------------------------------------------------------------------

    async def delete_model(self, request: Request) -> JSONResponse:
        """Delete a model."""
        await self._refresh_config()
        model_name = request.path_params["name"]
        if model_name not in self.model_registry.models:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Model '{model_name}' not found"}},
                status_code=404,
            )

        candidate = self.model_registry.to_config()
        candidate["models"] = [
            entry
            for entry in candidate["models"]
            if entry["name"] != model_name
        ]
        expected_revision, error = _parse_if_match_revision(
            request,
            self.model_registry.revision,
        )
        if error is not None:
            return error
        assert expected_revision is not None
        revision, error = await self._commit_model_registry(
            candidate,
            expected_revision=expected_revision,
        )
        if error is not None:
            return error
        assert revision is not None
        return JSONResponse(
            {
                "name": model_name,
                "revision": revision,
                "status": "deleted",
            },
            headers=_revision_headers(revision),
        )



    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # GET /admin/traces
    # ------------------------------------------------------------------

    async def traces(self, request: Request) -> JSONResponse:
        """Return recent request traces for the live traces view."""
        records = await self._tenant_records(request)
        # Clamp rather than trust: an unparseable limit used to 500, and a negative
        # one silently returned an empty list.
        try:
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 1000))

        # Order by timestamp rather than slicing the tail. Within one process the
        # list is already chronological, so the tail was "recent" for free — but a
        # fleet-wide refresh appends other instances' records in scan order, which
        # is arbitrary. Taking the tail of that would show a mix of new local
        # requests and whatever the scan happened to return last, and label it
        # recent.
        #
        # nlargest, not sorted(): this runs every 3s per open tab, and a full sort
        # of 100k records blocks the event loop for ~19ms where nlargest costs
        # ~5ms. Nulls sort oldest — a record with no timestamp cannot be claimed to
        # be the newest thing that happened.
        recent = heapq.nlargest(
            limit,
            records,
            key=lambda r: self.cost_tracker._as_aware(r.timestamp) if r.timestamp else _EPOCH,
        )

        traces = []
        for r in recent:
            traces.append({
                "request_id": r.request_id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "model": r.model,
                "provider": r.provider,
                "user_id": r.user_id,
                "project_id": r.project_id,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "cost": r.cost,
                "latency_ms": getattr(r, "latency_ms", 0),
                "status": getattr(r, "status", "success"),
                "cached_tokens": r.cached_tokens,
                "routing_strategy": getattr(r, "routing_strategy", ""),
            })

        return JSONResponse({"traces": traces, "total": len(records)})

    # GET /admin/efficiency
    # ------------------------------------------------------------------

    async def efficiency_overview(self, request: Request) -> JSONResponse:
        """Token efficiency overview across all users."""
        if self._efficiency_analyzer is None:
            return JSONResponse(
                {"error": {"type": "not_configured", "message": "Efficiency analyzer not configured"}},
                status_code=501,
            )

        # The analyzer reads the same record list this API does, so refreshing
        # here is enough — it needs no fleet awareness of its own.
        await self._synced_records()
        tenant_id = _request_tenant_id(request)
        all_metrics = self._efficiency_analyzer.get_all_user_metrics(
            tenant_id=tenant_id,
        )

        grade_distribution: dict[str, int] = {}
        for m in all_metrics:
            grade = m.grade.value
            grade_distribution[grade] = grade_distribution.get(grade, 0) + 1

        total_cost = sum(m.total_cost for m in all_metrics)
        avg_score = sum(m.score for m in all_metrics) / len(all_metrics) if all_metrics else 0.0

        wasteful_users = [
            {"user_id": m.entity_id, "score": m.score, "grade": m.grade.value, "cost": m.total_cost}
            for m in all_metrics if m.score < 50
        ]
        wasteful_users.sort(key=lambda x: x["score"])

        return JSONResponse({
            "total_users_analyzed": len(all_metrics),
            "avg_efficiency_score": round(avg_score, 1),
            "grade_distribution": grade_distribution,
            "total_cost": round(total_cost, 4),
            "wasteful_users": wasteful_users[:10],
            "users": [
                {
                    "user_id": m.entity_id,
                    "score": m.score,
                    "grade": m.grade.value,
                    "completion_prompt_ratio": m.completion_prompt_ratio,
                    "cache_utilization_rate": m.cache_utilization_rate,
                    "avg_cost_per_request": m.avg_cost_per_request,
                    "expensive_model_ratio": m.expensive_model_ratio,
                    "duplicate_request_rate": m.duplicate_request_rate,
                    "total_requests": m.total_requests,
                    "total_cost": m.total_cost,
                }
                for m in sorted(all_metrics, key=lambda x: x.score)
            ],
        })

    # ------------------------------------------------------------------
    # GET /admin/users/{id}/efficiency
    # ------------------------------------------------------------------

    async def user_efficiency(self, request: Request) -> JSONResponse:
        """Full efficiency report for a specific user."""
        user_id = request.path_params["id"]

        if self._efficiency_analyzer is None:
            return JSONResponse(
                {"error": {"type": "not_configured", "message": "Efficiency analyzer not configured"}},
                status_code=501,
            )

        await self._synced_records()
        tenant_id = _request_tenant_id(request)
        report = self._efficiency_analyzer.analyze_user(
            user_id,
            tenant_id=tenant_id,
        )

        result: dict = {
            "user_id": user_id,
            "metrics": {
                "score": report.metrics.score,
                "grade": report.metrics.grade.value,
                "completion_prompt_ratio": report.metrics.completion_prompt_ratio,
                "cache_utilization_rate": report.metrics.cache_utilization_rate,
                "avg_cost_per_request": report.metrics.avg_cost_per_request,
                "expensive_model_ratio": report.metrics.expensive_model_ratio,
                "token_velocity_per_hour": report.metrics.token_velocity_per_hour,
                "duplicate_request_rate": report.metrics.duplicate_request_rate,
                "avg_prompt_tokens": report.metrics.avg_prompt_tokens,
                "avg_completion_tokens": report.metrics.avg_completion_tokens,
                "total_requests": report.metrics.total_requests,
                "total_cost": report.metrics.total_cost,
            },
            "alerts": [
                {
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "message": a.message,
                    "metric_value": a.metric_value,
                    "threshold": a.threshold,
                }
                for a in report.alerts
            ],
            "recommendations": [
                {
                    "current_model": r.current_model,
                    "recommended_model": r.recommended_model,
                    "task_type": r.task_type,
                    "estimated_savings_pct": r.estimated_savings_pct,
                    "quality_impact": r.quality_impact,
                    "reason": r.reason,
                }
                for r in report.recommendations
            ],
            "peer_comparison": report.peer_comparison,
        }

        # Add semantic analysis if available
        if self._semantic_engine is not None:
            semantic = self._semantic_engine.generate_report(
                user_id=user_id,
                tenant_id=tenant_id,
            )
            result["semantic"] = {
                "output_analysis": {
                    "avg_completion_tokens": semantic.output_analysis.avg_completion_tokens,
                    "estimated_utilization": semantic.output_analysis.estimated_utilization,
                    "recommendation": semantic.output_analysis.recommendation,
                },
                "waste_summary": semantic.waste_summary,
            }
            if semantic.user_profile:
                result["semantic"]["profile"] = {
                    "dominant_task_type": semantic.user_profile.dominant_task_type,
                    "avg_complexity": semantic.user_profile.avg_complexity,
                    "typical_model": semantic.user_profile.typical_model,
                    "optimal_model": semantic.user_profile.optimal_model,
                    "estimated_monthly_savings": semantic.user_profile.estimated_monthly_savings,
                    "patterns": semantic.user_profile.patterns,
                }
            if semantic.model_recommendations:
                result["semantic"]["model_recommendations"] = [
                    {
                        "current_model": r.current_model,
                        "recommended_model": r.recommended_model,
                        "task_type": r.task_type,
                        "estimated_savings_pct": r.estimated_savings_pct,
                        "quality_impact": r.quality_impact,
                        "reason": r.reason,
                    }
                    for r in semantic.model_recommendations
                ]

        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/projects/{id}/efficiency
    # ------------------------------------------------------------------

    async def project_efficiency(self, request: Request) -> JSONResponse:
        """Full efficiency report for a specific project."""
        project_id = request.path_params["id"]

        if self._efficiency_analyzer is None:
            return JSONResponse(
                {"error": {"type": "not_configured", "message": "Efficiency analyzer not configured"}},
                status_code=501,
            )

        project = await self._get_project(
            project_id,
            _request_tenant_id(request),
        )
        if project is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": f"Project '{project_id}' not found"}},
                status_code=404,
            )

        await self._synced_records()
        tenant_id = _request_tenant_id(request)
        report = self._efficiency_analyzer.analyze_project(
            project_id,
            tenant_id=tenant_id,
        )

        result: dict = {
            "project_id": project_id,
            "name": project.name,
            "metrics": {
                "score": report.metrics.score,
                "grade": report.metrics.grade.value,
                "completion_prompt_ratio": report.metrics.completion_prompt_ratio,
                "cache_utilization_rate": report.metrics.cache_utilization_rate,
                "avg_cost_per_request": report.metrics.avg_cost_per_request,
                "expensive_model_ratio": report.metrics.expensive_model_ratio,
                "token_velocity_per_hour": report.metrics.token_velocity_per_hour,
                "duplicate_request_rate": report.metrics.duplicate_request_rate,
                "avg_prompt_tokens": report.metrics.avg_prompt_tokens,
                "avg_completion_tokens": report.metrics.avg_completion_tokens,
                "total_requests": report.metrics.total_requests,
                "total_cost": report.metrics.total_cost,
            },
            "alerts": [
                {
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "message": a.message,
                    "metric_value": a.metric_value,
                    "threshold": a.threshold,
                }
                for a in report.alerts
            ],
            "recommendations": [
                {
                    "current_model": r.current_model,
                    "recommended_model": r.recommended_model,
                    "task_type": r.task_type,
                    "estimated_savings_pct": r.estimated_savings_pct,
                    "quality_impact": r.quality_impact,
                    "reason": r.reason,
                }
                for r in report.recommendations
            ],
            "user_comparison": report.peer_comparison,
        }

        # Add semantic waste analysis if available
        if self._semantic_engine is not None:
            semantic = self._semantic_engine.generate_report(
                project_id=project_id,
                tenant_id=tenant_id,
            )
            result["semantic"] = {
                "output_analysis": {
                    "avg_completion_tokens": semantic.output_analysis.avg_completion_tokens,
                    "estimated_utilization": semantic.output_analysis.estimated_utilization,
                    "recommendation": semantic.output_analysis.recommendation,
                },
                "waste_summary": semantic.waste_summary,
            }

        return JSONResponse(result)

    # ------------------------------------------------------------------
    # GET /admin/dashboard
    # ------------------------------------------------------------------

    async def dashboard(self, request: Request) -> HTMLResponse:
        """Serve the admin dashboard SPA."""
        index_path = _STATIC_DIR / "index.html"
        html = index_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    # ------------------------------------------------------------------
    # GET /admin/static/{path}
    # ------------------------------------------------------------------

    async def static_asset(self, request: Request):
        """Serve vendored dashboard assets (React, Babel) from static/.

        Vendored locally so the dashboard works in air-gapped / offline
        deployments — no runtime dependency on unpkg.com. Path is confined to
        the static dir to prevent traversal.

        Also serves static/tour/ — the guided demo's narration JSON and MP3s.
        They sit under the dashboard's own static dir, next to the index.html
        they belong to, rather than in site/: the tour drives this SPA and is
        useless without it, and site/ is the marketing site, uploaded to a
        separate S3 bucket and absent entirely from some deployments (see
        ``test_a_missing_site_dir_404s_rather_than_raising``).

        Serves byte ranges for the same measured reason as ``site_asset``: a
        browser that cannot range-request audio reports it as unseekable, and
        the tour's scrub-to-scene would move the bar without moving the sound.
        """
        from starlette.responses import PlainTextResponse, Response

        rel = request.path_params.get("path", "")
        # Confine to _STATIC_DIR — reject anything that escapes it.
        target = (_STATIC_DIR / rel).resolve()
        try:
            target.relative_to(_STATIC_DIR.resolve())
        except ValueError:
            return PlainTextResponse("Not found", status_code=404)
        if not target.is_file():
            return PlainTextResponse("Not found", status_code=404)

        suffix = target.suffix.lower()
        media_type = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".html": "text/html",
            # The guided tour's narration. Without the mp3 type a browser gets
            # application/octet-stream and declines to play it; without the json
            # type the fetch of the script still works but nothing else here
            # would tell you the omission was deliberate.
            ".mp3": "audio/mpeg",
            ".json": "application/json",
        }.get(suffix, "application/octet-stream")

        body = target.read_bytes()
        headers = {
            "Cache-Control": "public, max-age=3600",
            "Accept-Ranges": "bytes",
        }
        start, end = _parse_byte_range(request.headers.get("range"), len(body))
        if start is None:
            return Response(body, media_type=media_type, headers=headers)
        headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
        return Response(
            body[start : end + 1],
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    async def architecture(self, request: Request) -> HTMLResponse:
        """Serve the architecture diagram as an SVG embedded in a full-page viewer."""
        svg_path = _PROJECT_ROOT / "docs" / "architecture.svg"
        if not svg_path.exists():
            return HTMLResponse("<h1>Architecture diagram not found</h1>", status_code=404)

        embed = _is_embedded(request)
        svg_content = svg_path.read_text(encoding="utf-8")
        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            "<title>AxonLLM Architecture</title>" + PAGE_FAVICON
            + "<style>" + BASE_STYLE +
            # The viewer centers one diagram in the viewport, so it overrides the
            # shared sheet's document flow rather than adding to it.
            "body{display:flex;flex-direction:column;min-height:100vh}"
            ".diagram{flex:1;display:flex;align-items:center;justify-content:center;"
            "padding:24px;overflow:auto}"
            ".diagram svg{max-width:100%;height:auto;border-radius:16px;"
            f"background:{PAGE_SURFACE};box-shadow:0 0 0 1px rgba(214,211,209,0.3),"
            "0 8px 24px rgba(0,0,0,0.06)}"
            # Embedded, the shell owns the viewport: 100vh here would size the
            # diagram to the window while the frame around it is shorter, and
            # the padding is already the .main pane's.
            + (PAGE_EMBED_STYLE + "body{min-height:0}.diagram{padding:0}"
               if embed else "")
            + "</style></head><body>"
            + ("" if embed else page_ribbon("Architecture")) +
            '<div class="diagram">' + svg_content + "</div>"
            "</body></html>"
        )
        return HTMLResponse(html)

    async def landing_page(self, request: Request) -> HTMLResponse:
        """Serve the marketing landing page at the gateway root.

        Read from disk per request rather than cached, matching `dashboard` and
        `architecture` — editing site/index.html and reloading shows the change
        without a restart, which is the whole point of a single-file page.

        site/ is outside the installed package and is not in package-data, so a
        pip-installed gateway will not have it. That is why this 404s with an
        explanation instead of raising: a missing landing page must not make the
        root path a 500 on an otherwise healthy deployment.
        """
        index_path = _PROJECT_ROOT / "site" / "index.html"
        if not index_path.exists():
            return HTMLResponse(
                "<h1>AxonLLM</h1><p>Landing page not found. The gateway is "
                'running — see <a href="/admin/dashboard">the dashboard</a>.</p>',
                status_code=404,
            )
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    async def site_asset(self, request: Request):
        """Serve the marketing site's sibling pages and their assets.

        The landing page is served at ``/``, so its nav links are relative —
        ``architecture.html`` resolves against the gateway origin here and
        against the bucket on S3, with no hardcoded host in either case. Without
        this route the nav would work on the deployed site and 404 on every
        self-hosted gateway.

        Deliberately restricted: only the suffixes in ``SITE_ASSET_TYPES``, and
        only files sitting directly in ``site/`` or in one of the directories
        named in ``SITE_ASSET_DIRS``. ``site/infra/`` holds the CDK app (Python
        source, deploy config) and must not be readable over HTTP, so the depth
        check matters as much as the traversal check below it — and the allowed
        subdirectories are listed by name rather than admitted by depth, so
        adding one is a decision rather than a side effect.

        Registered last of all routes (see ``bootstrap.build_app``) because one
        of its patterns is a bare single segment — ahead of the other factories
        it would shadow ``/chat``, ``/playground`` and ``/routing``.

        Serves byte ranges. Without them a browser reports ``audio.seekable`` as
        an empty range and refuses to seek at all, so the narration player's
        scrub bar would move and the audio would not — measured, not assumed.
        S3 serves ranges, so omitting them here would also make the gateway a
        worse demo than the deployed site.
        """
        from starlette.responses import PlainTextResponse, Response

        site_dir = _PROJECT_ROOT / "site"
        # Read off the URL rather than the path params: two route shapes reach
        # this handler (a file in site/, and a file one level down), and the URL
        # is the same string both times. Nothing is percent-decoded on the way,
        # so an encoded traversal stays an ordinary filename that simply is not
        # there — it 404s on is_file() below rather than escaping the checks.
        rel = request.url.path.lstrip("/")

        target = (site_dir / rel).resolve()
        try:
            inside = target.relative_to(site_dir.resolve())
        except ValueError:
            return PlainTextResponse("Not found", status_code=404)
        if not _is_servable_site_path(inside):
            return PlainTextResponse("Not found", status_code=404)
        if not target.is_file():
            return PlainTextResponse("Not found", status_code=404)

        body = target.read_bytes()
        media_type = SITE_ASSET_TYPES[target.suffix.lower()]
        headers = {
            "Cache-Control": "public, max-age=3600",
            # Advertised unconditionally: a browser that gets no Accept-Ranges
            # treats the resource as unseekable even if a Range would have
            # worked, and the whole file is already in memory either way.
            "Accept-Ranges": "bytes",
        }

        start, end = _parse_byte_range(request.headers.get("range"), len(body))
        if start is None:
            return Response(body, media_type=media_type, headers=headers)
        headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
        return Response(
            body[start : end + 1],
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    async def pricing_drift(self, request: Request) -> HTMLResponse:
        """Report provider mappings with no price, and prices nothing uses.

        Rendered fresh on each request from the live registry and the pricing
        table the cost tracker bills from, so editing pricing.yaml and reloading
        shows the new coverage without a restart.
        """
        report = audit_pricing(self.model_registry, self.cost_tracker.pricing_config)
        return HTMLResponse(
            render_drift_page(
                report,
                self._pricing_path,
                embed=_is_embedded(request),
                unpriced_mappings_blocked=(
                    self._app_config.deployment_profile == "production"
                ),
            )
        )

    async def catalog_drift(self, request: Request) -> HTMLResponse:
        """Report the three-way gap between declared, described, and observed models.

        The catalogue and the registry are separate files that nothing forces to
        agree, and every way they disagree fails quietly: /admin/catalog answers
        for models no mapping can reach, a routed model with no catalogue entry
        reports ``capabilities: []`` which reads as *none* rather than *unknown*,
        and traffic can name a model the registry never declared.

        Usage records are passed in so the report can separate a model that is
        declared and idle from one that is carrying traffic — and flag the
        reverse, traffic with no declaration, which is shadow AI observed rather
        than surveyed. Rendered fresh per request from the live registry, so
        editing either file and reloading shows the new coverage.
        """
        # Fleet-wide within the caller's tenant. A model served by another task
        # must remain visible, but one tenant's shadow traffic is not another
        # tenant's operational data.
        report = audit_catalog(
            self.model_registry,
            self._catalog,
            await self._tenant_records(request),
        )
        return HTMLResponse(
            render_catalog_drift_page(
                report,
                self._config_path,
                self._catalog_path,
                embed=_is_embedded(request),
            )
        )

    async def production_checklist(self, request: Request) -> HTMLResponse:
        """Report whether this deployment is ready to carry real traffic.

        Every check behind this page covers something that fails quietly — an
        unpriced mapping being unavailable in production, LOG_ONLY auth
        admitting every request, or a retired model id — so the state is only
        visible if something asks. Run fresh per request from the live config,
        so a fix shows on reload.

        Hidden in demo mode: ``run_checklist`` returns a did-not-run report and
        the page explains why, rather than listing failures that are correct for
        a demo and would train operators to ignore it.
        """
        report = await run_checklist(
            app_config=self._app_config,
            model_registry=self.model_registry,
            pricing_config=self.cost_tracker.pricing_config,
            provider_configs=self._provider_configs,
            persistence=self._persistence,
            api_key_service=self._api_key_service,
            project_ids=list(await self._all_projects()),
        )
        return HTMLResponse(
            render_checklist_page(report, embed=_is_embedded(request))
        )

    # ------------------------------------------------------------------
    # GET /admin/semantic-cache  |  DELETE /admin/semantic-cache
    # ------------------------------------------------------------------

    async def semantic_cache_stats(self, request: Request) -> JSONResponse:
        """Counters for the semantic cache, and which projects opted in.

        ``available`` distinguishes "no embedder, so the cache cannot run" from
        "running and returning no hits" — the two look identical from a hit rate
        of 0.0 and have completely different fixes. ``rejected_by_literals`` is
        surfaced for the same reason: a high value means the similarity
        threshold is admitting near-misses and only the literal guard is
        catching them, which is a signal to raise the threshold rather than a
        healthy state.
        """
        cache = self._semantic_cache
        if cache is None:
            return JSONResponse({
                "available": False,
                "reason": "no semantic cache wired into this gateway",
            })

        tenant_id = _request_tenant_id(request)
        opted_in = sorted(
            p.project_id
            for p in (await self._all_projects(tenant_id)).values()
            if p.semantic_cache_enabled
        )
        entries = sum(
            cache.entry_count(project_id, tenant_id=tenant_id)
            for project_id in opted_in
        )
        stats = cache.stats_for_scope(tenant_id=tenant_id)
        return JSONResponse({
            "available": cache.enabled,
            "reason": None if cache.enabled else "no embedder (check AXON_SEMANTIC_CACHE and AWS credentials)",
            "default_threshold": cache.threshold,
            "entries": entries,
            "projects_enabled": opted_in,
            "stats": stats.as_dict(),
        })

    async def invalidate_semantic_cache(self, request: Request) -> JSONResponse:
        """Drop cached entries, for one project (``?project_id=``) or all.

        A semantic cache cannot detect its own staleness: the documents or
        prompts behind an answer change with nothing observable in the request,
        so the only correct response to "these answers are wrong now" is an
        operator clearing it. Without this the alternative is a restart.
        """
        cache = self._semantic_cache
        if cache is None:
            return JSONResponse(
                {"error": {"type": "not_available",
                           "message": "no semantic cache wired into this gateway"}},
                status_code=404,
            )
        project_id = request.query_params.get("project_id")
        tenant_id = _request_tenant_id(request)
        if project_id is not None:
            removed = cache.invalidate(
                project_id,
                tenant_id=tenant_id,
            )
        else:
            projects = await self._all_projects(tenant_id)
            removed = sum(
                cache.invalidate(
                    current_project_id,
                    tenant_id=tenant_id,
                )
                for current_project_id in projects
            )
        logger.info(
            "semantic cache invalidated: scope=%s removed=%d",
            project_id or "all", removed,
        )
        return JSONResponse({
            "scope": project_id or "all",
            "removed": removed,
        })


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, returning None on failure."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _is_embedded(request: Request) -> bool:
    """Whether this page is being rendered inside the dashboard shell.

    An explicit ``?embed=1`` rather than sniffing ``Sec-Fetch-Dest: iframe``:
    the dashboard controls the URL it frames, while the header is not sent by
    every browser and would silently strip the ribbon from anyone else's
    legitimate embed -- leaving them a page with no way back.

    Default false, so the standalone URLs an operator has bookmarked or been
    sent in an alert keep their full chrome.
    """
    return request.query_params.get("embed") == "1"


# ------------------------------------------------------------------
# Route factory
# ------------------------------------------------------------------


def create_admin_routes(admin_api: AdminAPI) -> list[Route]:
    """Return Starlette Route objects for the admin API."""
    return [
        Route("/", admin_api.landing_page, methods=["GET"]),
        Route("/admin/dashboard", admin_api.dashboard, methods=["GET"]),
        Route("/admin/static/{path:path}", admin_api.static_asset, methods=["GET"]),
        Route("/admin/architecture", admin_api.architecture, methods=["GET"]),
        Route("/admin/pricing-drift", admin_api.pricing_drift, methods=["GET"]),
        Route("/admin/catalog-drift", admin_api.catalog_drift, methods=["GET"]),
        Route("/admin/production-checklist", admin_api.production_checklist, methods=["GET"]),
        Route("/admin/overview", admin_api.overview, methods=["GET"]),
        Route("/admin/projects", admin_api.list_projects, methods=["GET"]),
        Route("/admin/projects", admin_api.create_project, methods=["POST"]),
        Route("/admin/projects/{id}/members", admin_api.add_member, methods=["POST"]),
        Route("/admin/projects/{id}/members/{user_id}", admin_api.remove_member, methods=["DELETE"]),
        Route("/admin/projects/{id}/models", admin_api.list_project_models, methods=["GET"]),
        Route("/admin/projects/{id}/models", admin_api.add_project_model, methods=["POST"]),
        Route("/admin/projects/{id}/models/{model_name}", admin_api.remove_project_model, methods=["DELETE"]),
        Route("/admin/projects/{id}", admin_api.get_project, methods=["GET"]),
        Route("/admin/projects/{id}", admin_api.update_project, methods=["PUT"]),
        Route("/admin/usage", admin_api.usage, methods=["GET"]),
        Route("/admin/usage/export", admin_api.usage_export, methods=["GET"]),
        Route(
            "/admin/usage/exports/{job_id}",
            admin_api.usage_export_status,
            methods=["GET"],
        ),
        Route(
            "/admin/usage/exports/{job_id}/download",
            admin_api.usage_export_download,
            methods=["GET"],
        ),
        Route("/admin/users", admin_api.list_users, methods=["GET"]),
        Route("/admin/users/{id:path}/allowed-models", admin_api.set_user_allowed_models, methods=["PUT"]),
        Route("/admin/users/{id:path}/budget", admin_api.set_user_budget, methods=["PUT"]),
        Route("/admin/users/{id:path}/efficiency", admin_api.user_efficiency, methods=["GET"]),
        Route("/admin/users/{id:path}", admin_api.get_user, methods=["GET"]),
        Route("/admin/catalog", admin_api.catalog, methods=["GET"]),
        Route(
            "/admin/runtime-config",
            admin_api.runtime_config,
            methods=["GET"],
        ),
        Route("/admin/models", admin_api.list_models, methods=["GET"]),
        Route("/admin/models", admin_api.create_model, methods=["POST"]),
        Route("/admin/models/{name}", admin_api.update_model, methods=["PUT"]),
        Route("/admin/models/{name}", admin_api.delete_model, methods=["DELETE"]),
        Route("/admin/policies", admin_api.list_policies, methods=["GET"]),
        Route("/admin/policies", admin_api.create_policy, methods=["POST"]),
        Route("/admin/health", admin_api.health, methods=["GET"]),
        Route("/admin/traces", admin_api.traces, methods=["GET"]),
        Route("/admin/efficiency", admin_api.efficiency_overview, methods=["GET"]),
        Route("/admin/projects/{id}/efficiency", admin_api.project_efficiency, methods=["GET"]),
        Route("/admin/semantic-cache", admin_api.semantic_cache_stats, methods=["GET"]),
        Route("/admin/semantic-cache", admin_api.invalidate_semantic_cache, methods=["DELETE"]),
    ]


def create_site_routes(admin_api: AdminAPI) -> list[Route]:
    """Return the catch-all routes for the marketing site's static files.

    Separate from ``create_admin_routes`` because ``/{path}`` is a bare single
    segment and Starlette matches in order: registered alongside the admin
    routes it would shadow ``/chat``, ``/playground`` and ``/routing``. Keeping
    it in its own factory makes "must be appended last" something the call site
    shows rather than something a comment asks you to remember.

    Two patterns rather than a ``{path:path}`` convertor: that one also matches
    ``/admin/projects`` and every other multi-segment route, which would make
    the ordering requirement above cover the whole application rather than three
    single-segment pages. Depth is bounded by the pattern, and the handler
    re-checks it anyway.
    """
    return [
        Route("/{path}", admin_api.site_asset, methods=["GET"]),
        Route("/{directory}/{path}", admin_api.site_asset, methods=["GET"]),
    ]
