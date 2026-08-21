"""Validated deployment configuration for the Ostiari CDK app."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,31}$")
_ORG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)
_PROFILES = {
    "aws-demo": (True, False, False),
    "aws-empty": (False, False, False),
    "aws-agentcore-demo": (True, True, False),
    "aws-agentcore-empty": (False, True, False),
    "production": (False, False, True),
    "production-agentcore": (False, True, True),
}


class ConfigurationError(ValueError):
    pass


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a JSON object")
    return value


def _https(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an HTTPS URL")
    return value.rstrip("/")


def _required(mapping: dict[str, Any], key: str, section: str) -> str:
    value = str(mapping.get(key, "")).strip()
    if not value:
        raise ConfigurationError(f"{section}.{key} is required")
    return value


def _secret_arn(value: str, name: str, *, account: str, region: str) -> str:
    prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:"
    if not value.startswith(prefix) or not re.search(r"-[A-Za-z0-9]{6}$", value):
        raise ConfigurationError(
            f"{name} must be a same-account, same-region Secrets Manager ARN "
            "including its six-character suffix"
        )
    return value


def _ecr_image(value: str, name: str, *, account: str, region: str) -> str:
    prefix = f"{account}.dkr.ecr.{region}.amazonaws.com/"
    if not value.startswith(prefix) or not re.search(r"@sha256:[a-f0-9]{64}$", value):
        raise ConfigurationError(
            f"{name} must be a same-account, same-region ECR image pinned by "
            "a sha256 manifest digest"
        )
    return value


@dataclass(frozen=True)
class DeploymentConfig:
    name: str
    profile: str
    account: str
    region: str
    demo: bool
    agentcore: bool
    production: bool
    allowed_cidrs: tuple[str, ...]
    org_id: str
    images: dict[str, str]
    secrets: dict[str, str]
    auth: dict[str, str]
    domains: dict[str, str]
    desired_count: int
    alarm_topic_arn: str

    @property
    def stack_name(self) -> str:
        return f"Ostiari-{self.name}"

    @property
    def namespace(self) -> str:
        return f"{self.name}.ostiari.local"

    @classmethod
    def load(cls, path: str | Path | None = None) -> DeploymentConfig:
        configured = path or os.environ.get("OSTIARI_DEPLOY_CONFIG", "")
        if not configured:
            raise ConfigurationError("OSTIARI_DEPLOY_CONFIG is required")
        source = Path(configured).expanduser()
        try:
            raw = json.loads(source.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("Deployment config must be a JSON object")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeploymentConfig:
        name = str(raw.get("name", "")).strip()
        if not _NAME.fullmatch(name):
            raise ConfigurationError(
                "name must start with a letter and contain at most 32 letters, digits, or hyphens"
            )
        account = str(raw.get("account", "")).strip()
        if not re.fullmatch(r"\d{12}", account):
            raise ConfigurationError("account must be a 12-digit AWS account id")
        region = str(raw.get("region", "")).strip()
        if not region:
            raise ConfigurationError("region is required")

        profile = str(raw.get("profile", "")).strip()
        production = bool(raw.get("production"))
        demo = bool(raw.get("demo"))
        agentcore = bool(raw.get("agentcore"))
        expected = _PROFILES.get(profile)
        if expected is None:
            raise ConfigurationError(f"unsupported deployment profile: {profile}")
        if expected != (demo, agentcore, production):
            raise ConfigurationError(
                f"profile {profile} does not match demo/agentcore/production settings"
            )
        if production and demo:
            raise ConfigurationError("production profiles may not seed demo data")

        cidrs_raw = raw.get("allowed_cidrs")
        if cidrs_raw is None:
            cidrs_raw = [raw.get("allowed_cidr", "")]
        if not isinstance(cidrs_raw, list):
            raise ConfigurationError("allowed_cidrs must be an array")
        allowed_networks = []
        for value in cidrs_raw:
            cidr = str(value).strip()
            if not cidr:
                continue
            try:
                allowed_networks.append(str(ipaddress.ip_network(cidr, strict=False)))
            except ValueError as exc:
                raise ConfigurationError(f"invalid allowed CIDR: {cidr}") from exc
        allowed_cidrs = tuple(dict.fromkeys(allowed_networks))
        if not allowed_cidrs:
            raise ConfigurationError("at least one allowed CIDR is required")

        org_id = str(raw.get("org_id", "default")).strip()
        if not _ORG.fullmatch(org_id):
            raise ConfigurationError("org_id has an invalid format")

        config = cls(
            name=name,
            profile=profile,
            account=account,
            region=region,
            demo=demo,
            agentcore=agentcore,
            production=production,
            allowed_cidrs=allowed_cidrs,
            org_id=org_id,
            images={
                key: str(value).strip()
                for key, value in _mapping(raw.get("images"), "images").items()
            },
            secrets={
                key: str(value).strip()
                for key, value in _mapping(raw.get("secrets"), "secrets").items()
            },
            auth={
                key: str(value).strip() for key, value in _mapping(raw.get("auth"), "auth").items()
            },
            domains={
                key: str(value).strip()
                for key, value in _mapping(raw.get("domains"), "domains").items()
            },
            desired_count=int(raw.get("desired_count", 2 if production else 1)),
            alarm_topic_arn=str(raw.get("alarm_topic_arn", "")).strip(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.desired_count < 1:
            raise ConfigurationError("desired_count must be positive")
        if self.production and self.desired_count < 2:
            raise ConfigurationError("production desired_count must be at least 2")
        if any(
            "REPLACE_" in value
            for section in (self.images, self.secrets, self.auth, self.domains)
            for value in section.values()
        ):
            raise ConfigurationError("replace every REPLACE_* placeholder")
        if not self.production:
            return

        for key in ("control_plane", "gateway", "frontend"):
            image = _required(self.images, key, "images")
            _ecr_image(
                image,
                f"images.{key}",
                account=self.account,
                region=self.region,
            )
        for key in (
            "jwt",
            "admin_password",
            "encryption_key",
            "config_admin_key",
            "gateway_agent_token",
            "workload_client_secret",
        ):
            arn = _required(self.secrets, key, "secrets")
            _secret_arn(
                arn,
                f"secrets.{key}",
                account=self.account,
                region=self.region,
            )

        dashboard = _required(self.domains, "dashboard", "domains")
        gateway = _required(self.domains, "gateway", "domains")
        if "://" in dashboard or "://" in gateway:
            raise ConfigurationError("domain values must be hostnames, not URLs")
        if not _HOSTNAME.fullmatch(dashboard) or not _HOSTNAME.fullmatch(gateway):
            raise ConfigurationError("dashboard and gateway domains must be valid hostnames")
        if dashboard.rstrip(".").lower() == gateway.rstrip(".").lower():
            raise ConfigurationError("dashboard and gateway domains must be different")
        certificate = _required(self.domains, "certificate_arn", "domains")
        certificate_prefix = f"arn:aws:acm:{self.region}:{self.account}:certificate/"
        if not certificate.startswith(certificate_prefix):
            raise ConfigurationError(
                "domains.certificate_arn must be a same-account, same-region ACM ARN"
            )
        zone_id = self.domains.get("hosted_zone_id", "")
        zone_name = self.domains.get("hosted_zone_name", "").rstrip(".")
        if bool(zone_id) != bool(zone_name):
            raise ConfigurationError(
                "domains.hosted_zone_id and hosted_zone_name must be set together"
            )
        if zone_name:
            suffix = f".{zone_name.lower()}"
            for hostname in (dashboard, gateway):
                normalized = hostname.rstrip(".").lower()
                if normalized != zone_name.lower() and not normalized.endswith(suffix):
                    raise ConfigurationError(f"{hostname} is not inside hosted zone {zone_name}")

        _https(_required(self.auth, "workload_issuer", "auth"), "auth.workload_issuer")
        _https(_required(self.auth, "workload_token_url", "auth"), "auth.workload_token_url")
        _required(self.auth, "workload_audience", "auth")
        _required(self.auth, "gateway_client_id", "auth")
        _https(_required(self.auth, "agent_issuer", "auth"), "auth.agent_issuer")
        _required(self.auth, "agent_audience", "auth")
        try:
            rate_limit = int(self.auth.get("gateway_rate_limit_rpm", "600"))
        except ValueError as exc:
            raise ConfigurationError("auth.gateway_rate_limit_rpm must be an integer") from exc
        if rate_limit < 1:
            raise ConfigurationError("auth.gateway_rate_limit_rpm must be positive")

        browser_fields = (
            self.auth.get("browser_oidc_issuer", ""),
            self.auth.get("browser_oidc_client_id", ""),
            self.auth.get("browser_oidc_redirect_uri", ""),
        )
        if any(browser_fields) and not all(browser_fields):
            raise ConfigurationError(
                "browser_oidc_issuer, browser_oidc_client_id, and "
                "browser_oidc_redirect_uri must be set together"
            )
        if all(browser_fields):
            _https(browser_fields[0], "auth.browser_oidc_issuer")
            _https(browser_fields[2], "auth.browser_oidc_redirect_uri")

        if self.agentcore:
            image = _required(self.images, "agentcore", "images")
            _ecr_image(
                image,
                "images.agentcore",
                account=self.account,
                region=self.region,
            )
            for key in ("agentcore_client_id", "agentcore_token_url"):
                _required(self.auth, key, "auth")
            _https(self.auth["agentcore_token_url"], "auth.agentcore_token_url")
            arn = _required(self.secrets, "agentcore_client_secret", "secrets")
            _secret_arn(
                arn,
                "secrets.agentcore_client_secret",
                account=self.account,
                region=self.region,
            )
