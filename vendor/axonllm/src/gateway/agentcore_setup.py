"""Validated first-adopter configuration for AxonLLM AgentCore."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = 2
MANAGED_COGNITO = "managed-cognito"
EXTERNAL_OIDC = "external-oidc"
IDENTITY_MODES = (MANAGED_COGNITO, EXTERNAL_OIDC)
CUSTOM_DOMAIN = "custom-domain"
CLOUDFRONT = "cloudfront"
CONTROL_PLANE_ENDPOINT_MODES = (CUSTOM_DOMAIN, CLOUDFRONT)
DEFAULT_TENANT_CLAIM = "custom:tenant_id"
DEFAULT_PROJECT_CLAIM = "custom:project_id"
DEFAULT_SAML_LOGIN_PATH = "/admin/dashboard"
DEFAULT_AGENTCORE_PROVIDERS = (
    "anthropic",
    "bedrock",
    "bedrock-mantle",
    "fireworks",
    "google_ai",
    "groq",
    "openai",
    "together",
    "xai",
)
SUPPORTED_AGENTCORE_PROVIDERS = frozenset(
    (
        *DEFAULT_AGENTCORE_PROVIDERS,
        "ai21",
        "azure_openai",
        "cohere",
        "vertex_ai",
    )
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DOMAIN_PREFIX_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
_DOMAIN_NAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}"
    r"[a-z0-9])?\.)+[a-z]{2,63}$"
)
_PREFIX_LIST_PATTERN = re.compile(r"^pl-[0-9a-fA-F]+$")
_HOSTED_ZONE_PATTERN = re.compile(r"^Z[A-Z0-9]+$")
_SAML_LOGIN_PATH_PATTERN = re.compile(
    r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]+$"
)
_IMAGE_PATTERN = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
_ACM_CERTIFICATE_ARN_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):acm:"
    r"(?P<region>[a-z0-9-]+):[0-9]{12}:"
    r"certificate/[0-9a-fA-F-]+$"
)
_SECRETS_MANAGER_ARN_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:"
    r"(?P<region>[a-z0-9-]+):[0-9]{12}:"
    r"secret:[A-Za-z0-9/_+=.@-]{1,512}$"
)
_BEDROCK_ARN_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):bedrock:"
    r"(?P<region>[a-z0-9-]+):(?:[0-9]{12})?:"
    r"(?P<resource_type>foundation-model|inference-profile|"
    r"application-inference-profile|custom-model|provisioned-model|"
    r"imported-model)/[A-Za-z0-9][A-Za-z0-9._:/+-]*$"
)
_IAM_ROLE_ARN_PATTERN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:"
    r"role/[A-Za-z0-9+=,.@_/-]{1,512}$"
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "access_token",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)


class AgentCoreSetupError(ValueError):
    """Raised when first-adopter configuration is unsafe or incomplete."""


def _required_string(value: Any, name: str, *, max_length: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise AgentCoreSetupError(
            f"{name} must be a non-empty string without surrounding whitespace or control characters"
        )
    return value


def _identifier(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=128)
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise AgentCoreSetupError(
            f"{name} must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens"
        )
    return value


def _provider_names(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AgentCoreSetupError(f"{name} must be a non-empty array")
    providers: list[str] = []
    for index, raw_provider in enumerate(value):
        provider = _required_string(
            raw_provider,
            f"{name}[{index}]",
            max_length=64,
        )
        if provider not in SUPPORTED_AGENTCORE_PROVIDERS:
            raise AgentCoreSetupError(
                f"{name}[{index}] is not a supported AgentCore provider"
            )
        if provider in providers:
            raise AgentCoreSetupError(f"{name} must not contain duplicates")
        providers.append(provider)
    return tuple(providers)


def _claim_name(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=256)
    if any(character.isspace() for character in value):
        raise AgentCoreSetupError(f"{name} must not contain whitespace")
    return value


def _https_url(
    value: Any,
    name: str,
    *,
    discovery: bool = False,
    issuer: bool = False,
) -> str:
    value = _required_string(value, name)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AgentCoreSetupError(f"{name} must be a valid HTTPS URL") from exc
    hostname = parsed.hostname or ""
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname.casefold() == "localhost"
        or hostname.casefold().endswith(".localhost")
        or is_ip_literal
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() for character in value)
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise AgentCoreSetupError(f"{name} must be an HTTPS URL without userinfo, whitespace, or a fragment")
    if issuer and (parsed.query or value.endswith("/")):
        raise AgentCoreSetupError(f"{name} must not contain a query or trailing slash")
    if discovery and not parsed.path.endswith("/.well-known/openid-configuration"):
        raise AgentCoreSetupError(f"{name} must end with /.well-known/openid-configuration")
    return value


def _viewer_cidrs(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AgentCoreSetupError(f"{name} must be a non-empty array")
    if len(value) > 64:
        raise AgentCoreSetupError(f"{name} must contain at most 64 CIDRs")

    networks: list[ipaddress.IPv4Network] = []
    for index, raw_cidr in enumerate(value):
        cidr = _required_string(
            raw_cidr,
            f"{name}[{index}]",
            max_length=64,
        )
        try:
            network = ipaddress.ip_network(cidr, strict=True)
        except ValueError as exc:
            raise AgentCoreSetupError(
                f"{name}[{index}] must be a canonical IP network"
            ) from exc
        if not isinstance(network, ipaddress.IPv4Network):
            raise AgentCoreSetupError(
                f"{name}[{index}] must be an IPv4 network"
            )
        if (
            not network.is_global
            or network.is_multicast
            or network.prefixlen < 24
        ):
            raise AgentCoreSetupError(
                f"{name}[{index}] must be a public IPv4 network no broader "
                "than /24"
            )
        if any(network.overlaps(existing) for existing in networks):
            raise AgentCoreSetupError(f"{name} must not contain overlapping CIDRs")
        networks.append(network)
    return tuple(str(network) for network in networks)


def _oauth_identifier(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=512)
    if "," in value or any(character.isspace() for character in value):
        raise AgentCoreSetupError(
            f"{name} must not contain commas or whitespace"
        )
    return value


def _saml_login_path(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=2048)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise AgentCoreSetupError(
            f"{name} must be a protected application-local path"
        ) from exc
    path = parsed.path
    lowered = path.casefold()
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or path != value
        or not path.startswith("/")
        or path.startswith("//")
        or _SAML_LOGIN_PATH_PATTERN.fullmatch(path) is None
        or "//" in path
        or "\\" in path
        or "%" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
        or lowered in {"/", "/health", "/ready"}
        or any(
            lowered == prefix or lowered.startswith(f"{prefix}/")
            for prefix in ("/saml", "/scim", "/oauth2")
        )
    ):
        raise AgentCoreSetupError(
            f"{name} must be a protected application-local path"
        )
    return path


def _optional_secret_arn(
    value: Any,
    name: str,
    *,
    aws_region: str,
) -> str | None:
    if value in (None, ""):
        return None
    arn = _required_string(value, name, max_length=700)
    match = _SECRETS_MANAGER_ARN_PATTERN.fullmatch(arn)
    if match is None or match.group("region") != aws_region:
        raise AgentCoreSetupError(
            f"{name} must be a complete Secrets Manager ARN in {aws_region}"
        )
    return arn


def _email(value: Any, name: str) -> str:
    value = _required_string(value, name, max_length=320)
    local, separator, domain = value.rpartition("@")
    if (
        separator != "@"
        or not local
        or not domain
        or "." not in domain
        or any(character.isspace() for character in value)
    ):
        raise AgentCoreSetupError(f"{name} must be a valid email address")
    return value


def _strict_object(
    value: Any,
    name: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentCoreSetupError(f"{name} must be a JSON object")
    optional = optional or set()
    missing = required.difference(value)
    unknown = set(value).difference(required | optional)
    if missing:
        raise AgentCoreSetupError(f"{name} is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise AgentCoreSetupError(f"{name} contains unsupported fields: {', '.join(sorted(unknown))}")
    return value


def _optional_display_name(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _required_string(value, "admin.display_name", max_length=256)


def _budget_limit(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentCoreSetupError("tenant.budget_limit must be a number or null")
    normalized = float(value)
    if normalized < 0 or normalized != normalized or normalized == float("inf"):
        raise AgentCoreSetupError("tenant.budget_limit must be a finite non-negative number")
    return normalized


def _bounded_integer(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or maximum is not None
        and value > maximum
    ):
        qualifier = (
            f"between {minimum} and {maximum}"
            if maximum is not None
            else f"at least {minimum}"
        )
        raise AgentCoreSetupError(f"{name} must be {qualifier}")
    return value


def _bounded_float(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise AgentCoreSetupError(
            f"{name} must be between {minimum} and {maximum}"
        )
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise AgentCoreSetupError(f"{name} must be finite") from exc
    if (
        not math.isfinite(normalized)
        or not minimum <= normalized <= maximum
    ):
        raise AgentCoreSetupError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return normalized


@dataclass(frozen=True)
class AthenaQuerySetup:
    """Optional deployment-bound query roles and result limits."""

    role_arns: tuple[str, ...]
    timeout_seconds: float = 30.0
    max_rows: int = 1000
    max_result_bytes: int = 1024 * 1024
    max_bytes_scanned: int = 1024 * 1024 * 1024
    poll_interval_seconds: float = 0.25
    project_rpm: int = 30
    principal_rpm: int = 10
    project_concurrency: int = 5
    principal_concurrency: int = 2
    project_scan_bytes_per_minute: int = 5 * 1024 * 1024 * 1024
    principal_scan_bytes_per_minute: int = 2 * 1024 * 1024 * 1024
    max_datasources_per_tenant: int = 500

    def __post_init__(self) -> None:
        if self.principal_rpm > self.project_rpm:
            raise AgentCoreSetupError(
                "runtime.athena_query.principal_rpm must not exceed "
                "project_rpm"
            )
        if self.principal_concurrency > self.project_concurrency:
            raise AgentCoreSetupError(
                "runtime.athena_query.principal_concurrency must not "
                "exceed project_concurrency"
            )
        if (
            self.principal_scan_bytes_per_minute
            > self.project_scan_bytes_per_minute
        ):
            raise AgentCoreSetupError(
                "runtime.athena_query principal scan budget must not "
                "exceed the project scan budget"
            )
        if (
            self.max_bytes_scanned
            > self.principal_scan_bytes_per_minute
        ):
            raise AgentCoreSetupError(
                "runtime.athena_query.max_bytes_scanned must fit within "
                "the principal aggregate scan budget"
            )

    @classmethod
    def from_mapping(cls, raw: Any) -> AthenaQuerySetup:
        value = _strict_object(
            raw,
            "runtime.athena_query",
            required={"role_arns"},
            optional={
                "timeout_seconds",
                "max_rows",
                "max_result_bytes",
                "max_bytes_scanned",
                "poll_interval_seconds",
                "project_rpm",
                "principal_rpm",
                "project_concurrency",
                "principal_concurrency",
                "project_scan_bytes_per_minute",
                "principal_scan_bytes_per_minute",
                "max_datasources_per_tenant",
            },
        )
        raw_roles = value["role_arns"]
        if not isinstance(raw_roles, list) or not raw_roles:
            raise AgentCoreSetupError(
                "runtime.athena_query.role_arns must be a non-empty array"
            )
        roles: list[str] = []
        for index, raw_role in enumerate(raw_roles):
            role = _required_string(
                raw_role,
                f"runtime.athena_query.role_arns[{index}]",
                max_length=600,
            )
            if (
                "*" in role
                or _IAM_ROLE_ARN_PATTERN.fullmatch(role) is None
            ):
                raise AgentCoreSetupError(
                    "Athena query roles must be concrete IAM role ARNs"
                )
            if role in roles:
                raise AgentCoreSetupError(
                    "runtime.athena_query.role_arns must not contain "
                    "duplicates"
                )
            roles.append(role)
        return cls(
            role_arns=tuple(roles),
            timeout_seconds=_bounded_float(
                value.get("timeout_seconds", 30.0),
                "runtime.athena_query.timeout_seconds",
                minimum=0.001,
                maximum=300.0,
            ),
            max_rows=_bounded_integer(
                value.get("max_rows", 1000),
                "runtime.athena_query.max_rows",
                minimum=1,
                maximum=10_000,
            ),
            max_result_bytes=_bounded_integer(
                value.get("max_result_bytes", 1024 * 1024),
                "runtime.athena_query.max_result_bytes",
                minimum=1024,
                maximum=16 * 1024 * 1024,
            ),
            max_bytes_scanned=_bounded_integer(
                value.get(
                    "max_bytes_scanned",
                    1024 * 1024 * 1024,
                ),
                "runtime.athena_query.max_bytes_scanned",
                minimum=1,
            ),
            poll_interval_seconds=_bounded_float(
                value.get("poll_interval_seconds", 0.25),
                "runtime.athena_query.poll_interval_seconds",
                minimum=0.05,
                maximum=5.0,
            ),
            project_rpm=_bounded_integer(
                value.get("project_rpm", 30),
                "runtime.athena_query.project_rpm",
                minimum=1,
                maximum=10_000,
            ),
            principal_rpm=_bounded_integer(
                value.get("principal_rpm", 10),
                "runtime.athena_query.principal_rpm",
                minimum=1,
                maximum=10_000,
            ),
            project_concurrency=_bounded_integer(
                value.get("project_concurrency", 5),
                "runtime.athena_query.project_concurrency",
                minimum=1,
                maximum=100,
            ),
            principal_concurrency=_bounded_integer(
                value.get("principal_concurrency", 2),
                "runtime.athena_query.principal_concurrency",
                minimum=1,
                maximum=100,
            ),
            project_scan_bytes_per_minute=_bounded_integer(
                value.get(
                    "project_scan_bytes_per_minute",
                    5 * 1024 * 1024 * 1024,
                ),
                (
                    "runtime.athena_query."
                    "project_scan_bytes_per_minute"
                ),
                minimum=1,
            ),
            principal_scan_bytes_per_minute=_bounded_integer(
                value.get(
                    "principal_scan_bytes_per_minute",
                    2 * 1024 * 1024 * 1024,
                ),
                (
                    "runtime.athena_query."
                    "principal_scan_bytes_per_minute"
                ),
                minimum=1,
            ),
            max_datasources_per_tenant=_bounded_integer(
                value.get("max_datasources_per_tenant", 500),
                (
                    "runtime.athena_query."
                    "max_datasources_per_tenant"
                ),
                minimum=1,
                maximum=10_000,
            ),
        )


@dataclass(frozen=True)
class TenantSetup:
    tenant_id: str
    project_id: str
    project_name: str
    budget_limit: float | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> TenantSetup:
        value = _strict_object(
            raw,
            "tenant",
            required={"tenant_id", "project_id", "project_name"},
            optional={"budget_limit"},
        )
        return cls(
            tenant_id=_identifier(value["tenant_id"], "tenant.tenant_id"),
            project_id=_identifier(value["project_id"], "tenant.project_id"),
            project_name=_required_string(
                value["project_name"],
                "tenant.project_name",
                max_length=256,
            ),
            budget_limit=_budget_limit(value.get("budget_limit")),
        )


@dataclass(frozen=True)
class AdminSetup:
    user_name: str
    email: str
    display_name: str = ""
    subject: str | None = None

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        subject_required: bool,
    ) -> AdminSetup:
        value = _strict_object(
            raw,
            "admin",
            required={"user_name", "email"},
            optional={"display_name", "subject"},
        )
        subject = value.get("subject")
        if subject_required:
            subject = _required_string(subject, "admin.subject")
        elif subject is not None:
            raise AgentCoreSetupError("admin.subject is assigned by managed Cognito and must be omitted")
        return cls(
            user_name=_email(value["user_name"], "admin.user_name"),
            email=_email(value["email"], "admin.email"),
            display_name=_optional_display_name(value.get("display_name")),
            subject=subject,
        )


@dataclass(frozen=True)
class RuntimeSetup:
    verified_image_uri: str
    bedrock_invoke_resource_arns: tuple[str, ...]
    approved_https_prefix_list_id: str
    enabled_providers: tuple[str, ...] = DEFAULT_AGENTCORE_PROVIDERS
    athena_query: AthenaQuerySetup | None = None

    @classmethod
    def from_mapping(cls, raw: Any, *, aws_region: str) -> RuntimeSetup:
        value = _strict_object(
            raw,
            "runtime",
            required={
                "verified_image_uri",
                "bedrock_invoke_resource_arns",
                "approved_https_prefix_list_id",
            },
            optional={"athena_query", "enabled_providers"},
        )
        image = _required_string(
            value["verified_image_uri"],
            "runtime.verified_image_uri",
        )
        match = _IMAGE_PATTERN.fullmatch(image)
        if match is None or match.group("region") != aws_region:
            raise AgentCoreSetupError(
                f"runtime.verified_image_uri must be an immutable private ECR digest URI in {aws_region}"
            )
        raw_arns = value["bedrock_invoke_resource_arns"]
        if not isinstance(raw_arns, list) or not raw_arns:
            raise AgentCoreSetupError("runtime.bedrock_invoke_resource_arns must be a non-empty array")
        arns: list[str] = []
        for index, raw_arn in enumerate(raw_arns):
            arn = _required_string(
                raw_arn,
                f"runtime.bedrock_invoke_resource_arns[{index}]",
            )
            arn_match = _BEDROCK_ARN_PATTERN.fullmatch(arn)
            if arn_match is None or "*" in arn:
                raise AgentCoreSetupError(
                    "each Bedrock resource must be a concrete model or "
                    "inference-profile ARN"
                )
            if (
                arn_match.group("resource_type") != "foundation-model"
                and arn_match.group("region") != aws_region
            ):
                raise AgentCoreSetupError(
                    "Bedrock inference profiles and account-scoped resources "
                    f"must be in {aws_region}; cross-region foundation-model "
                    "destinations are allowed"
                )
            if arn in arns:
                raise AgentCoreSetupError("runtime.bedrock_invoke_resource_arns must not contain duplicates")
            arns.append(arn)
        prefix_list_id = _required_string(
            value["approved_https_prefix_list_id"],
            "runtime.approved_https_prefix_list_id",
        )
        if _PREFIX_LIST_PATTERN.fullmatch(prefix_list_id) is None:
            raise AgentCoreSetupError("runtime.approved_https_prefix_list_id must be an EC2 managed prefix list ID")
        return cls(
            verified_image_uri=image,
            bedrock_invoke_resource_arns=tuple(arns),
            approved_https_prefix_list_id=prefix_list_id,
            enabled_providers=_provider_names(
                value.get(
                    "enabled_providers",
                    list(DEFAULT_AGENTCORE_PROVIDERS),
                ),
                "runtime.enabled_providers",
            ),
            athena_query=(
                AthenaQuerySetup.from_mapping(value["athena_query"])
                if value.get("athena_query") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ManagedCognitoSetup:
    hosted_ui_domain_prefix: str
    oauth_callback_urls: tuple[str, ...] = ()
    ses_from_email: str | None = None
    ses_verified_domain: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> ManagedCognitoSetup:
        value = _strict_object(
            raw,
            "managed_cognito",
            required={"hosted_ui_domain_prefix"},
            optional={
                "oauth_callback_urls",
                "ses_from_email",
                "ses_verified_domain",
            },
        )
        prefix = _required_string(
            value["hosted_ui_domain_prefix"],
            "managed_cognito.hosted_ui_domain_prefix",
            max_length=63,
        )
        if _DOMAIN_PREFIX_PATTERN.fullmatch(prefix) is None:
            raise AgentCoreSetupError(
                "managed_cognito.hosted_ui_domain_prefix must be 3-63 "
                "lowercase letters, numbers, or hyphens and cannot start or "
                "end with a hyphen"
            )
        raw_urls = value.get("oauth_callback_urls", [])
        if not isinstance(raw_urls, list):
            raise AgentCoreSetupError(
                "managed_cognito.oauth_callback_urls must be an array"
            )
        urls: list[str] = []
        for index, raw_url in enumerate(raw_urls):
            url = _https_url(
                raw_url,
                f"managed_cognito.oauth_callback_urls[{index}]",
            )
            if "," in url:
                raise AgentCoreSetupError("managed Cognito callback URLs must not contain commas")
            if url in urls:
                raise AgentCoreSetupError("managed Cognito callback URLs must not contain duplicates")
            urls.append(url)
        ses_from_email = value.get("ses_from_email")
        ses_verified_domain = value.get("ses_verified_domain")
        if (ses_from_email is None) != (ses_verified_domain is None):
            raise AgentCoreSetupError(
                "managed_cognito.ses_from_email and "
                "managed_cognito.ses_verified_domain must be supplied together"
            )
        if ses_from_email is not None:
            ses_from_email = _email(
                ses_from_email,
                "managed_cognito.ses_from_email",
            )
            ses_verified_domain = _required_string(
                ses_verified_domain,
                "managed_cognito.ses_verified_domain",
                max_length=320,
            )
            if (
                ses_verified_domain != ses_from_email
                and (
                    _DOMAIN_NAME_PATTERN.fullmatch(ses_verified_domain)
                    is None
                    or ses_from_email.rpartition("@")[2].casefold()
                    != ses_verified_domain
                )
            ):
                raise AgentCoreSetupError(
                    "managed_cognito.ses_verified_domain must be either the "
                    "exact sender email or its lowercase domain"
                )
        return cls(
            hosted_ui_domain_prefix=prefix,
            oauth_callback_urls=tuple(urls),
            ses_from_email=ses_from_email,
            ses_verified_domain=ses_verified_domain,
        )


@dataclass(frozen=True)
class ControlPlaneSetup:
    """Managed-Cognito web control-plane deployment inputs."""

    endpoint_mode: str
    verified_image_uri: str
    approved_https_prefix_list_id: str
    domain_name: str | None = None
    certificate_arn: str | None = None
    public_hosted_zone_id: str | None = None
    approved_ingress_prefix_list_id: str | None = None
    allowed_viewer_cidrs: tuple[str, ...] = ()
    scim_tenants_secret_arn: str | None = None
    saml_login_path: str = DEFAULT_SAML_LOGIN_PATH

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        aws_region: str,
    ) -> ControlPlaneSetup:
        value = _strict_object(
            raw,
            "control_plane",
            required={"verified_image_uri", "approved_https_prefix_list_id"},
            optional={
                "endpoint_mode",
                "domain_name",
                "certificate_arn",
                "public_hosted_zone_id",
                "approved_ingress_prefix_list_id",
                "allowed_viewer_cidrs",
                "scim_tenants_secret_arn",
                "saml_login_path",
            },
        )
        endpoint_mode = value.get("endpoint_mode", CUSTOM_DOMAIN)
        if endpoint_mode not in CONTROL_PLANE_ENDPOINT_MODES:
            raise AgentCoreSetupError(
                "control_plane.endpoint_mode must be 'custom-domain' or "
                "'cloudfront'"
            )
        image = _required_string(
            value["verified_image_uri"],
            "control_plane.verified_image_uri",
        )
        image_match = _IMAGE_PATTERN.fullmatch(image)
        if image_match is None or image_match.group("region") != aws_region:
            raise AgentCoreSetupError(
                "control_plane.verified_image_uri must be an immutable "
                f"private ECR digest URI in {aws_region}"
            )
        approved_https_prefix_list_id = _required_string(
            value["approved_https_prefix_list_id"],
            "control_plane.approved_https_prefix_list_id",
            max_length=64,
        )
        if _PREFIX_LIST_PATTERN.fullmatch(approved_https_prefix_list_id) is None:
            raise AgentCoreSetupError(
                "control_plane.approved_https_prefix_list_id must be an EC2 "
                "managed prefix list ID"
            )

        domain_name = None
        certificate_arn = None
        public_hosted_zone_id = None
        approved_ingress_prefix_list_id = None
        allowed_viewer_cidrs: tuple[str, ...] = ()
        custom_fields = {
            "domain_name",
            "certificate_arn",
            "public_hosted_zone_id",
            "approved_ingress_prefix_list_id",
        }
        if endpoint_mode == CUSTOM_DOMAIN:
            missing = custom_fields.difference(value)
            if missing:
                raise AgentCoreSetupError(
                    "custom-domain control_plane is missing required fields: "
                    + ", ".join(sorted(missing))
                )
            if "allowed_viewer_cidrs" in value:
                raise AgentCoreSetupError(
                    "custom-domain control_plane forbids allowed_viewer_cidrs"
                )
            domain_name = _required_string(
                value["domain_name"],
                "control_plane.domain_name",
                max_length=253,
            )
            if _DOMAIN_NAME_PATTERN.fullmatch(domain_name) is None:
                raise AgentCoreSetupError(
                    "control_plane.domain_name must be a lowercase fully "
                    "qualified DNS hostname"
                )
            certificate_arn = _required_string(
                value["certificate_arn"],
                "control_plane.certificate_arn",
                max_length=512,
            )
            certificate_match = _ACM_CERTIFICATE_ARN_PATTERN.fullmatch(
                certificate_arn
            )
            if (
                certificate_match is None
                or certificate_match.group("region") != aws_region
            ):
                raise AgentCoreSetupError(
                    "control_plane.certificate_arn must be a regional ACM "
                    f"certificate ARN in {aws_region}"
                )
            public_hosted_zone_id = _required_string(
                value["public_hosted_zone_id"],
                "control_plane.public_hosted_zone_id",
                max_length=64,
            )
            if _HOSTED_ZONE_PATTERN.fullmatch(public_hosted_zone_id) is None:
                raise AgentCoreSetupError(
                    "control_plane.public_hosted_zone_id must be a Route 53 "
                    "public hosted-zone ID"
                )
            approved_ingress_prefix_list_id = _required_string(
                value["approved_ingress_prefix_list_id"],
                "control_plane.approved_ingress_prefix_list_id",
                max_length=64,
            )
            if (
                _PREFIX_LIST_PATTERN.fullmatch(
                    approved_ingress_prefix_list_id
                )
                is None
            ):
                raise AgentCoreSetupError(
                    "control_plane.approved_ingress_prefix_list_id must be an "
                    "EC2 managed prefix list ID"
                )
        else:
            present = custom_fields.intersection(value)
            if present:
                raise AgentCoreSetupError(
                    "cloudfront control_plane forbids custom-domain fields: "
                    + ", ".join(sorted(present))
                )
            if aws_region != "us-east-1":
                raise AgentCoreSetupError(
                    "cloudfront control_plane currently requires aws_region "
                    "'us-east-1' for its global WAF and distribution"
                )
            allowed_viewer_cidrs = _viewer_cidrs(
                value.get("allowed_viewer_cidrs"),
                "control_plane.allowed_viewer_cidrs",
            )
        return cls(
            endpoint_mode=endpoint_mode,
            domain_name=domain_name,
            verified_image_uri=image,
            certificate_arn=certificate_arn,
            public_hosted_zone_id=public_hosted_zone_id,
            approved_ingress_prefix_list_id=approved_ingress_prefix_list_id,
            approved_https_prefix_list_id=approved_https_prefix_list_id,
            allowed_viewer_cidrs=allowed_viewer_cidrs,
            scim_tenants_secret_arn=_optional_secret_arn(
                value.get("scim_tenants_secret_arn"),
                "control_plane.scim_tenants_secret_arn",
                aws_region=aws_region,
            ),
            saml_login_path=_saml_login_path(
                value.get(
                    "saml_login_path",
                    DEFAULT_SAML_LOGIN_PATH,
                ),
                "control_plane.saml_login_path",
            ),
        )


@dataclass(frozen=True)
class ExternalOidcSetup:
    issuer: str
    discovery_url: str
    client_id: str
    audience: str
    tenant_claim: str
    project_claim: str

    @classmethod
    def from_mapping(cls, raw: Any) -> ExternalOidcSetup:
        value = _strict_object(
            raw,
            "external_oidc",
            required={
                "issuer",
                "discovery_url",
                "client_id",
                "audience",
                "tenant_claim",
                "project_claim",
            },
        )
        issuer = _https_url(
            value["issuer"],
            "external_oidc.issuer",
            issuer=True,
        )
        discovery_url = _https_url(
            value["discovery_url"],
            "external_oidc.discovery_url",
            discovery=True,
        )
        if discovery_url != f"{issuer}/.well-known/openid-configuration":
            raise AgentCoreSetupError(
                "external_oidc.discovery_url must be the configured issuer "
                "followed by /.well-known/openid-configuration"
            )
        return cls(
            issuer=issuer,
            discovery_url=discovery_url,
            client_id=_oauth_identifier(
                value["client_id"],
                "external_oidc.client_id",
            ),
            audience=_oauth_identifier(
                value["audience"],
                "external_oidc.audience",
            ),
            tenant_claim=_claim_name(
                value["tenant_claim"],
                "external_oidc.tenant_claim",
            ),
            project_claim=_claim_name(
                value["project_claim"],
                "external_oidc.project_claim",
            ),
        )


@dataclass(frozen=True)
class AgentCoreSetupConfig:
    schema_version: int
    target: str
    identity_mode: str
    aws_region: str
    tenant: TenantSetup
    admin: AdminSetup
    runtime: RuntimeSetup
    managed_cognito: ManagedCognitoSetup | None = None
    control_plane: ControlPlaneSetup | None = None
    external_oidc: ExternalOidcSetup | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> AgentCoreSetupConfig:
        value = _strict_object(
            raw,
            "configuration",
            required={
                "schema_version",
                "target",
                "identity_mode",
                "aws_region",
                "tenant",
                "admin",
                "runtime",
            },
            optional={
                "managed_cognito",
                "control_plane",
                "external_oidc",
            },
        )
        if (
            isinstance(value["schema_version"], bool)
            or not isinstance(value["schema_version"], int)
            or value["schema_version"] != SCHEMA_VERSION
        ):
            raise AgentCoreSetupError(f"schema_version must be {SCHEMA_VERSION}")
        if value["target"] != "agentcore":
            raise AgentCoreSetupError("target must be 'agentcore'")
        identity_mode = value["identity_mode"]
        if identity_mode not in IDENTITY_MODES:
            raise AgentCoreSetupError(
                "identity_mode must be 'managed-cognito' or 'external-oidc'; "
                "AgentCore has no unauthenticated production mode"
            )
        aws_region = _identifier(value["aws_region"], "aws_region")
        managed_raw = value.get("managed_cognito")
        control_plane_raw = value.get("control_plane")
        external_raw = value.get("external_oidc")
        if identity_mode == MANAGED_COGNITO:
            if (
                managed_raw is None
                or control_plane_raw is None
                or external_raw is not None
            ):
                raise AgentCoreSetupError(
                    "managed-cognito requires managed_cognito and "
                    "control_plane and forbids external_oidc"
                )
        elif (
            external_raw is None
            or managed_raw is not None
            or control_plane_raw is not None
        ):
            raise AgentCoreSetupError(
                "external-oidc requires external_oidc and forbids "
                "managed_cognito and control_plane"
            )
        return cls(
            schema_version=SCHEMA_VERSION,
            target="agentcore",
            identity_mode=identity_mode,
            aws_region=aws_region,
            tenant=TenantSetup.from_mapping(value["tenant"]),
            admin=AdminSetup.from_mapping(
                value["admin"],
                subject_required=identity_mode == EXTERNAL_OIDC,
            ),
            runtime=RuntimeSetup.from_mapping(
                value["runtime"],
                aws_region=aws_region,
            ),
            managed_cognito=(ManagedCognitoSetup.from_mapping(managed_raw) if managed_raw is not None else None),
            control_plane=(
                ControlPlaneSetup.from_mapping(
                    control_plane_raw,
                    aws_region=aws_region,
                )
                if control_plane_raw is not None
                else None
            ),
            external_oidc=(ExternalOidcSetup.from_mapping(external_raw) if external_raw is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.admin.subject is None:
            value["admin"].pop("subject")
        if self.managed_cognito is None:
            value.pop("managed_cognito")
        else:
            if self.managed_cognito.oauth_callback_urls:
                value["managed_cognito"]["oauth_callback_urls"] = list(
                    self.managed_cognito.oauth_callback_urls
                )
            else:
                value["managed_cognito"].pop("oauth_callback_urls")
            if self.managed_cognito.ses_from_email is None:
                value["managed_cognito"].pop("ses_from_email")
                value["managed_cognito"].pop("ses_verified_domain")
        if self.control_plane is None:
            value.pop("control_plane")
        else:
            value["control_plane"]["allowed_viewer_cidrs"] = list(
                self.control_plane.allowed_viewer_cidrs
            )
            if self.control_plane.endpoint_mode == CUSTOM_DOMAIN:
                value["control_plane"].pop("endpoint_mode")
            for optional_field in (
                "domain_name",
                "certificate_arn",
                "public_hosted_zone_id",
                "approved_ingress_prefix_list_id",
                "scim_tenants_secret_arn",
            ):
                if value["control_plane"][optional_field] is None:
                    value["control_plane"].pop(optional_field)
            if not self.control_plane.allowed_viewer_cidrs:
                value["control_plane"].pop("allowed_viewer_cidrs")
        if self.external_oidc is None:
            value.pop("external_oidc")
        value["runtime"]["bedrock_invoke_resource_arns"] = list(self.runtime.bedrock_invoke_resource_arns)
        value["runtime"]["enabled_providers"] = list(
            self.runtime.enabled_providers
        )
        if self.runtime.athena_query is None:
            value["runtime"].pop("athena_query")
        else:
            value["runtime"]["athena_query"]["role_arns"] = list(
                self.runtime.athena_query.role_arns
            )
        return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AgentCoreSetupError(f"configuration contains duplicate JSON field {key!r}")
        value[key] = item
    return value


def _reject_non_finite_number(value: str) -> None:
    raise AgentCoreSetupError(f"configuration contains non-finite number {value}")


def load_agentcore_setup(path: str | Path) -> AgentCoreSetupConfig:
    config_path = Path(path)
    try:
        raw_bytes = config_path.read_bytes()
    except OSError as exc:
        raise AgentCoreSetupError(f"cannot read AgentCore setup file {config_path}: {exc}") from exc
    if len(raw_bytes) > 128 * 1024:
        raise AgentCoreSetupError("AgentCore setup file exceeds 128 KiB")
    try:
        raw = json.loads(
            raw_bytes,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentCoreSetupError(f"AgentCore setup file is not valid UTF-8 JSON: {exc}") from exc
    return AgentCoreSetupConfig.from_mapping(raw)


def write_agentcore_setup(
    config: AgentCoreSetupConfig,
    path: str | Path,
) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def redact_sensitive(value: Any) -> Any:
    """Return a log-safe copy of nested setup data."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _comma_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def config_from_args(args: argparse.Namespace) -> AgentCoreSetupConfig:
    mode = args.identity_mode
    mapping: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target": "agentcore",
        "identity_mode": mode,
        "aws_region": args.aws_region,
        "tenant": {
            "tenant_id": args.tenant,
            "project_id": args.project,
            "project_name": args.project_name,
            "budget_limit": args.budget_limit,
        },
        "admin": {
            "user_name": args.admin_user_name,
            "email": args.admin_email,
            "display_name": args.admin_display_name,
        },
        "runtime": {
            "verified_image_uri": args.verified_image_uri,
            "bedrock_invoke_resource_arns": _comma_values(args.bedrock_invoke_resource_arns),
            "approved_https_prefix_list_id": (args.approved_https_prefix_list_id),
            "enabled_providers": (
                list(getattr(args, "enabled_provider", None) or [])
                or _comma_values(os.environ.get("AXON_ENABLED_PROVIDERS"))
                or list(DEFAULT_AGENTCORE_PROVIDERS)
            ),
        },
    }
    athena_role_arns = list(args.athena_query_role_arn or [])
    if not athena_role_arns:
        athena_role_arns = _comma_values(
            os.environ.get("AXON_ATHENA_QUERY_ROLE_ARNS")
        )
    if athena_role_arns:
        mapping["runtime"]["athena_query"] = {
            "role_arns": athena_role_arns,
            "timeout_seconds": args.athena_query_timeout_seconds,
            "max_rows": args.athena_query_max_rows,
            "max_result_bytes": args.athena_query_max_result_bytes,
            "max_bytes_scanned": args.athena_query_max_bytes_scanned,
            "poll_interval_seconds": (
                args.athena_query_poll_interval_seconds
            ),
            "project_rpm": args.athena_query_project_rpm,
            "principal_rpm": args.athena_query_principal_rpm,
            "project_concurrency": (
                args.athena_query_project_concurrency
            ),
            "principal_concurrency": (
                args.athena_query_principal_concurrency
            ),
            "project_scan_bytes_per_minute": (
                args.athena_query_project_scan_bytes_per_minute
            ),
            "principal_scan_bytes_per_minute": (
                args.athena_query_principal_scan_bytes_per_minute
            ),
            "max_datasources_per_tenant": (
                args.athena_query_max_datasources_per_tenant
            ),
        }
    if mode == MANAGED_COGNITO:
        callback_urls = list(args.oauth_callback_url or [])
        if not callback_urls:
            callback_urls = _comma_values(os.environ.get("AXON_OAUTH_CALLBACK_URLS"))
        mapping["managed_cognito"] = {
            "hosted_ui_domain_prefix": args.hosted_ui_domain_prefix,
        }
        if callback_urls:
            mapping["managed_cognito"]["oauth_callback_urls"] = callback_urls
        if args.ses_from_email or args.ses_verified_domain:
            mapping["managed_cognito"].update(
                {
                    "ses_from_email": args.ses_from_email,
                    "ses_verified_domain": args.ses_verified_domain,
                }
            )
        endpoint_mode = (
            args.control_plane_endpoint_mode or CUSTOM_DOMAIN
        )
        mapping["control_plane"] = {
            "endpoint_mode": endpoint_mode,
            "verified_image_uri": (
                args.control_plane_verified_image_uri
            ),
            "approved_https_prefix_list_id": (
                args.control_plane_approved_https_prefix_list_id
            ),
            "saml_login_path": (
                args.control_plane_saml_login_path
            ),
        }
        if endpoint_mode == CUSTOM_DOMAIN:
            mapping["control_plane"].update(
                {
                    "domain_name": args.control_plane_domain_name,
                    "certificate_arn": (
                        args.control_plane_certificate_arn
                    ),
                    "public_hosted_zone_id": (
                        args.control_plane_public_hosted_zone_id
                    ),
                    "approved_ingress_prefix_list_id": (
                        args.control_plane_approved_ingress_prefix_list_id
                    ),
                }
            )
        else:
            viewer_cidrs = list(
                args.control_plane_allowed_viewer_cidr or []
            )
            if not viewer_cidrs:
                viewer_cidrs = _comma_values(
                    os.environ.get(
                        "AXON_CONTROL_PLANE_ALLOWED_VIEWER_CIDRS"
                    )
                )
            mapping["control_plane"]["allowed_viewer_cidrs"] = viewer_cidrs
        if args.control_plane_scim_tenants_secret_arn:
            mapping["control_plane"]["scim_tenants_secret_arn"] = (
                args.control_plane_scim_tenants_secret_arn
            )
    elif mode == EXTERNAL_OIDC:
        mapping["admin"]["subject"] = args.admin_subject
        mapping["external_oidc"] = {
            "issuer": args.oidc_issuer,
            "discovery_url": args.oidc_discovery_url,
            "client_id": args.oidc_client_id,
            "audience": args.oidc_audience,
            "tenant_claim": args.oidc_tenant_claim,
            "project_claim": args.oidc_project_claim,
        }
    return AgentCoreSetupConfig.from_mapping(mapping)


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def add_setup_subcommands(subparsers: argparse._SubParsersAction) -> None:
    setup = subparsers.add_parser(
        "setup",
        help="Configure a local demo or authenticated AgentCore deployment",
    )
    targets = setup.add_subparsers(dest="setup_target")

    local = targets.add_parser(
        "local-demo",
        help="Start the anonymous seeded demo (non-production only)",
    )
    local.add_argument("--start", action="store_true")
    local.add_argument(
        "--acknowledge-non-production",
        action="store_true",
        help="Required with --start because this mode accepts anonymous traffic",
    )

    agentcore = targets.add_parser(
        "agentcore",
        help="Create or deploy an authenticated AgentCore setup file",
    )
    agentcore.add_argument("--config", help="Use an existing setup JSON file")
    agentcore.add_argument(
        "--output",
        default="axonllm-agentcore.json",
        help="Generated setup JSON path",
    )
    agentcore.add_argument("--deploy", action="store_true")
    agentcore.add_argument("--yes", action="store_true")
    agentcore.add_argument("--bootstrap-cdk", action="store_true")
    agentcore.add_argument("--show-config", action="store_true")
    agentcore.add_argument(
        "--provider-env-file",
        default=_env("AXON_PROVIDER_ENV_FILE"),
        help=(
            "Owner-only env file containing allowlisted provider "
            "credentials"
        ),
    )
    agentcore.add_argument(
        "--rollback-provider-secret-version",
        help=(
            "Roll back the deployed provider secret and force a new runtime "
            "version"
        ),
    )
    agentcore.add_argument(
        "--identity-mode",
        choices=IDENTITY_MODES,
        default=_env("AXON_IDENTITY_MODE"),
    )
    agentcore.add_argument(
        "--aws-region",
        default=_env("AWS_DEFAULT_REGION", "us-east-1"),
    )
    agentcore.add_argument("--tenant", default=_env("AXON_TENANT_ID"))
    agentcore.add_argument("--project", default=_env("AXON_PROJECT_ID"))
    agentcore.add_argument(
        "--project-name",
        default=_env("AXON_PROJECT_NAME", "Production"),
    )
    agentcore.add_argument(
        "--budget-limit",
        type=float,
        default=_env("AXON_PROJECT_BUDGET_LIMIT"),
    )
    agentcore.add_argument(
        "--admin-user-name",
        default=_env("AXON_ADMIN_USER_NAME"),
    )
    agentcore.add_argument(
        "--admin-email",
        default=_env("AXON_ADMIN_EMAIL"),
    )
    agentcore.add_argument(
        "--admin-display-name",
        default=_env("AXON_ADMIN_DISPLAY_NAME", ""),
    )
    agentcore.add_argument(
        "--admin-subject",
        default=_env("AXON_ADMIN_SUBJECT"),
    )
    agentcore.add_argument(
        "--verified-image-uri",
        default=_env("AXON_VERIFIED_IMAGE_URI"),
    )
    agentcore.add_argument(
        "--bedrock-invoke-resource-arns",
        default=_env("AXON_BEDROCK_INVOKE_RESOURCE_ARNS"),
    )
    agentcore.add_argument(
        "--approved-https-prefix-list-id",
        default=_env("AXON_APPROVED_HTTPS_PREFIX_LIST_ID"),
    )
    agentcore.add_argument(
        "--enabled-provider",
        action="append",
        choices=tuple(sorted(SUPPORTED_AGENTCORE_PROVIDERS)),
        help=(
            "Provider required in the production runtime; repeat for each "
            "provider. Defaults to every supported provider."
        ),
    )
    agentcore.add_argument(
        "--athena-query-role-arn",
        action="append",
        help=(
            "Enable read-only queries with an exact project IAM role; "
            "repeat to approve more than one role"
        ),
    )
    agentcore.add_argument(
        "--athena-query-timeout-seconds",
        type=float,
        default=float(
            _env("AXON_ATHENA_QUERY_TIMEOUT_SECONDS", "30")
        ),
    )
    agentcore.add_argument(
        "--athena-query-max-rows",
        type=int,
        default=int(_env("AXON_ATHENA_QUERY_MAX_ROWS", "1000")),
    )
    agentcore.add_argument(
        "--athena-query-max-result-bytes",
        type=int,
        default=int(
            _env(
                "AXON_ATHENA_QUERY_MAX_RESULT_BYTES",
                str(1024 * 1024),
            )
        ),
    )
    agentcore.add_argument(
        "--athena-query-max-bytes-scanned",
        type=int,
        default=int(
            _env(
                "AXON_ATHENA_QUERY_MAX_BYTES_SCANNED",
                str(1024 * 1024 * 1024),
            )
        ),
    )
    agentcore.add_argument(
        "--athena-query-poll-interval-seconds",
        type=float,
        default=float(
            _env(
                "AXON_ATHENA_QUERY_POLL_INTERVAL_SECONDS",
                "0.25",
            )
        ),
    )
    agentcore.add_argument(
        "--athena-query-project-rpm",
        type=int,
        default=int(_env("AXON_ATHENA_QUERY_PROJECT_RPM", "30")),
    )
    agentcore.add_argument(
        "--athena-query-principal-rpm",
        type=int,
        default=int(_env("AXON_ATHENA_QUERY_PRINCIPAL_RPM", "10")),
    )
    agentcore.add_argument(
        "--athena-query-project-concurrency",
        type=int,
        default=int(
            _env("AXON_ATHENA_QUERY_PROJECT_CONCURRENCY", "5")
        ),
    )
    agentcore.add_argument(
        "--athena-query-principal-concurrency",
        type=int,
        default=int(
            _env("AXON_ATHENA_QUERY_PRINCIPAL_CONCURRENCY", "2")
        ),
    )
    agentcore.add_argument(
        "--athena-query-project-scan-bytes-per-minute",
        type=int,
        default=int(
            _env(
                "AXON_ATHENA_QUERY_PROJECT_SCAN_BYTES_PER_MINUTE",
                str(5 * 1024 * 1024 * 1024),
            )
        ),
    )
    agentcore.add_argument(
        "--athena-query-principal-scan-bytes-per-minute",
        type=int,
        default=int(
            _env(
                "AXON_ATHENA_QUERY_PRINCIPAL_SCAN_BYTES_PER_MINUTE",
                str(2 * 1024 * 1024 * 1024),
            )
        ),
    )
    agentcore.add_argument(
        "--athena-query-max-datasources-per-tenant",
        type=int,
        default=int(
            _env(
                "AXON_ATHENA_QUERY_MAX_DATASOURCES_PER_TENANT",
                "500",
            )
        ),
    )
    agentcore.add_argument(
        "--hosted-ui-domain-prefix",
        default=_env("AXON_COGNITO_DOMAIN_PREFIX"),
    )
    agentcore.add_argument(
        "--oauth-callback-url",
        action="append",
        help=(
            "Legacy schema-v2 compatibility value; browser OAuth callbacks "
            "are owned by the deployed control plane"
        ),
    )
    agentcore.add_argument(
        "--ses-from-email",
        default=_env("AXON_COGNITO_SES_FROM_EMAIL"),
        help="Verified SES sender used for Cognito invitations",
    )
    agentcore.add_argument(
        "--ses-verified-domain",
        default=_env("AXON_COGNITO_SES_VERIFIED_DOMAIN"),
        help=(
            "Exact SES-verified sender email or its verified lowercase domain"
        ),
    )
    agentcore.add_argument(
        "--control-plane-endpoint-mode",
        choices=CONTROL_PLANE_ENDPOINT_MODES,
        default=_env("AXON_CONTROL_PLANE_ENDPOINT_MODE"),
        help=(
            "Use a custom Route 53/ACM hostname or an AWS-generated "
            "CloudFront hostname"
        ),
    )
    agentcore.add_argument(
        "--control-plane-domain-name",
        default=_env("AXON_CONTROL_PLANE_DOMAIN_NAME"),
    )
    agentcore.add_argument(
        "--control-plane-verified-image-uri",
        default=_env("AXON_CONTROL_PLANE_VERIFIED_IMAGE_URI"),
    )
    agentcore.add_argument(
        "--control-plane-certificate-arn",
        default=_env("AXON_CONTROL_PLANE_CERTIFICATE_ARN"),
    )
    agentcore.add_argument(
        "--control-plane-public-hosted-zone-id",
        default=_env("AXON_CONTROL_PLANE_PUBLIC_HOSTED_ZONE_ID"),
    )
    agentcore.add_argument(
        "--control-plane-approved-ingress-prefix-list-id",
        default=_env(
            "AXON_CONTROL_PLANE_APPROVED_INGRESS_PREFIX_LIST_ID"
        ),
    )
    agentcore.add_argument(
        "--control-plane-approved-https-prefix-list-id",
        default=_env(
            "AXON_CONTROL_PLANE_APPROVED_HTTPS_PREFIX_LIST_ID"
        ),
    )
    agentcore.add_argument(
        "--control-plane-allowed-viewer-cidr",
        action="append",
        help=(
            "CloudFront viewer CIDR; repeat for each reviewed public network"
        ),
    )
    agentcore.add_argument(
        "--control-plane-scim-tenants-secret-arn",
        default=_env("AXON_CONTROL_PLANE_SCIM_TENANTS_SECRET_ARN"),
    )
    agentcore.add_argument(
        "--control-plane-saml-login-path",
        default=_env(
            "AXON_CONTROL_PLANE_SAML_LOGIN_PATH",
            DEFAULT_SAML_LOGIN_PATH,
        ),
    )
    agentcore.add_argument(
        "--oidc-issuer",
        default=_env("AXON_OIDC_ISSUER"),
    )
    agentcore.add_argument(
        "--oidc-discovery-url",
        default=_env("AXON_OIDC_DISCOVERY_URL"),
    )
    agentcore.add_argument(
        "--oidc-client-id",
        default=_env("AXON_OIDC_CLIENT_ID"),
    )
    agentcore.add_argument(
        "--oidc-audience",
        default=_env("AXON_OIDC_AUDIENCE"),
    )
    agentcore.add_argument(
        "--oidc-tenant-claim",
        default=_env("AXON_OIDC_TENANT_CLAIM"),
    )
    agentcore.add_argument(
        "--oidc-project-claim",
        default=_env("AXON_OIDC_PROJECT_CLAIM"),
    )


