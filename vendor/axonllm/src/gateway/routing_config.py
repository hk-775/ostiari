"""Versioned, credential-free routing configuration snapshots."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from src.gateway.model_registry import ModelRegistry
from src.gateway.routing_config_contract import (
    ROUTING_CONFIG_SCHEMA,
    ROUTING_CONFIG_SIGNATURE_SCHEMA,
    ROUTING_CONFIG_SIGNING_ALGORITHM,
    validate_routing_config_signing_key_arn,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RoutingConfigSnapshot:
    """An immutable control-plane snapshot consumed by router instances.

    This document owns logical models, provider mappings, and routing policy.
    Provider endpoints and credentials remain deployment-owned secrets and are
    intentionally excluded.
    """

    revision: int
    document: str
    sha256: str
    signing_key_arn: str | None = None
    signing_algorithm: str | None = None
    signature_b64: str | None = None

    def __post_init__(self) -> None:
        signature_fields = (
            self.signing_key_arn,
            self.signing_algorithm,
            self.signature_b64,
        )
        if all(value is None for value in signature_fields):
            return
        if any(value is None for value in signature_fields):
            raise ValueError(
                "routing configuration signature metadata is incomplete"
            )
        validate_routing_config_signing_key_arn(self.signing_key_arn or "")
        if self.signing_algorithm != ROUTING_CONFIG_SIGNING_ALGORITHM:
            raise ValueError(
                "routing configuration signing algorithm is unsupported"
            )
        try:
            signature = base64.b64decode(
                self.signature_b64 or "",
                validate=True,
            )
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                "routing configuration signature is not canonical base64"
            ) from exc
        if (
            not signature
            or len(signature) > 512
            or base64.b64encode(signature).decode("ascii")
            != self.signature_b64
        ):
            raise ValueError(
                "routing configuration signature is not canonical base64"
            )

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        revision: int,
    ) -> RoutingConfigSnapshot:
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError(
                "routing configuration revision must be non-negative"
            )
        ModelRegistry.from_config(config, revision=revision)
        document = json.dumps(
            config,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return cls(
            revision=revision,
            document=document,
            sha256=hashlib.sha256(
                document.encode("utf-8")
            ).hexdigest(),
        )

    @classmethod
    def from_document(
        cls,
        document: str,
        *,
        revision: int,
        sha256: str,
        signing_key_arn: str | None = None,
        signing_algorithm: str | None = None,
        signature_b64: str | None = None,
    ) -> RoutingConfigSnapshot:
        """Validate and reconstruct one canonical persisted snapshot."""
        if not isinstance(document, str) or not _SHA256.fullmatch(sha256):
            raise ValueError("routing configuration document is malformed")
        try:
            config = json.loads(document)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "routing configuration document is malformed"
            ) from exc
        if not isinstance(config, dict):
            raise ValueError("routing configuration document is malformed")
        canonical = cls.from_config(config, revision=revision)
        if canonical.document != document:
            raise ValueError(
                "routing configuration document is not canonical"
            )
        if canonical.sha256 != sha256:
            raise ValueError(
                "routing configuration document checksum mismatch"
            )
        return cls(
            revision=revision,
            document=document,
            sha256=sha256,
            signing_key_arn=signing_key_arn,
            signing_algorithm=signing_algorithm,
            signature_b64=signature_b64,
        )

    @classmethod
    def from_registry(
        cls,
        registry: ModelRegistry,
    ) -> RoutingConfigSnapshot:
        return cls.from_config(
            registry.to_config(),
            revision=registry.revision,
        )

    @property
    def config(self) -> dict[str, Any]:
        """Return a detached copy safe for validation and atomic adoption."""
        value = json.loads(self.document)
        if not isinstance(value, dict):
            raise RuntimeError(
                "routing configuration document is malformed"
            )
        return value

    def apply(self, registry: ModelRegistry) -> None:
        """Atomically replace one live registry with this validated snapshot."""
        registry.replace_config(
            self.config,
            revision=self.revision,
        )

    @property
    def is_signed(self) -> bool:
        return self.signature_b64 is not None

    @property
    def signing_payload(self) -> bytes:
        """Canonical bytes whose digest is signed by KMS."""
        return json.dumps(
            {
                "document_sha256": self.sha256,
                "revision": self.revision,
                "schema": ROUTING_CONFIG_SCHEMA,
                "signature_schema": ROUTING_CONFIG_SIGNATURE_SCHEMA,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def signing_digest(self) -> bytes:
        return hashlib.sha256(self.signing_payload).digest()

    @property
    def signature(self) -> bytes:
        if self.signature_b64 is None:
            raise ValueError("routing configuration snapshot is unsigned")
        return base64.b64decode(self.signature_b64, validate=True)

    def with_signature(
        self,
        *,
        signing_key_arn: str,
        signature: bytes,
        signing_algorithm: str = ROUTING_CONFIG_SIGNING_ALGORITHM,
    ) -> RoutingConfigSnapshot:
        if not isinstance(signature, bytes) or not signature:
            raise ValueError(
                "routing configuration signature must be non-empty bytes"
            )
        return RoutingConfigSnapshot(
            revision=self.revision,
            document=self.document,
            sha256=self.sha256,
            signing_key_arn=signing_key_arn,
            signing_algorithm=signing_algorithm,
            signature_b64=base64.b64encode(signature).decode("ascii"),
        )

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": ROUTING_CONFIG_SCHEMA,
            "revision": self.revision,
            "sha256": self.sha256,
            "config": self.config,
        }
        if self.is_signed:
            value["signature"] = {
                "schema": ROUTING_CONFIG_SIGNATURE_SCHEMA,
                "key_arn": self.signing_key_arn,
                "algorithm": self.signing_algorithm,
                "value": self.signature_b64,
            }
        return value
