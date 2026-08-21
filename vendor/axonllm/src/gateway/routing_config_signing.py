"""AWS KMS authenticity boundary for routing configuration snapshots."""

from __future__ import annotations

import asyncio
import threading
from typing import Protocol

from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_config_contract import (
    ROUTING_CONFIG_SIGNING_ALGORITHM,
    routing_config_signing_key_region,
    validate_routing_config_signing_key_arn,
)


class RoutingConfigSignatureError(RuntimeError):
    """A routing snapshot could not be authenticated safely."""


class RoutingConfigRollbackError(RuntimeError):
    """A routing snapshot attempted to move a live router backward."""


class _KmsClient(Protocol):
    def sign(self, **kwargs) -> dict: ...

    def verify(self, **kwargs) -> dict: ...


class KmsRoutingConfigAuthenticator:
    """Sign and verify routing snapshots with one exact asymmetric KMS key."""

    def __init__(
        self,
        key_arn: str,
        *,
        region: str,
        client: _KmsClient | None = None,
    ) -> None:
        self.key_arn = validate_routing_config_signing_key_arn(key_arn)
        if routing_config_signing_key_region(self.key_arn) != region:
            raise ValueError(
                "routing configuration signing key region does not match "
                "the runtime region"
            )
        self._region = region
        self._client = client
        self._client_lock = threading.Lock()

    def _get_client(self) -> _KmsClient:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config

                self._client = boto3.client(
                    "kms",
                    region_name=self._region,
                    config=Config(
                        retries={
                            "total_max_attempts": 3,
                            "mode": "adaptive",
                        },
                        connect_timeout=2,
                        read_timeout=5,
                    ),
                )
        return self._client

    @staticmethod
    def _validate_response(response: dict, key_arn: str) -> None:
        if (
            not isinstance(response, dict)
            or response.get("KeyId") != key_arn
            or response.get("SigningAlgorithm")
            != ROUTING_CONFIG_SIGNING_ALGORITHM
        ):
            raise RoutingConfigSignatureError(
                "KMS returned unexpected routing signature metadata"
            )

    async def sign(
        self,
        snapshot: RoutingConfigSnapshot,
    ) -> RoutingConfigSnapshot:
        """Return the snapshot with an exact-key KMS signature attached."""
        if snapshot.is_signed:
            raise RoutingConfigSignatureError(
                "routing configuration snapshot is already signed"
            )

        def _sign() -> dict:
            return self._get_client().sign(
                KeyId=self.key_arn,
                Message=snapshot.signing_digest,
                MessageType="DIGEST",
                SigningAlgorithm=ROUTING_CONFIG_SIGNING_ALGORITHM,
            )

        try:
            response = await asyncio.to_thread(_sign)
        except RoutingConfigSignatureError:
            raise
        except Exception as exc:
            raise RoutingConfigSignatureError(
                "routing configuration signing is unavailable"
            ) from exc
        self._validate_response(response, self.key_arn)
        signature = response.get("Signature")
        if not isinstance(signature, bytes) or not signature:
            raise RoutingConfigSignatureError(
                "KMS returned an invalid routing signature"
            )
        return snapshot.with_signature(
            signing_key_arn=self.key_arn,
            signature=signature,
        )

    async def verify(self, snapshot: RoutingConfigSnapshot) -> None:
        """Reject an unsigned, wrong-key, or cryptographically invalid snapshot."""
        if not snapshot.is_signed:
            raise RoutingConfigSignatureError(
                "routing configuration snapshot is unsigned"
            )
        if snapshot.signing_key_arn != self.key_arn:
            raise RoutingConfigSignatureError(
                "routing configuration signature uses an unexpected key"
            )
        if (
            snapshot.signing_algorithm
            != ROUTING_CONFIG_SIGNING_ALGORITHM
        ):
            raise RoutingConfigSignatureError(
                "routing configuration signature algorithm is unsupported"
            )

        def _verify() -> dict:
            return self._get_client().verify(
                KeyId=self.key_arn,
                Message=snapshot.signing_digest,
                MessageType="DIGEST",
                Signature=snapshot.signature,
                SigningAlgorithm=ROUTING_CONFIG_SIGNING_ALGORITHM,
            )

        try:
            response = await asyncio.to_thread(_verify)
        except RoutingConfigSignatureError:
            raise
        except Exception as exc:
            raise RoutingConfigSignatureError(
                "routing configuration verification is unavailable"
            ) from exc
        self._validate_response(response, self.key_arn)
        if response.get("SignatureValid") is not True:
            raise RoutingConfigSignatureError(
                "routing configuration signature is invalid"
            )