def cmd_setup_agentcore(args: argparse.Namespace) -> None:
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        config = load_agentcore_setup(config_path)
    else:
        config = config_from_args(args)
        config_path = write_agentcore_setup(config, args.output)
        print(f"Wrote authenticated AgentCore setup: {config_path}")

    print(
        "Validated AgentCore setup: "
        f"{config.identity_mode}, {config.aws_region}, "
        f"tenant {config.tenant.tenant_id}, "
        f"project {config.tenant.project_id}"
    )
    if args.show_config:
        print(
            json.dumps(
                redact_sensitive(config.to_dict()),
                indent=2,
                sort_keys=True,
            )
        )
    if not args.deploy:
        print(
            "Deploy with: "
            f"{sys.executable} -m src.gateway.deployment.agentcore_deploy "
            f"--config {config_path}"
        )
        return

    command = [
        sys.executable,
        "-m",
        "src.gateway.deployment.agentcore_deploy",
        "--config",
        str(config_path),
    ]
    if args.yes:
        command.append("--yes")
    if args.bootstrap_cdk:
        command.append("--bootstrap-cdk")
    if args.provider_env_file:
        command.extend(
            ["--provider-env-file", args.provider_env_file]
        )
    if args.rollback_provider_secret_version:
        command.extend(
            [
                "--rollback-provider-secret-version",
                args.rollback_provider_secret_version,
            ]
        )
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise AgentCoreSetupError(
            f"AgentCore deployment failed with exit code {exc.returncode}"
        ) from exc


def local_demo_environment(
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(source if source is not None else os.environ)
    environment.update(
        {
            "AXON_DEPLOYMENT_PROFILE": "development",
            "AXON_REQUIRE_CANONICAL_IDENTITY": "false",
            "LLM_ROUTER_DYNAMODB_ENABLED": "false",
            "AXON_AUTH_MODE": "LOG_ONLY",
            "AXON_LOAD_DEMO_DATA": "true",
            "AXON_ATHENA_QUERY_ENABLED": "false",
            "AXON_CONTROL_PLANE_ONLY": "false",
        }
    )
    return environment


def cmd_setup_local_demo(args: argparse.Namespace) -> None:
    warning = "NON-PRODUCTION LOCAL DEMO: seeded fictional data and LOG_ONLY authentication accept anonymous requests."
    print(warning, file=os.sys.stderr)
    if not args.start:
        print("Start it explicitly with: uv run axon setup local-demo --start --acknowledge-non-production")
        return
    if not args.acknowledge_non_production:
        raise AgentCoreSetupError("--start requires --acknowledge-non-production")
    os.execvpe(
        sys.executable,
        [sys.executable, "-m", "src.gateway.local_server"],
        local_demo_environment(),
    )
