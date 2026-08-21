"""Pre-production readiness checklist, run against the live configuration.

Every check here exists because the thing it checks fails *quietly*. The gateway
starts, serves traffic and returns 200s in all of these states — the config is
wrong in a way no request surfaces:

* an unpriced mapping bills $0.00 in development; production excludes it from
  routing, which can silently narrow a model's providers or remove the model;
* a retired or aliased model id serves a different model than the one named, or
  fails over silently;
* ``AXON_AUTH_MODE=LOG_ONLY`` admits every unauthenticated request while logging
  that it would have denied them;
* canonical tenant identity disabled trusts credential claims instead of
  resolving server-held membership and project grants;
* demo data seeded in production shows fabricated spend on the real dashboard;
* DynamoDB disabled or unreachable drops every write, by design, without raising;
* legacy API-key metadata scopes do not constrain ``/v1`` chat, canonical
  service-principal authority can drift from key metadata, and a key issued
  without ``expires_at`` is valid until revoked.

None of these raise, so none of them appear in a log an operator greps after an
incident. A checklist is the format that fits: the question being answered is not
"is anything broken right now" but "is this deployment ready to carry real
traffic", and that has to be answerable before the traffic arrives.

**Production only.** :func:`should_run` gates the whole page off when
``AXON_LOAD_DEMO_DATA=true``. A demo deliberately runs with no credentials,
LOG_ONLY auth and seeded data — the exact configuration this checklist is meant
to fail — so rendering it there would show a wall of red that is correct for a
demo and teaches operators to ignore the page. It also makes live outbound calls,
which a demo should not.

Checks are reported rather than used as a boot gate. Production's priced-mapping
router guard is the one enforcement boundary: it rejects unpriced mappings
regardless of this page. The checklist still fails them because a configured
model that is unavailable is not launch-ready.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from src.gateway.admin.model_availability import (
    AvailabilityReport,
    check_model_availability,
    should_run as availability_should_run,
)
from src.gateway.admin.pricing_drift import PricingDriftReport, audit_pricing
from src.gateway.config import AppConfig
from src.gateway.model_registry import ModelRegistry
from src.gateway.models import TokenPricing
from src.gateway.provider_config import ProviderConfig
from .page_style import (
    EMBED_STYLE, FAVICON, ribbon,
    BASE_STYLE, BORDER, ERR, INFO, OK, SURFACE, TEXT_DIM, TEXT_HEADING, UNKNOWN, WARN,
)


class Status(str, Enum):
    """Outcome of a single check.

    ``UNKNOWN`` is deliberately distinct from ``PASS``. A check that could not
    run has not passed, and collapsing the two is how an expired credential or a
    blocked egress route turns into a green checklist.
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


# Severity order for sorting: unresolved and worst first, so the page opens on
# whatever needs attention rather than on a list of things already fine.
_SEVERITY = {Status.FAIL: 0, Status.WARN: 1, Status.UNKNOWN: 2, Status.PASS: 3}


@runtime_checkable
class PersistenceProbe(Protocol):
    """The part of DynamoPersistence this module needs.

    A Protocol rather than the concrete class: the checklist only reads a health
    probe, and depending on the full persistence layer would drag boto3 into a
    module whose job is to report on configuration.
    """

    @property
    def enabled(self) -> bool: ...

    async def health_status(self) -> dict: ...


@runtime_checkable
class KeyProbe(Protocol):
    """The part of APIKeyService this module needs: a per-project key listing.

    A Protocol for the same reason as :class:`PersistenceProbe` — the checklist
    reports on key configuration and has no business holding something that can
    mint or revoke one.
    """

    async def list_keys(self, project_id: str) -> list: ...


@dataclass(frozen=True)
class CheckResult:
    """One line on the checklist."""

    key: str
    title: str
    status: Status
    # What was found. States the observation, not the remedy.
    summary: str
    # Why it matters and what to do. Empty on a pass — a green line needs no
    # instructions, and filling every row with advice buries the rows that do.
    detail: str = ""
    # Where the fix goes: a config file, an env var, an admin page.
    fix: str = ""
    # Supporting rows, rendered as a nested table when present.
    rows: list[tuple[str, str]] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """True for a state that should be resolved before production traffic."""
        return self.status is Status.FAIL


