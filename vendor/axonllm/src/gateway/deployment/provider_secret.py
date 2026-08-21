#!/usr/bin/env python3
"""Synchronize and roll back AgentCore provider credentials without logging values."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import shlex
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4


ALLOWED_SECRET_FIELDS = (
    "AI21_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "COHERE_API_KEY",
    "FIREWORKS_API_KEY",
    "GCP_CREDENTIALS_JSON",
    "GCP_LOCATION",
    "GCP_PROJECT_ID",
    "GOOGLE_AI_API_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "TOGETHER_API_KEY",
    "VERTEX_AI_ENDPOINT",
    "XAI_API_KEY",
)
SUPPORTED_PROVIDERS = frozenset(
    {
        "ai21",
        "anthropic",
        "azure_openai",
        "bedrock",
        "bedrock-mantle",
        "cohere",
        "fireworks",
        "google_ai",
        "groq",
        "openai",
        "together",
        "vertex_ai",
        "xai",
    }
)
PROVIDER_REQUIRED_FIELDS = {
    "ai21": ("AI21_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "azure_openai": (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
    ),
    "cohere": ("COHERE_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "google_ai": ("GOOGLE_AI_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "vertex_ai": (
        "GCP_CREDENTIALS_JSON",
        "GCP_PROJECT_ID",
    ),
    "xai": ("XAI_API_KEY",),
}
PROVIDER_SECRET_FIELDS = {
    **PROVIDER_REQUIRED_FIELDS,
    "bedrock": (),
    "bedrock-mantle": (),
    "vertex_ai": (
        "GCP_CREDENTIALS_JSON",
        "GCP_LOCATION",
        "GCP_PROJECT_ID",
        "VERTEX_AI_ENDPOINT",
    ),
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class ProviderSecretError(RuntimeError):
    """A safe-to-report provider-secret lifecycle failure."""


@dataclass(frozen=True)
class ProviderSecretVersion:
    secret_arn: str
    version_id: str
    previous_version_id: str | None
    changed: bool
    configured_fields: tuple[str, ...]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        """Return metadata that cannot reconstruct a provider credential."""
        return {
            "secretArn": self.secret_arn,
            "versionId": self.version_id,
            "previousVersionId": self.previous_version_id,
            "changed": self.changed,
            "configuredFields": list(self.configured_fields),
            "fingerprint": self.fingerprint,
        }


def load_provider_environment_file(path: str | Path) -> dict[str, str]:
    """Read allowlisted values from a non-executable, owner-only env file."""
    env_path = Path(path).expanduser().resolve()
    try:
        file_stat = env_path.stat()
        raw = env_path.read_bytes()
    except OSError as exc:
        raise ProviderSecretError(
            f"unable to read provider environment file {env_path}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ProviderSecretError("provider environment file must be a regular file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ProviderSecretError(
            "provider environment file must not be accessible by group or others"
        )
    if len(raw) > 256 * 1024:
        raise ProviderSecretError("provider environment file exceeds 256 KiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderSecretError(
            "provider environment file must contain UTF-8 text"
        ) from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if _ENV_ASSIGNMENT.fullmatch(name) is None:
            raise ProviderSecretError(
                f"provider environment file line {line_number} has an invalid name"
            )
        if name not in ALLOWED_SECRET_FIELDS:
            continue
        if name in values:
            raise ProviderSecretError(
                f"provider environment file repeats {name}"
            )
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError as exc:
            raise ProviderSecretError(
                f"provider environment file line {line_number} is malformed"
            ) from exc
        if len(tokens) > 1:
            raise ProviderSecretError(
                f"provider environment file line {line_number} is malformed"
            )
        values[name] = tokens[0] if tokens else ""
    return values


def normalize_enabled_providers(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate and normalize a non-empty provider allowlist."""
    if not isinstance(values, (tuple, list)) or not values:
        raise ProviderSecretError("enabled providers must be a non-empty list")
    normalized: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value not in SUPPORTED_PROVIDERS
        ):
            raise ProviderSecretError("enabled providers contain an unsupported name")
        if value in normalized:
            raise ProviderSecretError("enabled providers must not contain duplicates")
        normalized.append(value)
    return tuple(normalized)


