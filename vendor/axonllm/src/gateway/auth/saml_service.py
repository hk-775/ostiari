"""Managed Cognito federation contract for enterprise SAML login.

AxonLLM is not a SAML service provider. Production browser authentication is
owned by Cognito and the selected control-plane endpoint:

* Cognito validates the SAML protocol, signatures, issuer, audience,
  destination, recipient, timestamps, request correlation, and replay.
* Cognito owns RelayState. Custom-domain mode uses the ALB session; CloudFront
  mode exchanges the code with S256 PKCE and stores only an opaque app session.
* AxonLLM validates the resulting OIDC identity and resolves all authority
  through the canonical principal repository.

Accepting a SAML assertion in this process would create a second session and
identity authority without a distributed request/replay store or session-key
lifecycle.  The direct ACS and SP metadata methods therefore fail closed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

MANAGED_COGNITO_MODE = "managed-cognito"
CUSTOM_DOMAIN_ENDPOINT_MODE = "custom-domain"
CLOUDFRONT_ENDPOINT_MODE = "cloudfront"
DEFAULT_LOGIN_PATH = "/admin/dashboard"
APP_LOGIN_PATH = "/auth/login"
MAX_RETURN_TO_BYTES = 2048

LEGACY_DIRECT_SAML_ENV_VARS = frozenset(
    {
        "AXON_SAML_SP_ENTITY_ID",
        "AXON_SAML_ACS_URL",
        "AXON_SAML_IDP_ENTITY_ID",
        "AXON_SAML_IDP_SSO_URL",
        "AXON_SAML_IDP_CERT",
        "AXON_SAML_IDP_CERT_FILE",
    }
)

_AWS_DNS_SUFFIXES = {
    "aws": "amazonaws.com",
    "aws-cn": "amazonaws.com.cn",
    "aws-us-gov": "amazonaws.com",
}
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_REGION_PATTERN = re.compile(r"^[a-z0-9-]{3,32}$")
_RESERVED_LOGIN_PREFIXES = ("/auth", "/saml", "/scim", "/oauth2")
_NON_LOGIN_PATHS = frozenset({"/", "/health", "/ready"})


class SamlError(ValueError):
    """Raised when the managed SAML handoff cannot be performed safely."""


@dataclass(frozen=True)
class SamlConfig:
    """Inputs that prove the managed Cognito trust boundary is active."""

    federation_mode: str = ""
    login_path: str = DEFAULT_LOGIN_PATH
    deployment_profile: str = "development"
    auth_mode: str = "ENFORCE"
    canonical_identity_required: bool = False
    control_plane_only: bool = False
    aws_region: str = ""
    oidc_issuer: str = ""
    oidc_audience: str = ""
    alb_signer_arn: str = ""
    alb_client_id: str = ""
    alb_issuer: str = ""
    endpoint_mode: str = CUSTOM_DOMAIN_ENDPOINT_MODE
    browser_auth_client_id: str = ""
    legacy_direct_configuration: bool = False

    @property
    def configuration_error(self) -> str | None:
        """Return a non-secret reason this handoff must remain disabled."""
        if self.legacy_direct_configuration:
            return (
                "legacy direct-SP SAML settings are unsupported; configure "
                "the SAML identity provider on the Cognito user pool"
            )
        if self.federation_mode != MANAGED_COGNITO_MODE:
            return "managed Cognito SAML federation is not enabled"
        if self.deployment_profile != "production":
            return "managed SAML federation requires the production profile"
        if self.auth_mode != "ENFORCE":
            return "managed SAML federation requires enforced authentication"
        if not self.canonical_identity_required:
            return "managed SAML federation requires canonical identity"
        if not self.control_plane_only:
            return "managed SAML federation is available only on the control plane"
        if self.endpoint_mode not in {
            CUSTOM_DOMAIN_ENDPOINT_MODE,
            CLOUDFRONT_ENDPOINT_MODE,
        }:
            return "the control-plane endpoint mode is invalid"
        partition = _partition_for_region(self.aws_region)
        if self.endpoint_mode == CUSTOM_DOMAIN_ENDPOINT_MODE:
            if not _is_alb_trust_config(
                signer_arn=self.alb_signer_arn,
                client_id=self.alb_client_id,
                issuer=self.alb_issuer,
                region=self.aws_region,
            ):
                return "the regional ALB trust configuration is invalid"
            partition = self.alb_signer_arn.split(":", 2)[1]
        if not _is_cognito_issuer(
            self.oidc_issuer,
            region=self.aws_region,
            partition=partition,
        ):
            return "managed SAML federation requires a Cognito OIDC issuer"
        if self.endpoint_mode == CUSTOM_DOMAIN_ENDPOINT_MODE:
            if (
                not self.oidc_audience
                or self.oidc_audience != self.alb_client_id
            ):
                return "the OIDC audience must match the ALB Cognito client"
        elif (
            not self.browser_auth_client_id
            or self.oidc_audience != self.browser_auth_client_id
        ):
            return (
                "the OIDC audience must match the public browser "
                "Cognito client"
            )
        try:
            _safe_local_target(self.login_path)
        except SamlError:
            return "the managed SAML login path is unsafe"
        return None

    @property
    def enabled(self) -> bool:
        return self.configuration_error is None


class SamlService:
    """Safe application handoff to ALB/Cognito-managed SAML federation."""

    def __init__(self, config: SamlConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def configuration_error(self) -> str | None:
        return self._config.configuration_error

    def login_target(self, return_to: str | None = None) -> str:
        """Return the ALB route or app login route that starts Cognito auth."""
        if not self.enabled:
            raise SamlError("managed Cognito SAML federation is unavailable")
        target = _safe_local_target(return_to or self._config.login_path)
        if self._config.endpoint_mode == CLOUDFRONT_ENDPOINT_MODE:
            return f"{APP_LOGIN_PATH}?{urlencode({'return_to': target})}"
        return target

    def handle_acs(self, _saml_response_b64: str) -> None:
        """Reject direct assertions; Cognito is the only SAML protocol endpoint."""
        raise SamlError(
            "direct SAML assertions are disabled; use managed Cognito federation"
        )

    def sp_metadata(self) -> None:
        """Reject direct-SP metadata; the IdP must use Cognito's SP metadata."""
        raise SamlError(
            "AxonLLM is not a SAML service provider; use Cognito SP metadata"
        )