@dataclass
class ChecklistReport:
    """The full checklist for one deployment."""

    checks: list[CheckResult] = field(default_factory=list)
    # True when the checklist itself was skipped (demo mode).
    did_not_run: bool = False
    # Set when a live provider check ran, for the page to describe its coverage.
    availability: AvailabilityReport | None = None

    def count(self, status: Status) -> int:
        return sum(1 for c in self.checks if c.status is status)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is Status.FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is Status.WARN]

    @property
    def ready(self) -> bool:
        """True when nothing is failing.

        Warnings do not block. They are states a deployment can reasonably ship
        with — an unchecked provider, a partially-priced catalogue — where the
        operator needs to know, not to be stopped.
        """
        return not self.failures

    @property
    def unresolved(self) -> int:
        return len(self.failures) + len(self.warnings) + self.count(Status.UNKNOWN)


def should_run(app_config: AppConfig) -> bool:
    """Whether this deployment should render the checklist at all.

    Off in demo mode, where every check would fail correctly and mean nothing.
    """
    return not app_config.load_demo_data


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_pricing(
    report: PricingDriftReport,
    *,
    unpriced_mappings_blocked: bool = False,
) -> CheckResult:
    """Every routable mapping bills at a real rate.

    FAIL rather than WARN when a model has no priced provider at all: that model
    bills $0.00 outright, so spend never accrues and a budget cap on it can never
    trigger. A partially-priced model still bills on its priced provider, so it
    warns instead.
    """
    unpriced = len(report.unpriced)
    fully = len(report.models_fully_unpriced)

    if not unpriced:
        extra = ""
        if report.orphans:
            n = len(report.orphans)
            extra = f" {n} unused pricing entr{'ies' if n != 1 else 'y'} — housekeeping only."
        return CheckResult(
            key="pricing",
            title="Token pricing covers every provider mapping",
            status=Status.PASS,
            summary=f"All {report.total_mappings} mappings priced.{extra}",
        )

    rows = [
        (f"{e.provider} / {e.model_id}", e.model)
        for e in report.unpriced
    ]
    if fully:
        status = Status.FAIL
        if unpriced_mappings_blocked:
            summary = (
                f"{unpriced} of {report.total_mappings} mappings unpriced; "
                f"{fully} configured model{'s are' if fully != 1 else ' is'} "
                "unavailable in production."
            )
            detail = (
                "Production routing fails closed by excluding every unpriced "
                "mapping from model listing, direct routing, smart routing, "
                "streaming, and ensembles. Add verified or contracted rates "
                "before treating the affected models as launch-ready."
            )
        else:
            summary = (
                f"{unpriced} of {report.total_mappings} mappings unpriced; "
                f"{fully} model{'s' if fully != 1 else ''} "
                f"{'have' if fully != 1 else 'has'} no priced provider at all."
            )
            detail = (
                f"Requests to {'those models' if fully != 1 else 'that model'} bill at "
                "$0.00. Project spend never accrues, so budget blocks and quota alerts "
                "cannot fire — enforcement fails open, not closed."
            )
    else:
        status = Status.WARN
        summary = (
            f"{unpriced} of {report.total_mappings} mappings unpriced, but every "
            "model has at least one priced provider."
        )
        if unpriced_mappings_blocked:
            detail = (
                "Production excludes the unpriced mappings, so billing remains "
                "fail-closed but routing and failover capacity are narrower than "
                "the model configuration advertises."
            )
        else:
            detail = (
                "Traffic that lands on an unpriced provider is billed at $0.00, so "
                "spend under-counts whenever routing or failover picks it. Smart "
                "routing also scores those candidates on an estimate rather than a "
                "measured cost."
            )

    return CheckResult(
        key="pricing",
        title="Token pricing covers every provider mapping",
        status=status,
        summary=summary,
        detail=detail,
        fix="Add the missing rates to config/pricing.yaml — see the Pricing page for a paste-ready fragment.",
        rows=rows,
    )


