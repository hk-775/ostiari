"""Dependency-free constants for signed routing configuration."""

from __future__ import annotations

import re

ROUTING_CONFIG_SCHEMA = "axonllm.routing-config/v1"
ROUTING_CONFIG_SIGNATURE_SCHEMA = "axonllm.routing-config-signature/v1"
ROUTING_CONFIG_SIGNING_ALGORITHM = "ECDSA_SHA_256"
ROUTING_CONFIG_SIGNING_MODES = frozenset(
    {"disabled", "verify", "sign-verify"}
)
_KMS_KEY_ARN = re.compile(
    r"^arn:aws(?:-[a-z0-9-]+)?:kms:[a-z0-9-]+:\d{12}:key/"
    r"(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    r"|mrk-[0-9a-fA-F]{32})$"
)


def validate_routing_config_signing_key_arn(value: str) -> str:
    """Return one exact KMS key ARN suitable for routing signatures."""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _KMS_KEY_ARN.fullmatch(value)
    ):
        raise ValueError(
            "routing configuration signing key must be a full KMS key ARN"
        )
    return value


def routing_config_signing_key_region(value: str) -> str:
    """Return the AWS region encoded in one validated KMS key ARN."""
    return validate_routing_config_signing_key_arn(value).split(":", 5)[3]
