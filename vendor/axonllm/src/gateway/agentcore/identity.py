"""Trusted AgentCore context to canonical AxonLLM principal resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.gateway.auth.principal import (
    PrincipalResolver,
    canonical_request_context,
)
from src.gateway.models import AuthMethod, Principal, RequestContext

from .errors import AgentCoreAdapterError
from .runtime import OIDCTokenVerifier


AUTHORIZATION_HEADER = "authorization"
FACADE_IDENTITY_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-identity-token"


@dataclass(frozen=True)
class InvocationIdentity:
    """Canonical authority plus the signed tenant/project resource hints."""

    principal: Principal
    request_context: RequestContext
    tenant_id: str
    project_id: str


def _single_header(
    headers: Mapping[Any, Any],
    expected_name: str,
) -> str | None:
    matches = [value for name, value in headers.items() if isinstance(name, str) and name.casefold() == expected_name]
    if not matches:
        return None
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise AgentCoreAdapterError(
            401,
            "invalid_runtime_identity",
            "Invocation identity is invalid.",
        )
    return matches[0]


def extract_bearer_token(context: Any) -> str:
    """Read one identity token only from SDK-populated request headers."""
    headers = getattr(context, "request_headers", None)
    if not isinstance(headers, Mapping):
        raise AgentCoreAdapterError(
            401,
            "runtime_identity_required",
            "Invocation identity is required.",
        )

    authorization = _single_header(headers, AUTHORIZATION_HEADER)
    facade_identity = _single_header(headers, FACADE_IDENTITY_HEADER)
    candidates = [value for value in (authorization, facade_identity) if value is not None]
    if len(candidates) != 1:
        raise AgentCoreAdapterError(
            401,
            "invalid_runtime_identity",
            "Exactly one invocation identity is required.",
        )

    scheme, separator, token = candidates[0].partition(" ")
    if (
        separator != " "
        or scheme.casefold() != "bearer"
        or not token
        or token != token.strip()
        or any(character.isspace() for character in token)
    ):
        raise AgentCoreAdapterError(
            401,
            "invalid_runtime_identity",
            "Invocation identity is invalid.",
        )
    return token


def _required_claim(value: Any, claim_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AgentCoreAdapterError(
            401,
            "invalid_runtime_identity",
            f"Verified identity is missing {claim_name}.",
        )
    return value


def _sanitize_verified_context(context: RequestContext) -> RequestContext:
    """Keep identity hints and discard all token-supplied authority."""
    if context.auth_method is not AuthMethod.OIDC_JWT:
        raise AgentCoreAdapterError(
            401,
            "invalid_runtime_identity",
            "Verified identity has an unsupported authentication method.",
        )

    issuer = _required_claim(context.issuer, "issuer")
    subject = _required_claim(context.subject, "subject")
    tenant_id = _required_claim(context.tenant_id, "tenant")
    project_id = _required_claim(context.project_id, "project")
    return RequestContext(
        user_id=subject,
        project_id=project_id,
        roles=[],
        scopes=[],
        auth_method=AuthMethod.OIDC_JWT,
        tenant_id=tenant_id,
        issuer=issuer,
        subject=subject,
    )


async def resolve_invocation_identity(
    context: Any,
    token_verifier: OIDCTokenVerifier,
    principal_resolver: PrincipalResolver,
) -> InvocationIdentity:
    """Verify the runtime token, then resolve server-held tenant authority."""
    token = extract_bearer_token(context)
    try:
        verified = await token_verifier.validate_oidc_jwt(token)
    except Exception as exc:
        raise AgentCoreAdapterError(
            503,
            "identity_verifier_unavailable",
            "Identity verification is temporarily unavailable.",
        ) from exc
    if verified is None:
        raise AgentCoreAdapterError(
            401,
            "invalid_runtime_identity",
            "Invocation identity is invalid.",
        )

    credential_context = _sanitize_verified_context(verified)
    try:
        principal = await principal_resolver.resolve(credential_context)
    except Exception as exc:
        raise AgentCoreAdapterError(
            503,
            "principal_resolver_unavailable",
            "Authorization is temporarily unavailable.",
        ) from exc
    if principal is None:
        raise AgentCoreAdapterError(
            403,
            "tenant_membership_required",
            "Active tenant membership is required.",
        )

    if (
        principal.issuer != credential_context.issuer
        or principal.subject != credential_context.subject
        or principal.auth_method is not credential_context.auth_method
    ):
        raise AgentCoreAdapterError(
            403,
            "tenant_membership_required",
            "Active tenant membership is required.",
        )

    canonical_context = canonical_request_context(
        credential_context,
        principal,
    )
    return InvocationIdentity(
        principal=principal,
        request_context=canonical_context,
        tenant_id=credential_context.tenant_id or "",
        project_id=credential_context.project_id,
    )