def check_model_ids(report: AvailabilityReport | None) -> CheckResult:
    """Pinned model ids still exist at their providers.

    WARN rather than FAIL even when ids are missing. A list call cannot
    distinguish a retired id from an alias the provider still honours, and the
    aliased case keeps serving traffic — so this reports a discrepancy to
    investigate, not a proven breakage. Overstating it would be the same error as
    trusting the list in the first place.
    """
    if report is None or report.did_not_run:
        return CheckResult(
            key="model_ids",
            title="Pinned model ids still exist at their providers",
            status=Status.UNKNOWN,
            summary="Live provider check did not run.",
            detail=(
                "Model ids are pinned in config/models.yaml and providers retire "
                "and rename them independently. Without this check, a retired id "
                "surfaces as a failover and an aliased one not at all."
            ),
            fix="Set AXON_CHECK_MODEL_AVAILABILITY=true to enable the live check.",
        )

    if not report.checked_providers:
        return CheckResult(
            key="model_ids",
            title="Pinned model ids still exist at their providers",
            status=Status.UNKNOWN,
            summary="No provider catalogue could be read.",
            detail=(
                "Every provider was either unconfigured, unreachable, or has no "
                "catalogue endpoint wired. Nothing was verified either way."
            ),
            rows=[(e.provider, e.reason) for e in report.errors],
            fix="Check provider credentials and outbound network access.",
        )

    checked_note = (
        f"Checked {report.total_checked} mapping"
        f"{'s' if report.total_checked != 1 else ''} across "
        f"{len(report.checked_providers)} provider"
        f"{'s' if len(report.checked_providers) != 1 else ''} "
        f"({', '.join(report.checked_providers)})."
    )
    if report.unchecked_mappings:
        checked_note += (
            f" {report.unchecked_mappings} mapping"
            f"{'s' if report.unchecked_mappings != 1 else ''} on "
            f"{', '.join(sorted(report.unsupported))} not checked."
        )

    if not report.unlisted:
        return CheckResult(
            key="model_ids",
            title="Pinned model ids still exist at their providers",
            status=Status.PASS,
            summary=f"Every checked id is listed by its provider. {checked_note}",
        )

    rows = []
    for entry in report.unlisted:
        hint = f" — closest listed: {entry.suggestion}" if entry.suggestion else ""
        rows.append((f"{entry.provider} / {entry.model_id}", f"{entry.model}{hint}"))

    n = len(report.unlisted)
    return CheckResult(
        key="model_ids",
        title="Pinned model ids still exist at their providers",
        status=Status.WARN,
        summary=f"{n} pinned id{'s are' if n != 1 else ' is'} not in the provider's own model list. {checked_note}",
        detail=(
            "Two possibilities, and a list call cannot tell them apart: the id is "
            "retired (requests fail and the router fails over), or it is an "
            "undocumented alias the provider still honours — which answers 200 "
            "while serving a different model, and bills $0.00 because an alias "
            "appears on no price list. Confirm with one real completion and check "
            "which model the response reports."
        ),
        fix="Update the model_id in config/models.yaml, and add its rate to config/pricing.yaml.",
        rows=rows,
    )


def check_provider_credentials(
    model_registry: ModelRegistry,
    provider_configs: dict[str, ProviderConfig],
) -> CheckResult:
    """Every provider the registry routes to has credentials loaded.

    ``load_provider_configs`` drops providers with no key without raising, so a
    missing credential is invisible until a request routes there. FAIL when a
    model has no credentialled provider at all — that model cannot serve a single
    request, and nothing says so until one arrives.
    """
    # bedrock and bedrock-mantle authenticate through the boto3 credential chain
    # (instance role, env, profile) rather than providers.yaml, so their absence
    # from provider_configs is normal and says nothing about whether they work.
    aws_native = {"bedrock", "bedrock-mantle"}

    missing: list[tuple[str, str]] = []
    dead_models: list[str] = []
    providers_used: set[str] = set()

    for model_name in sorted(model_registry.models):
        model_config = model_registry.models[model_name]
        usable = 0
        for mapping in model_config.providers:
            providers_used.add(mapping.provider)
            if mapping.provider in aws_native or mapping.provider in provider_configs:
                usable += 1
        if model_config.providers and usable == 0:
            dead_models.append(model_name)

    for provider in sorted(providers_used - aws_native):
        if provider not in provider_configs:
            count = sum(
                1
                for m in model_registry.models.values()
                for p in m.providers
                if p.provider == provider
            )
            missing.append((provider, f"{count} mapping{'s' if count != 1 else ''}"))

    configured = sorted((providers_used & set(provider_configs)) | (providers_used & aws_native))

    if not missing:
        return CheckResult(
            key="credentials",
            title="Every routed provider has credentials",
            status=Status.PASS,
            summary=f"All {len(providers_used)} providers in the registry are configured.",
        )

    if dead_models:
        status = Status.FAIL
        summary = (
            f"{len(missing)} provider{'s' if len(missing) != 1 else ''} unconfigured, "
            f"leaving {len(dead_models)} model{'s' if len(dead_models) != 1 else ''} "
            "with no usable provider."
        )
        detail = (
            "Those models cannot serve a request. Nothing reports it at startup — "
            "providers without credentials are dropped silently — so the first "
            "signal is a failed request in production. Unusable: "
            + ", ".join(dead_models)
            + "."
        )
    else:
        status = Status.WARN
        summary = (
            f"{len(missing)} provider{'s' if len(missing) != 1 else ''} in the "
            "registry have no credentials, but every model has an alternative."
        )
        detail = (
            "Routing and failover will skip these, so the catalogue is narrower "
            "than models.yaml describes and capacity is lower than it looks under "
            f"load. Configured: {', '.join(configured)}."
        )

    return CheckResult(
        key="credentials",
        title="Every routed provider has credentials",
        status=status,
        summary=summary,
        detail=detail,
        fix="Set the provider's API key env var, or remove the mapping from config/models.yaml.",
        rows=missing,
    )