def load_saml_config(
    *,
    deployment_profile: str = "development",
    auth_mode: str = "ENFORCE",
    canonical_identity_required: bool = False,
    control_plane_only: bool = False,
    aws_region: str = "",
    oidc_issuer: str = "",
    oidc_audience: str = "",
    alb_signer_arn: str = "",
    alb_client_id: str = "",
    alb_issuer: str = "",
    endpoint_mode: str = CUSTOM_DOMAIN_ENDPOINT_MODE,
    browser_auth_client_id: str = "",
    environ: Mapping[str, str] | None = None,
) -> SamlConfig:
    """Load only the application-side managed-federation switch and target.

    IdP metadata, certificates, and assertions intentionally never enter this
    process.  Presence of any retired direct-SP variable disables the handoff
    so an old deployment cannot appear to provide authentication.
    """
    values = os.environ if environ is None else environ
    return SamlConfig(
        federation_mode=values.get("AXON_SAML_FEDERATION_MODE", "").strip(),
        login_path=values.get(
            "AXON_SAML_LOGIN_PATH",
            DEFAULT_LOGIN_PATH,
        ).strip(),
        deployment_profile=deployment_profile,
        auth_mode=auth_mode,
        canonical_identity_required=canonical_identity_required,
        control_plane_only=control_plane_only,
        aws_region=aws_region,
        oidc_issuer=oidc_issuer,
        oidc_audience=oidc_audience,
        alb_signer_arn=alb_signer_arn,
        alb_client_id=alb_client_id,
        alb_issuer=alb_issuer,
        endpoint_mode=endpoint_mode,
        browser_auth_client_id=browser_auth_client_id,
        legacy_direct_configuration=any(
            values.get(name, "").strip()
            for name in LEGACY_DIRECT_SAML_ENV_VARS
        ),
    )


def _partition_for_region(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


def _is_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
    )


def _is_cognito_issuer(
    value: str,
    *,
    region: str,
    partition: str,
) -> bool:
    if not _is_https_url(value):
        return False
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    dns_suffix = _AWS_DNS_SUFFIXES.get(partition)
    if (
        dns_suffix is None
        or hostname != f"cognito-idp.{region}.{dns_suffix}"
    ):
        return False
    path_parts = [part for part in parsed.path.split("/") if part]
    return (
        len(path_parts) == 1
        and parsed.path == f"/{path_parts[0]}"
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", path_parts[0]) is not None
    )


def _is_alb_trust_config(
    *,
    signer_arn: str,
    client_id: str,
    issuer: str,
    region: str,
) -> bool:
    if (
        not signer_arn
        or signer_arn != signer_arn.strip()
        or not client_id
        or client_id != client_id.strip()
        or len(client_id.encode("utf-8", errors="ignore")) > 2048
        or any(
            not character.isprintable() or character.isspace()
            for character in client_id
        )
        or _REGION_PATTERN.fullmatch(region) is None
    ):
        return False
    arn_parts = signer_arn.split(":", 5)
    if len(arn_parts) != 6:
        return False
    arn, partition, service, signer_region, account_id, resource = arn_parts
    dns_suffix = _AWS_DNS_SUFFIXES.get(partition)
    return (
        arn == "arn"
        and dns_suffix is not None
        and service == "elasticloadbalancing"
        and signer_region == region
        and len(account_id) == 12
        and account_id.isdigit()
        and resource.startswith("loadbalancer/app/")
        and issuer == (
            f"https://public-keys.auth.elb.{region}.{dns_suffix}"
        )
    )


def _safe_local_target(value: str) -> str:
    """Validate an application-local redirect without creating an open redirect."""
    if not isinstance(value, str):
        raise SamlError("return_to must be a string")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise SamlError("return_to is not valid UTF-8") from exc
    if not value or encoded_length > MAX_RETURN_TO_BYTES:
        raise SamlError("return_to is empty or too long")
    if (
        _INVALID_PERCENT_ESCAPE.search(value)
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        raise SamlError("return_to contains control characters")

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise SamlError("return_to is malformed") from exc
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise SamlError("return_to must be a same-origin path")
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        raise SamlError("return_to must be an absolute local path")

    decoded_path = parsed.path
    for _ in range(3):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    lowered_path = decoded_path.casefold()
    if (
        decoded_path.startswith("//")
        or "//" in decoded_path
        or "\\" in decoded_path
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in decoded_path
        )
        or any(
            segment in {".", ".."}
            for segment in decoded_path.split("/")
        )
        or any(
            lowered_path == prefix
            or lowered_path.startswith(f"{prefix}/")
            for prefix in _RESERVED_LOGIN_PREFIXES
        )
        or lowered_path in _NON_LOGIN_PATHS
    ):
        raise SamlError("return_to does not identify a protected application path")

    return urlunsplit(("", "", parsed.path, parsed.query, ""))
