"""Configuration loading utilities for AxonLLM.

Reads YAML config files and environment variables, producing typed objects.
All functions return sensible defaults when files are missing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.gateway.config import AppConfig, VALID_PROVIDERS
from src.gateway.models import TokenPricing

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DemoSeedData:
    """Demo/seed data loaded from YAML for local development."""

    projects: list[dict] = field(default_factory=list)
    user_budgets: list[dict] = field(default_factory=list)
    usage_seeds: list[dict] = field(default_factory=list)
    policies: list[dict] = field(default_factory=list)
    # The org > business_unit > project > environment tree. Distinct from
    # ``policies``, which is Cedar authorization text; these carry the numeric
    # and model limits the quota enforcer reads on every request.
    policy_nodes: list[dict] = field(default_factory=list)
    unhealthy_providers: list[dict] = field(default_factory=list)
    audit_events: list[dict] = field(default_factory=list)
    api_keys: list[dict] = field(default_factory=list)
    webhook_destinations: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# load_app_config
# ---------------------------------------------------------------------------


def load_app_config() -> AppConfig:
    """Build an AppConfig from environment variables, falling back to defaults."""
    return AppConfig(
        deployment_profile=_load_deployment_profile(),
        aws_region=(
            os.environ.get("AWS_DEFAULT_REGION")
            or os.environ.get("AWS_REGION")
            or "us-east-1"
        ),
        bedrock_region=os.environ.get("AXON_BEDROCK_REGION", "us-east-1"),
        server_host=os.environ.get("AXON_SERVER_HOST", "0.0.0.0"),
        server_port=int(os.environ.get("AXON_SERVER_PORT", "8000")),
        models_config_path=os.environ.get("AXON_MODELS_CONFIG", "config/models.yaml"),
        providers_config_path=os.environ.get("AXON_PROVIDERS_CONFIG", "config/providers.yaml"),
        enabled_providers=_load_enabled_providers(),
        pricing_config_path=os.environ.get("AXON_PRICING_CONFIG", "config/pricing.yaml"),
        demo_seed_config_path=os.environ.get("AXON_DEMO_SEED_CONFIG", "config/demo_seed.yaml"),
        catalog_config_path=os.environ.get("AXON_CATALOG_CONFIG", "config/catalog.yaml"),
        ensemble_config_path=os.environ.get("AXON_ENSEMBLE_CONFIG", "config/ensemble.yaml"),
        spokes_config_path=os.environ.get("AXON_SPOKES_CONFIG", "config/spokes.yaml"),
        load_demo_data=os.environ.get("AXON_LOAD_DEMO_DATA", "false").lower() == "true",
        oidc_issuer=os.environ.get("AXON_OIDC_ISSUER", ""),
        oidc_audience=os.environ.get("AXON_OIDC_AUDIENCE", ""),
        oidc_tenant_claim=os.environ.get(
            "AXON_OIDC_TENANT_CLAIM",
            "custom:tenant_id",
        ),
        oidc_project_claim=os.environ.get(
            "AXON_OIDC_PROJECT_CLAIM",
            "custom:project_id",
        ),
        alb_signer_arn=os.environ.get("AXON_ALB_SIGNER_ARN", ""),
        alb_client_id=os.environ.get("AXON_ALB_CLIENT_ID", ""),
        alb_issuer=os.environ.get("AXON_ALB_ISSUER", ""),
        control_plane_endpoint_mode=os.environ.get(
            "AXON_CONTROL_PLANE_ENDPOINT_MODE",
            "custom-domain",
        ).strip().lower(),
        control_plane_public_url=(
            os.environ.get("AXON_CONTROL_PLANE_URL")
            or os.environ.get("AXON_CONTROL_PLANE_PUBLIC_URL")
            or os.environ.get("AXON_BROWSER_AUTH_PUBLIC_URL")
            or ""
        ),
        cognito_hosted_ui_url=(
            os.environ.get("AXON_COGNITO_HOSTED_UI_URL")
            or os.environ.get("AXON_COGNITO_HOSTED_UI_BASE_URL", "")
        ),
        browser_auth_mode=os.environ.get(
            "AXON_BROWSER_AUTH_MODE",
            "",
        ).strip().lower(),
        browser_auth_client_id=os.environ.get(
            "AXON_BROWSER_AUTH_CLIENT_ID",
            os.environ.get("AXON_OIDC_AUDIENCE", ""),
        ),
        browser_auth_authorization_endpoint=os.environ.get(
            "AXON_BROWSER_AUTH_AUTHORIZATION_ENDPOINT",
            "",
        ),
        browser_auth_token_endpoint=os.environ.get(
            "AXON_BROWSER_AUTH_OAUTH_EXCHANGE_URL",
            "",
        ),
        browser_auth_logout_endpoint=os.environ.get(
            "AXON_BROWSER_AUTH_LOGOUT_ENDPOINT",
            "",
        ),
        browser_auth_redirect_uri=os.environ.get(
            "AXON_BROWSER_AUTH_REDIRECT_URI",
            "",
        ),
        browser_auth_signed_out_uri=os.environ.get(
            "AXON_BROWSER_AUTH_SIGNED_OUT_URI",
            "",
        ),
        browser_session_max_seconds=_load_int(
            "AXON_BROWSER_AUTH_SESSION_TTL_SECONDS",
            _load_int(
                "AXON_BROWSER_SESSION_MAX_SECONDS",
                8 * 60 * 60,
            ),
        ),
        browser_auth_flow_ttl_seconds=_load_int(
            "AXON_BROWSER_AUTH_FLOW_TTL_SECONDS",
            600,
        ),
        auth_mode=_load_auth_mode(),
        canonical_identity_required=os.environ.get(
            "AXON_REQUIRE_CANONICAL_IDENTITY", "false"
        ).lower() == "true",
        durable_persistence_enabled=os.environ.get(
            "LLM_ROUTER_DYNAMODB_ENABLED", "false"
        ).lower() == "true",
        routing_config_signing_mode=os.environ.get(
            "AXON_ROUTING_CONFIG_SIGNING_MODE",
            "disabled",
        ).strip().lower(),
        routing_config_signing_key_arn=os.environ.get(
            "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN",
            "",
        ).strip(),
        semantic_cache_enabled=os.environ.get(
            "AXON_SEMANTIC_CACHE", "false"
        ).lower() == "true",
        semantic_cache_region=os.environ.get(
            "AXON_SEMANTIC_CACHE_REGION", os.environ.get("AXON_BEDROCK_REGION", "us-east-1")
        ),
        semantic_cache_model=os.environ.get("AXON_SEMANTIC_CACHE_MODEL", ""),
        semantic_cache_threshold=_load_semantic_threshold(),
        athena_query_enabled=_load_strict_bool(
            "AXON_ATHENA_QUERY_ENABLED",
            False,
        ),
        athena_query_bindings=os.environ.get(
            "AXON_ATHENA_QUERY_BINDINGS",
            "",
        ),
        athena_query_timeout_seconds=_load_float(
            "AXON_ATHENA_QUERY_TIMEOUT_SECONDS",
            30.0,
        ),
        athena_query_max_rows=_load_int(
            "AXON_ATHENA_QUERY_MAX_ROWS",
            1000,
        ),
        athena_query_max_result_bytes=_load_int(
            "AXON_ATHENA_QUERY_MAX_RESULT_BYTES",
            1024 * 1024,
        ),
        athena_query_max_bytes_scanned=_load_int(
            "AXON_ATHENA_QUERY_MAX_BYTES_SCANNED",
            1024 * 1024 * 1024,
        ),
        athena_query_poll_interval_seconds=_load_float(
            "AXON_ATHENA_QUERY_POLL_INTERVAL_SECONDS",
            0.25,
        ),
        athena_query_project_rpm=_load_int(
            "AXON_ATHENA_QUERY_PROJECT_RPM",
            30,
        ),
        athena_query_principal_rpm=_load_int(
            "AXON_ATHENA_QUERY_PRINCIPAL_RPM",
            10,
        ),
        athena_query_project_concurrency=_load_int(
            "AXON_ATHENA_QUERY_PROJECT_CONCURRENCY",
            5,
        ),
        athena_query_principal_concurrency=_load_int(
            "AXON_ATHENA_QUERY_PRINCIPAL_CONCURRENCY",
            2,
        ),
        athena_query_project_scan_bytes_per_minute=_load_int(
            "AXON_ATHENA_QUERY_PROJECT_SCAN_BYTES_PER_MINUTE",
            5 * 1024 * 1024 * 1024,
        ),
        athena_query_principal_scan_bytes_per_minute=_load_int(
            "AXON_ATHENA_QUERY_PRINCIPAL_SCAN_BYTES_PER_MINUTE",
            2 * 1024 * 1024 * 1024,
        ),
        athena_query_max_datasources_per_tenant=_load_int(
            "AXON_ATHENA_QUERY_MAX_DATASOURCES_PER_TENANT",
            500,
        ),
        control_plane_only=_load_strict_bool(
            "AXON_CONTROL_PLANE_ONLY",
            False,
        ),
    )


def _load_strict_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be 'true' or 'false'")


def _load_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _load_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _load_enabled_providers() -> frozenset[str] | None:
    raw = os.environ.get("AXON_ENABLED_PROVIDERS")
    if raw is None:
        return None
    providers = frozenset(
        provider.strip()
        for provider in raw.split(",")
        if provider.strip()
    )
    if not providers:
        raise ValueError("AXON_ENABLED_PROVIDERS must name at least one provider")
    unknown = providers.difference(VALID_PROVIDERS)
    if unknown:
        raise ValueError(
            "AXON_ENABLED_PROVIDERS contains unknown providers: "
            + ", ".join(sorted(unknown))
        )
    return providers


def _load_deployment_profile() -> str:
    """Load the explicit runtime security profile, rejecting unsafe typos."""
    configured = os.environ.get("AXON_DEPLOYMENT_PROFILE")
    if configured is None:
        # Demo tooling may load config without going through serve_dashboard.py.
        # Every ordinary runtime still defaults to the production contract.
        if os.environ.get("AXON_LOAD_DEMO_DATA", "false").lower() == "true":
            return "development"
        return "production"
    raw = configured.strip().lower()
    if raw not in {"development", "production"}:
        raise ValueError(
            "AXON_DEPLOYMENT_PROFILE must be 'development' or 'production'"
        )
    return raw


def _load_semantic_threshold() -> float | None:
    """Parse AXON_SEMANTIC_CACHE_THRESHOLD, or None to use the module default.

    An unparseable or out-of-range value falls back to None rather than to 0.0:
    a threshold of zero makes every cached entry a match, so the failure mode of
    a typo would be a cache that answers every question with the first response
    it ever stored.
    """
    raw = os.environ.get("AXON_SEMANTIC_CACHE_THRESHOLD", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Unparseable AXON_SEMANTIC_CACHE_THRESHOLD=%r — using the default", raw
        )
        return None
    if not 0.0 < value <= 1.0:
        logger.warning(
            "AXON_SEMANTIC_CACHE_THRESHOLD=%r is outside (0.0, 1.0] — using the default",
            raw,
        )
        return None
    return value


def _load_auth_mode() -> str:
    """Resolve the auth enforcement mode from AXON_AUTH_MODE.

    Defaults to ENFORCE (fail-closed): a deploy that sets nothing gets
    authentication and admin RBAC enforced. Set AXON_AUTH_MODE=LOG_ONLY
    explicitly for local development / demos where you want the gateway to
    run without credentials. An unrecognized value falls back to ENFORCE
    rather than silently disabling auth.
    """
    raw = os.environ.get("AXON_AUTH_MODE", "ENFORCE").strip().upper()
    if raw not in ("ENFORCE", "LOG_ONLY"):
        logger.warning(
            "Unrecognized AXON_AUTH_MODE=%r — falling back to ENFORCE (fail-closed)", raw
        )
        return "ENFORCE"
    if raw == "LOG_ONLY":
        logger.warning(
            "AXON_AUTH_MODE=LOG_ONLY — authentication and admin RBAC are NOT enforced. "
            "Use this only for local development, never in production."
        )
    return raw


# ---------------------------------------------------------------------------
# load_pricing_config
# ---------------------------------------------------------------------------


def load_pricing_config(path: str) -> dict[str, dict[str, TokenPricing]]:
    """Load token pricing from a YAML file.

    Expected format::

        providers:
          openai:
            gpt-4:
              prompt_token_cost: 0.03
              completion_token_cost: 0.06
              cached_token_cost: null      # optional
              image_token_cost: null        # optional
              reasoning_token_cost: null    # optional
              per_request_cost: 0.0         # optional

    Returns an empty dict if the file does not exist.
    Skips malformed entries (missing required fields) with a warning.
    """
    if not Path(path).exists():
        logger.warning("Pricing config not found at %s — using empty pricing", path)
        return {}

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    providers_raw = raw.get("providers", {})
    if not isinstance(providers_raw, dict):
        logger.warning("Pricing config at %s has invalid 'providers' key", path)
        return {}

    result: dict[str, dict[str, TokenPricing]] = {}
    for provider, models in providers_raw.items():
        if not isinstance(models, dict):
            logger.warning("Pricing config: skipping provider '%s' (not a dict)", provider)
            continue
        provider_pricing: dict[str, TokenPricing] = {}
        for model, entry in models.items():
            if not isinstance(entry, dict):
                logger.warning("Pricing config: skipping %s/%s (not a dict)", provider, model)
                continue
            if "prompt_token_cost" not in entry or "completion_token_cost" not in entry:
                logger.warning(
                    "Pricing config: skipping %s/%s (missing required fields)", provider, model
                )
                continue
            provider_pricing[model] = TokenPricing(
                prompt_token_cost=float(entry["prompt_token_cost"]),
                completion_token_cost=float(entry["completion_token_cost"]),
                cached_token_cost=_opt_float(entry.get("cached_token_cost")),
                cache_creation_token_cost=_opt_float(entry.get("cache_creation_token_cost")),
                image_token_cost=_opt_float(entry.get("image_token_cost")),
                reasoning_token_cost=_opt_float(entry.get("reasoning_token_cost")),
                per_request_cost=float(entry.get("per_request_cost", 0.0)),
            )
        if provider_pricing:
            result[provider] = provider_pricing
    return result


# ---------------------------------------------------------------------------
# load_demo_seed_config
# ---------------------------------------------------------------------------


def load_demo_seed_config(path: str) -> DemoSeedData:
    """Load demo seed data from a YAML file.

    Returns empty DemoSeedData if the file does not exist.
    """
    if not Path(path).exists():
        logger.warning("Demo seed config not found at %s — using empty seed data", path)
        return DemoSeedData()

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return DemoSeedData(
        projects=raw.get("projects", []),
        user_budgets=raw.get("user_budgets", []),
        usage_seeds=raw.get("usage_seeds", []),
        policies=raw.get("policies", []),
        policy_nodes=raw.get("policy_nodes", []),
        unhealthy_providers=raw.get("unhealthy_providers", []),
        audit_events=raw.get("audit_events", []),
        api_keys=raw.get("api_keys", []),
        webhook_destinations=raw.get("webhook_destinations", []),
    )


# ---------------------------------------------------------------------------
# load_catalog_config
# ---------------------------------------------------------------------------


def load_catalog_config(path: str, fallback: dict | None = None) -> dict:
    """Load provider model catalog from a YAML file.

    Returns *fallback* (or empty dict) if the file does not exist.
    """
    if not Path(path).exists():
        logger.warning("Catalog config not found at %s — using fallback", path)
        return fallback if fallback is not None else {}

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return raw.get("providers", fallback or {})


# ---------------------------------------------------------------------------
# load_ensemble_config
# ---------------------------------------------------------------------------


def load_ensemble_config(path: str):
    """Load ensemble routing presets from a YAML file.

    Returns an empty (unconfigured) ``EnsembleConfig`` if the file does not
    exist, mirroring the missing-file-with-warning behaviour of the other
    loaders in this module.
    """
    # Function-level import: ``EnsembleConfig`` lives in a sibling module that
    # may be created in a separate task. Importing here avoids import-order
    # coupling at module load time.
    from src.gateway.ensemble_config import EnsembleConfig

    if not Path(path).exists():
        logger.warning("Ensemble config not found at %s — using empty ensemble config", path)
        return EnsembleConfig()

    config = EnsembleConfig()
    config.load(path)
    return config



# ---------------------------------------------------------------------------
# Serialization helpers (for round-trip testing)
# ---------------------------------------------------------------------------


def serialize_pricing_config(pricing: dict[str, dict[str, TokenPricing]]) -> dict:
    """Convert a pricing dict back to a plain dict suitable for YAML dump."""
    result: dict[str, dict[str, dict]] = {}
    for provider, models in pricing.items():
        provider_dict: dict[str, dict] = {}
        for model, tp in models.items():
            entry: dict[str, float | None] = {
                "prompt_token_cost": tp.prompt_token_cost,
                "completion_token_cost": tp.completion_token_cost,
            }
            if tp.cached_token_cost is not None:
                entry["cached_token_cost"] = tp.cached_token_cost
            if tp.cache_creation_token_cost is not None:
                entry["cache_creation_token_cost"] = tp.cache_creation_token_cost
            if tp.image_token_cost is not None:
                entry["image_token_cost"] = tp.image_token_cost
            if tp.reasoning_token_cost is not None:
                entry["reasoning_token_cost"] = tp.reasoning_token_cost
            if tp.per_request_cost != 0.0:
                entry["per_request_cost"] = tp.per_request_cost
            provider_dict[model] = entry
        result[provider] = provider_dict
    return {"providers": result}


def serialize_demo_seed_config(seed_data: DemoSeedData) -> dict:
    """Convert DemoSeedData back to a plain dict suitable for YAML dump."""
    result: dict[str, list] = {}
    if seed_data.projects:
        result["projects"] = seed_data.projects
    if seed_data.user_budgets:
        result["user_budgets"] = seed_data.user_budgets
    if seed_data.usage_seeds:
        result["usage_seeds"] = seed_data.usage_seeds
    if seed_data.policies:
        result["policies"] = seed_data.policies
    if seed_data.policy_nodes:
        result["policy_nodes"] = seed_data.policy_nodes
    if seed_data.unhealthy_providers:
        result["unhealthy_providers"] = seed_data.unhealthy_providers
    if seed_data.audit_events:
        result["audit_events"] = seed_data.audit_events
    if seed_data.api_keys:
        result["api_keys"] = seed_data.api_keys
    if seed_data.webhook_destinations:
        result["webhook_destinations"] = seed_data.webhook_destinations
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _opt_float(value) -> float | None:
    """Convert a value to float or return None."""
    if value is None:
        return None
    return float(value)