def check_auth_mode(app_config: AppConfig) -> CheckResult:
    """Authentication is enforced rather than logged.

    ``LOG_ONLY`` is the one check that is unambiguously a production FAIL: the
    gateway accepts every unauthenticated request and writes a line saying it
    would have denied it. The log makes it look handled.
    """
    if app_config.auth_mode == "ENFORCE":
        return CheckResult(
            key="auth_mode",
            title="API authentication is enforced",
            status=Status.PASS,
            summary="AXON_AUTH_MODE=ENFORCE — unauthenticated requests are rejected.",
        )

    return CheckResult(
        key="auth_mode",
        title="API authentication is enforced",
        status=Status.FAIL,
        summary=f"AXON_AUTH_MODE={app_config.auth_mode} — authentication is logged, not enforced.",
        detail=(
            "Every unauthenticated request is served, and admin RBAC is advisory: "
            "the gateway records what it would have denied and then allows it. The "
            "audit log fills with denials that did not happen, which reads like "
            "the control is working."
        ),
        fix="Unset AXON_AUTH_MODE, or set it to ENFORCE. ENFORCE is the default — this value was set explicitly.",
    )


def check_canonical_identity(app_config: AppConfig) -> CheckResult:
    """Tenant roles and project grants come from durable server-held state."""
    if app_config.canonical_identity_required:
        return CheckResult(
            key="canonical_identity",
            title="Tenant membership is authoritative",
            status=Status.PASS,
            summary=(
                "AXON_REQUIRE_CANONICAL_IDENTITY=true — credentials resolve "
                "through durable tenant membership."
            ),
        )

    return CheckResult(
        key="canonical_identity",
        title="Tenant membership is authoritative",
        status=Status.FAIL,
        summary=(
            "AXON_REQUIRE_CANONICAL_IDENTITY=false — legacy claim-based "
            "authority is still enabled."
        ),
        detail=(
            "OIDC role, tenant, and project claims can reach legacy authorization "
            "without an active server-held membership check. SCIM deprovisioning "
            "therefore cannot reliably revoke an existing credential."
        ),
        fix=(
            "Provision tenant principal records in DynamoDB, test every principal "
            "and project, then set AXON_REQUIRE_CANONICAL_IDENTITY=true."
        ),
    )