def _validated_endpoint(value: str, field_name: str) -> str:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ProviderSecretError(f"{field_name} is not a valid HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderSecretError(f"{field_name} must be an HTTPS endpoint")
    return value.rstrip("/")


def _validated_value(field_name: str, value: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 64 * 1024
        or "\x00" in value
    ):
        raise ProviderSecretError(
            f"environment variable {field_name} is malformed"
        )
    if field_name != "GCP_CREDENTIALS_JSON" and (
        "\r" in value or "\n" in value
    ):
        raise ProviderSecretError(
            f"environment variable {field_name} is malformed"
        )
    if field_name in {"AZURE_OPENAI_ENDPOINT", "VERTEX_AI_ENDPOINT"}:
        return _validated_endpoint(value, field_name)
    if field_name == "GCP_CREDENTIALS_JSON":
        try:
            document = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderSecretError(
                "GCP_CREDENTIALS_JSON is not valid JSON"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("type") not in {"external_account", "service_account"}
        ):
            raise ProviderSecretError(
                "GCP_CREDENTIALS_JSON must be a supported Google credential document"
            )
        return json.dumps(document, separators=(",", ":"), sort_keys=True)
    if field_name in {"GCP_PROJECT_ID", "GCP_LOCATION"}:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ProviderSecretError(
                f"environment variable {field_name} is malformed"
            )
    return value


def collect_provider_secret(
    environ: Mapping[str, str],
    enabled_providers: tuple[str, ...] | list[str],
) -> dict[str, str]:
    """Collect only approved fields and require credentials for enabled HTTP providers."""
    enabled = normalize_enabled_providers(enabled_providers)
    selected_fields = {
        field_name
        for provider in enabled
        for field_name in PROVIDER_SECRET_FIELDS[provider]
    }
    values: dict[str, str] = {}
    for field_name in sorted(selected_fields):
        value = environ.get(field_name)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ProviderSecretError(
                f"environment variable {field_name} must be a string"
            )
        values[field_name] = _validated_value(field_name, value)

    missing: list[str] = []
    for provider in enabled:
        for field_name in PROVIDER_REQUIRED_FIELDS.get(provider, ()):
            if field_name not in values:
                missing.append(f"{provider}:{field_name}")
    if missing:
        raise ProviderSecretError(
            "enabled providers are missing credential environment variables: "
            + ", ".join(sorted(missing))
        )
    return values


def _parse_current_secret(
    response: Mapping[str, Any],
    *,
    allow_bootstrap_placeholder: bool = False,
) -> tuple[dict[str, str], str, bool]:
    version_id = response.get("VersionId")
    secret_string = response.get("SecretString")
    if not isinstance(version_id, str) or not version_id:
        raise ProviderSecretError("provider secret has no current version identifier")
    if not isinstance(secret_string, str):
        raise ProviderSecretError("provider secret must use SecretString")
    try:
        payload = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise ProviderSecretError("provider secret does not contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderSecretError("provider secret must contain a JSON object")
    unexpected = set(payload).difference(ALLOWED_SECRET_FIELDS)
    bootstrap_placeholder = unexpected == {"placeholder"}
    if unexpected and not (
        allow_bootstrap_placeholder and bootstrap_placeholder
    ):
        raise ProviderSecretError(
            "provider secret contains unsupported fields"
        )
    current: dict[str, str] = {}
    for field_name in ALLOWED_SECRET_FIELDS:
        value = payload.get(field_name)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ProviderSecretError(
                f"provider secret field {field_name} is malformed"
            )
        current[field_name] = _validated_value(field_name, value)
    return current, version_id, bootstrap_placeholder


def _serialized(values: Mapping[str, str]) -> str:
    return json.dumps(
        dict(values),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _fingerprint(serialized: str) -> str:
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def synchronize_provider_secret(
    client: Any,
    *,
    secret_arn: str,
    environ: Mapping[str, str],
    enabled_providers: tuple[str, ...] | list[str],
) -> ProviderSecretVersion:
    """Replace AWSCURRENT with the complete approved environment-backed document."""
    if not isinstance(secret_arn, str) or not secret_arn.startswith("arn:"):
        raise ProviderSecretError("provider secret ARN is invalid")
    desired = collect_provider_secret(environ, enabled_providers)
    desired_json = _serialized(desired)
    try:
        current_response = client.get_secret_value(
            SecretId=secret_arn,
            VersionStage="AWSCURRENT",
        )
        (
            current,
            current_version,
            bootstrap_placeholder,
        ) = _parse_current_secret(
            current_response,
            allow_bootstrap_placeholder=True,
        )
    except ProviderSecretError:
        raise
    except Exception as exc:
        raise ProviderSecretError(
            "unable to read the current provider secret version"
        ) from exc

    current_json = _serialized(current)
    if (
        not bootstrap_placeholder
        and hmac.compare_digest(current_json, desired_json)
    ):
        return ProviderSecretVersion(
            secret_arn=secret_arn,
            version_id=current_version,
            previous_version_id=None,
            changed=False,
            configured_fields=tuple(sorted(desired)),
            fingerprint=_fingerprint(desired_json),
        )

    try:
        response = client.put_secret_value(
            SecretId=secret_arn,
            ClientRequestToken=str(uuid4()),
            SecretString=desired_json,
            VersionStages=["AWSCURRENT"],
        )
    except Exception as exc:
        raise ProviderSecretError(
            "unable to publish the provider secret version"
        ) from exc
    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or not version_id:
        raise ProviderSecretError(
            "Secrets Manager did not return a provider secret version identifier"
        )
    return ProviderSecretVersion(
        secret_arn=secret_arn,
        version_id=version_id,
        previous_version_id=current_version,
        changed=True,
        configured_fields=tuple(sorted(desired)),
        fingerprint=_fingerprint(desired_json),
    )


def rollback_provider_secret(
    client: Any,
    *,
    secret_arn: str,
    version_id: str,
    enabled_providers: tuple[str, ...] | list[str],
) -> ProviderSecretVersion:
    """Move AWSCURRENT to a reviewed prior version without reading its values."""
    if not isinstance(secret_arn, str) or not secret_arn.startswith("arn:"):
        raise ProviderSecretError("provider secret ARN is invalid")
    enabled = normalize_enabled_providers(enabled_providers)
    if (
        not isinstance(version_id, str)
        or not version_id
        or version_id != version_id.strip()
        or len(version_id) > 256
    ):
        raise ProviderSecretError("provider secret rollback version is invalid")
    try:
        metadata = client.describe_secret(SecretId=secret_arn)
        stages_by_version = metadata.get("VersionIdsToStages")
        if not isinstance(stages_by_version, dict):
            raise ProviderSecretError(
                "provider secret version metadata is malformed"
            )
        current_versions = [
            candidate
            for candidate, stages in stages_by_version.items()
            if isinstance(stages, list) and "AWSCURRENT" in stages
        ]
        if len(current_versions) != 1:
            raise ProviderSecretError(
                "provider secret must have exactly one AWSCURRENT version"
            )
        if version_id not in stages_by_version:
            raise ProviderSecretError(
                "provider secret rollback version does not exist"
            )
        current_version = current_versions[0]
        selected = client.get_secret_value(
            SecretId=secret_arn,
            VersionId=version_id,
        )
        values, selected_version, _ = _parse_current_secret(selected)
        if selected_version != version_id:
            raise ProviderSecretError(
                "provider secret rollback version does not match the request"
            )
        selected_values = collect_provider_secret(values, enabled)
        if selected_values != values:
            raise ProviderSecretError(
                "provider secret rollback version contains fields for "
                "disabled providers"
            )
        values = selected_values
        if current_version != version_id:
            client.update_secret_version_stage(
                SecretId=secret_arn,
                VersionStage="AWSCURRENT",
                MoveToVersionId=version_id,
                RemoveFromVersionId=current_version,
            )
    except ProviderSecretError:
        raise
    except Exception as exc:
        raise ProviderSecretError(
            "unable to roll back the provider secret version"
        ) from exc
    serialized = _serialized(values)
    return ProviderSecretVersion(
        secret_arn=secret_arn,
        version_id=selected_version,
        previous_version_id=(
            current_version if current_version != selected_version else None
        ),
        changed=current_version != selected_version,
        configured_fields=tuple(sorted(values)),
        fingerprint=_fingerprint(serialized),
    )