async def check_api_keys(
    keys: KeyProbe | None,
    project_ids: list[str],
) -> CheckResult:
    """API-key authorization and expiry are understood before production use.

    Three properties of the key model can fail quietly:

    1. **Legacy metadata scopes do not constrain chat.** In legacy identity
       mode, ``AuthMiddleware`` puts ``key_record.scopes`` on the
       ``RequestContext``, but the chat path does not consult them. The key's
       project model and budget settings are the effective bounds.

    2. **Canonical authority lives on the service principal.** In canonical
       identity mode, mapped ``/api`` and ``/v1`` data-plane routes use the
       resolved server-held service principal's action scopes and explicit
       project grant. This checklist lists key records but cannot resolve and
       compare their principal records, so it cannot prove that relationship is
       present and consistent.

    3. **``expires_at`` is optional and defaults to none.** ``issue_key`` takes
       ``expires_at: datetime | None = None`` and the admin route only sets it
       when the caller supplies one, so the default key is valid forever. There
       is no maximum age and no rotation reminder: revocation is the only way a
       key ever stops working. ``rotate_key`` copies the old ``expires_at``
       through, so rotating a non-expiring key yields another one.

    WARN, not FAIL, whenever live keys exist. The checklist cannot prove
    key/principal consistency, and a non-expiring key may be deliberate when a
    reliable revocation process exists.
    """
    if keys is None:
        return CheckResult(
            key="api_keys",
            title="API key authorization mode and expiry",
            status=Status.UNKNOWN,
            summary="No API key service available to inspect.",
            detail=(
                "The checklist could not enumerate API-key metadata or compare "
                "keys with canonical service-principal records, so authorization "
                "consistency and expiry are unverified."
            ),
            fix="Pass the gateway's APIKeyService to run_checklist.",
        )

    try:
        found = []
        for pid in project_ids:
            found.extend(await keys.list_keys(pid))
    except Exception as exc:
        return CheckResult(
            key="api_keys",
            title="API key authorization mode and expiry",
            status=Status.UNKNOWN,
            summary=f"Could not list API keys ({type(exc).__name__}).",
            fix="Check persistence connectivity and credentials.",
        )

    live = [k for k in found if not getattr(k, "revoked", False)]
    if not live:
        return CheckResult(
            key="api_keys",
            title="API key authorization mode and expiry",
            status=Status.PASS,
            summary=(
                f"No live API keys across {len(project_ids)} "
                f"project{'s' if len(project_ids) != 1 else ''}."
            ),
        )

    never_expire = [k for k in live if getattr(k, "expires_at", None) is None]

    rows: list[tuple[str, str]] = []
    for k in live:
        scopes = getattr(k, "scopes", []) or []
        expiry = getattr(k, "expires_at", None)
        rows.append((
            f"{getattr(k, 'project_id', '?')} / {getattr(k, 'name', '?')}",
            f"key_metadata_scopes={', '.join(scopes) if scopes else 'none'} · "
            f"expires={expiry.date().isoformat() if expiry else 'never'}",
        ))

    n = len(live)
    detail = (
        "Legacy mode: API-key metadata scopes do not constrain /v1 chat; the "
        "key's project allowed_models and budget_limit are the effective bounds. "
        "Canonical mode: mapped /api and /v1 data-plane routes are governed by "
        "the resolved server-held service-principal action scopes and explicit "
        "project grant, not the key record's scope list. This checklist only "
        "lists key records; it does not resolve or compare their principal "
        "records, so key/principal consistency is unverified."
    )
    if never_expire:
        detail += (
            f" {len(never_expire)} of {n} live key"
            f"{'s' if n != 1 else ''} never expire: expires_at defaults to "
            "none, there is no maximum age, and rotate_key copies the old "
            "value through — so revocation is the only thing that ever stops "
            "them."
        )

    return CheckResult(
        key="api_keys",
        title="API key authorization mode and expiry",
        status=Status.WARN,
        summary=(
            f"{n} live key{'s' if n != 1 else ''}; "
            f"{len(never_expire)} without an expiry. "
            "Key/principal consistency is unverified."
        ),
        detail=detail,
        fix=(
            "Reconcile each live key with its canonical service-principal "
            "record and set expires_at when issuing. In legacy mode, use "
            "per-project allowed_models and budget_limit rather than key "
            "metadata scopes to bound chat access."
        ),
        rows=rows,
    )


def check_demo_data(app_config: AppConfig, environ: dict[str, str]) -> CheckResult:
    """Demo seed data is not loaded.

    Reached only when the checklist is rendered, which already requires
    ``load_demo_data`` to be False. Production still requires an explicit value
    so an orchestrator, Compose override, or developer entrypoint cannot change
    the effective profile without leaving evidence in the deployment contract.
    """
    if app_config.load_demo_data:  # pragma: no cover - checklist is gated off here
        return CheckResult(
            key="demo_data",
            title="Demo seed data is not loaded",
            status=Status.FAIL,
            summary="AXON_LOAD_DEMO_DATA=true — fabricated projects and spend are loaded.",
            detail=(
                "The dashboard shows seeded usage and costs alongside real traffic, "
                "with nothing distinguishing them."
            ),
            fix="Set AXON_LOAD_DEMO_DATA=false.",
        )

    if "AXON_LOAD_DEMO_DATA" not in environ:
        return CheckResult(
            key="demo_data",
            title="Demo seed data is not loaded",
            status=Status.WARN,
            summary="Not loaded, but AXON_LOAD_DEMO_DATA is unset rather than explicitly false.",
            detail=(
                "The standalone image defaults this to false, but evaluation "
                "Compose and the developer entrypoint explicitly enable demo "
                "data. Production should bind the value rather than inherit an "
                "entrypoint or orchestration default."
            ),
            fix="Set AXON_LOAD_DEMO_DATA=false explicitly in the deployment environment.",
        )

    return CheckResult(
        key="demo_data",
        title="Demo seed data is not loaded",
        status=Status.PASS,
        summary="AXON_LOAD_DEMO_DATA is explicitly false.",
    )


async def check_persistence(persistence: PersistenceProbe | None) -> CheckResult:
    """State is persisted and the store is reachable.

    Writes are swallowed by design — a provider call should not 500 because
    DynamoDB hiccuped — which means an unreachable table loses every record with
    no request-visible symptom. FAIL when enabled but unreachable (writes are
    being dropped right now); WARN when disabled (in-memory only, so a restart
    loses billing and usage history).
    """
    if persistence is None or not getattr(persistence, "enabled", False):
        return CheckResult(
            key="persistence",
            title="State survives a restart",
            status=Status.WARN,
            summary="DynamoDB persistence is disabled — state is in-memory only.",
            detail=(
                "Cost records, usage history, API keys and config changes are lost "
                "on restart, and are not shared between instances — so per-project "
                "budget counters reset on deploy and are computed per-replica "
                "behind a load balancer."
            ),
            fix="Set LLM_ROUTER_DYNAMODB_ENABLED=true and AXON_DYNAMODB_TABLE.",
        )

    try:
        status = await persistence.health_status()
    except Exception as exc:
        return CheckResult(
            key="persistence",
            title="State survives a restart",
            status=Status.UNKNOWN,
            summary=f"Persistence health probe failed ({type(exc).__name__}).",
            fix="Check AWS credentials and region.",
        )

    table = status.get("table", "?")
    reachable = status.get("reachable")
    last_error = status.get("last_write_error")

    if reachable is False:
        return CheckResult(
            key="persistence",
            title="State survives a restart",
            status=Status.FAIL,
            summary=f"DynamoDB table {table} is enabled but not reachable.",
            detail=(
                "Every write is being dropped. Failures are logged and swallowed "
                "so requests still succeed, which means billing and usage data is "
                "disappearing with no request-visible symptom."
            ),
            fix="Verify the table exists in this region and the task role has read/write access.",
        )

    if last_error:
        return CheckResult(
            key="persistence",
            title="State survives a restart",
            status=Status.WARN,
            summary=f"Table {table} is reachable, but a write has been dropped.",
            detail=(
                f"Most recent dropped write: {last_error}. Writes fail without "
                "raising, so this may be intermittent throttling or a permissions "
                "gap on one item type."
            ),
            fix="Check the gateway error log for the underlying DynamoDB exception.",
        )

    return CheckResult(
        key="persistence",
        title="State survives a restart",
        status=Status.PASS,
        summary=f"DynamoDB table {table} is enabled and reachable.",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_checklist(
    *,
    app_config: AppConfig,
    model_registry: ModelRegistry,
    pricing_config: dict[str, dict[str, TokenPricing]],
    provider_configs: dict[str, ProviderConfig],
    persistence: PersistenceProbe | None = None,
    api_key_service: KeyProbe | None = None,
    project_ids: list[str] | None = None,
    environ: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> ChecklistReport:
    """Run every check against the live configuration.

    Returns a report with :attr:`ChecklistReport.did_not_run` set in demo mode
    rather than an empty pass — see the module docstring.
    """
    environ = dict(os.environ) if environ is None else environ

    if not should_run(app_config):
        return ChecklistReport(did_not_run=True)

    availability: AvailabilityReport | None = None
    if availability_should_run(
        load_demo_data=app_config.load_demo_data,
        enabled_override=environ.get("AXON_CHECK_MODEL_AVAILABILITY"),
    ):
        availability = await check_model_availability(
            model_registry,
            provider_configs,
            timeout=timeout,
            bedrock_region=app_config.bedrock_region,
        )

    drift = audit_pricing(model_registry, pricing_config)

    checks = [
        check_auth_mode(app_config),
        check_canonical_identity(app_config),
        check_demo_data(app_config, environ),
        check_provider_credentials(model_registry, provider_configs),
        check_pricing(
            drift,
            unpriced_mappings_blocked=(
                app_config.deployment_profile == "production"
            ),
        ),
        check_model_ids(availability),
        await check_persistence(persistence),
        await check_api_keys(api_key_service, project_ids or []),
    ]

    # Worst first, stable within a severity so the order does not shift between
    # reloads for reasons the operator cannot see.
    checks.sort(key=lambda c: _SEVERITY[c.status])

    return ChecklistReport(checks=checks, availability=availability)


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

_STYLE = BASE_STYLE + (
    # Per-check cards and status pills — the only thing this page adds.
    f".check{{background:{SURFACE};border:1px solid {BORDER};border-radius:16px;"
    f"padding:14px 18px;margin-bottom:10px;border-left:5px solid {BORDER};"
    "box-shadow:0 0 0 1px rgba(214,211,209,0.3),0 1px 3px rgba(0,0,0,0.04)}"
    f".check.fail{{border-left-color:{ERR}}}"
    f".check.warn{{border-left-color:{WARN}}}"
    f".check.pass{{border-left-color:{OK}}}"
    # UNKNOWN is stone, not a status hue: a check that could not run has no
    # result, and coloring it like one is how an expired credential reads as a
    # green checklist.
    f".check.unknown{{border-left-color:{UNKNOWN}}}"
    f".check h3{{margin:0 0 4px;font-size:15px;display:flex;align-items:center;"
    f"gap:9px;color:{TEXT_HEADING}}}"
    ".pill{font-size:10px;font-weight:700;letter-spacing:.07em;padding:3px 8px;"
    f"border-radius:6px;text-transform:uppercase;color:{SURFACE};flex-shrink:0}}"
    f".pill.fail{{background:{ERR}}}.pill.warn{{background:{WARN}}}"
    f".pill.pass{{background:{OK}}}.pill.unknown{{background:{UNKNOWN}}}"
    ".check p{margin:5px 0;font-size:13px;line-height:1.55}"
    f".check .why{{color:{TEXT_DIM}}}"
    f".check .fix{{color:{INFO}}}"
    "table{margin:10px 0 2px;font-size:12px}"
    "th{padding:6px 10px;font-size:11px}"
    "td{padding:5px 10px}"
    f".note{{color:{TEXT_DIM};font-size:12px;line-height:1.6;margin:20px 0 0;"
    f"border-top:1px solid {BORDER};padding-top:14px}}"
)

_PILL = {
    Status.FAIL: "Fail",
    Status.WARN: "Warn",
    Status.PASS: "Pass",
    Status.UNKNOWN: "Unknown",
}


def _esc(value: object) -> str:
    return html.escape(str(value))


def render_checklist_page(report: ChecklistReport, *, embed: bool = False) -> str:
    """Render the checklist as a self-contained HTML page.

    With ``embed``, drop the ribbon and the page framing: the dashboard shell
    supplies both, and two stacked toolbars is the tell that a page was bolted
    on rather than built in.
    """
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        "<title>AxonLLM — Production Readiness</title>",
        FAVICON,
        f"<style>{_STYLE}{EMBED_STYLE if embed else ''}</style></head><body>",
    ]
    if not embed:
        parts.append(
            ribbon("Production Readiness", ("/admin/pricing-drift", "Pricing detail"))
        )
    parts.append('<div class="wrap">')

    if report.did_not_run:
        # Demo mode. Say why there is nothing here, rather than showing an empty
        # list that reads as a pass.
        parts.append(
            '<div class="banner ok"><h1>Not applicable in demo mode.</h1>'
            "<p>This deployment is running with <code>AXON_LOAD_DEMO_DATA=true</code>. "
            "The checklist reports on real credentials, real pricing coverage and "
            "live provider catalogues — a demo deliberately has none of those, so "
            "every check would fail correctly and mean nothing.</p>"
            "<p>Run without demo data to see the real result.</p></div>"
            "</div></body></html>"
        )
        return "".join(parts)

    fails, warns = report.failures, report.warnings
    unknowns = report.count(Status.UNKNOWN)

    if fails:
        n = len(fails)
        parts.append(
            f'<div class="banner fail"><h1>{n} check{"s" if n != 1 else ""} '
            f'{"are" if n != 1 else "is"} failing.</h1>'
            "<p>Each of these is a state the gateway runs in without complaint — it "
            "serves traffic, returns 200s, and writes nothing to a log that would "
            "reveal the problem. They are listed first below.</p>"
            "<p>Nothing here is enforced. The gateway will keep serving in this "
            "configuration; the checklist reports, it does not block.</p></div>"
        )
    elif warns or unknowns:
        parts.append(
            '<div class="banner warn"><h1>No failures, '
            f"{report.unresolved} item{'s' if report.unresolved != 1 else ''} to review.</h1>"
            "<p>Nothing here prevents production traffic. These are states worth "
            "knowing about — a narrower catalogue than configured, coverage this "
            "page could not verify — rather than defects.</p></div>"
        )
    else:
        parts.append(
            '<div class="banner ok"><h1>All checks pass.</h1>'
            "<p>Authentication is enforced, no demo data is loaded, every routed "
            "provider is credentialled, every mapping bills at a real rate, every "
            "pinned model id is live, and state survives a restart.</p></div>"
        )

    parts.append(
        '<div class="stats">'
        f'<div class="stat"><b>{report.count(Status.PASS)}</b><small>passing</small></div>'
        f'<div class="stat"><b>{len(warns)}</b><small>warnings</small></div>'
        f'<div class="stat"><b>{len(fails)}</b><small>failing</small></div>'
        f'<div class="stat"><b>{unknowns}</b><small>unknown</small></div>'
        "</div>"
    )

    for check in report.checks:
        cls = check.status.value
        parts.append(
            f'<div class="check {cls}"><h3><span class="pill {cls}">'
            f"{_PILL[check.status]}</span>{_esc(check.title)}</h3>"
            f"<p>{_esc(check.summary)}</p>"
        )
        if check.detail:
            parts.append(f'<p class="why">{_esc(check.detail)}</p>')
        if check.rows:
            parts.append("<table><tr><th>Item</th><th>Detail</th></tr>")
            for left, right in check.rows:
                parts.append(
                    f"<tr><td><code>{_esc(left)}</code></td><td>{_esc(right)}</td></tr>"
                )
            parts.append("</table>")
        if check.fix:
            parts.append(f'<p class="fix">→ {_esc(check.fix)}</p>')
        parts.append("</div>")

    parts.append(
        '<p class="note">Rendered fresh on each request from the live configuration, '
        "so a fix shows up on reload without a restart. The model-id check makes one "
        "outbound list request per configured provider and never a completion — "
        "listing is free, generating a token is not, and an admin page should not "
        "bill. Set <code>AXON_CHECK_MODEL_AVAILABILITY=false</code> to disable those "
        "calls. This page is hidden in demo mode.</p>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Startup notice
# ---------------------------------------------------------------------------


def format_startup_notice(report: ChecklistReport, url: str) -> str | None:
    """Startup banner for failing checks, or None when there is nothing to say.

    Gated on failures only. Warnings are real but survivable, and a banner that
    prints on every healthy boot is one nobody reads — which is the failure mode
    the whole page exists to avoid.
    """
    if report.did_not_run or not report.failures:
        return None

    rule = "  " + "─" * 68
    n = len(report.failures)
    lines = [
        rule,
        f"  ⚠  PRODUCTION READINESS: {n} check{'s' if n != 1 else ''} failing.",
        "",
    ]
    for check in report.failures:
        lines.append(f"     ✗  {check.title}")
        lines.append(f"        {check.summary}")
    lines += ["", f"     Review:  {url}", rule]
    return "\n".join(lines)
