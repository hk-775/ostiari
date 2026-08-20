"""Fail-closed broker for bounded AgentCore production transition mutations.

The invocation contains identity and immutable S3 version IDs only. All
mutation targets and the CloudFormation execution role come from Lambda
environment variables, while every desired value comes from KMS-signed
transition evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
from typing import Any, Mapping


EVIDENCE_BUCKET_ENV = "AXON_DEPLOYMENT_EVIDENCE_BUCKET"
EVIDENCE_PREFIX_ENV = "AXON_DEPLOYMENT_EVIDENCE_PREFIX"
SIGNING_KEY_ARN_ENV = "AXON_AGENTCORE_TRANSITION_SIGNING_KEY_ARN"
AGENTCORE_STACK_NAME_ENV = "AXON_AGENTCORE_STACK_NAME"
CONTROL_PLANE_STACK_NAME_ENV = "AXON_CONTROL_PLANE_STACK_NAME"
EXECUTION_ROLE_ARN_ENV = "AXON_CLOUDFORMATION_EXECUTION_ROLE_ARN"
AWS_REGION_ENV = "AWS_REGION"

KMS_BUNDLE_SCHEMA = "https://axonllm.dev/schemas/kms-evidence-signature/v1"
KMS_SIGNING_ALGORITHM = "ECDSA_SHA_256"
KMS_MESSAGE_TYPE = "DIGEST"
RECOVERY_BINDING_SCHEMA = "axonllm.agentcore-deployment-transition-recovery/v1"
DEPLOYMENT_EVIDENCE_SCHEMA = "https://axonllm.dev/schemas/agentcore-deployment-evidence/v5"
DEPLOYMENT_COMMIT_SCHEMA = "axonllm.agentcore-deployment-evidence-commit/v1"

_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_SIGNATURE_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 64

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RUN_ID = re.compile(r"^[1-9][0-9]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSITION_ID = re.compile(r"^[0-9a-f]{64}$")
_CHANGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")
_CANDIDATE_ENDPOINT = re.compile(r"^candidate_[0-9a-f]{32}$")
_BUCKET = re.compile(
    r"^(?![0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$)"
    r"[a-z0-9](?:[a-z0-9.-]{1,61})[a-z0-9]$"
)
_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_STACK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[1-9][0-9]*$")
_ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):iam::"
    r"(?P<account>[0-9]{12}):role/[A-Za-z0-9+=,.@_/-]{1,512}$"
)
_KMS_KEY_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-[a-z0-9-]+)?):kms:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"key/[A-Za-z0-9-]{1,256}$"
)
_IMAGE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)

_BASE_EVENT_FIELDS = frozenset(
    {
        "repository",
        "runId",
        "runAttempt",
        "intentVersionId",
        "intentSignatureVersionId",
        "recoverySetupVersionId",
        "recoverySetupSignatureVersionId",
        "recoveryBindingVersionId",
        "recoveryBindingSignatureVersionId",
    }
)
_COMMIT_EVENT_FIELDS = frozenset(
    {
        "deploymentEvidenceVersionId",
        "deploymentEvidenceSignatureVersionId",
        "deploymentCommitVersionId",
        "deploymentCommitSignatureVersionId",
    }
)

_INTENT_FIELDS = frozenset(
    {
        "candidateEndpointName",
        "candidateRuntimeVersion",
        "controlPlane",
        "enabledProviders",
        "previousProductionRuntimeVersion",
        "productionEndpointArn",
        "productionRuntimeVersion",
        "providerSecretVersion",
        "region",
        "runtimeArn",
        "schemaVersion",
        "sharedRuntimeConfiguration",
        "transition",
    }
)
_TRANSITION_FIELDS = frozenset(
    {
        "changeId",
        "deploymentCommit",
        "repository",
        "rollbackNotBefore",
        "runAttempt",
        "runId",
        "transitionId",
    }
)
_SHARED_RUNTIME_FIELDS = frozenset(
    {
        "AlarmNotificationEmail",
        "ApprovedHttpsPrefixListId",
        "AthenaConfigurationFingerprint",
        "BedrockInvokeResourceArns",
    }
)
_CONTROL_INTENT_FIELDS = frozenset(
    {
        "previousParameters",
        "previousStackId",
        "stackExisted",
        "targetImage",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "schema",
        "deployment",
        "release",
        "images",
        "configuration",
        "stacks",
        "providerSecret",
        "recovery",
        "certification",
        "productionCertification",
        "productionValidation",
        "launchRehearsalSource",
        "launchRehearsal",
        "externalOidcCertificationSource",
        "externalOidcCertification",
        "qualificationTeardownSource",
        "qualificationTeardown",
    }
)
_DEPLOYMENT_FIELDS = frozenset(
    {
        "operation",
        "changeId",
        "environment",
        "repository",
        "commit",
        "workflowRef",
        "workflowCommit",
        "parentWorkflowRef",
        "parentWorkflowCommit",
        "runId",
        "runAttempt",
        "actor",
        "actorId",
        "triggeringActor",
        "generatedAt",
        "awsAccountId",
        "awsRegion",
    }
)

_IN_PROGRESS_STACK_STATUSES = frozenset(
    {
        "CREATE_IN_PROGRESS",
        "DELETE_IN_PROGRESS",
        "IMPORT_IN_PROGRESS",
        "IMPORT_ROLLBACK_IN_PROGRESS",
        "REVIEW_IN_PROGRESS",
        "ROLLBACK_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_IN_PROGRESS",
    }
)
_HEALTHY_STACK_STATUSES = frozenset(
    {
        "CREATE_COMPLETE",
        "IMPORT_COMPLETE",
        "UPDATE_COMPLETE",
    }
)
_DELETABLE_FIRST_LAUNCH_STATUSES = _HEALTHY_STACK_STATUSES | frozenset(
    {
        "CREATE_FAILED",
        "DELETE_FAILED",
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
    }
)


class MutationBrokerError(RuntimeError):
    """Raised when a production transition cannot be proven safe."""


@dataclass(frozen=True)
class BrokerClients:
    """Injectable AWS clients used by the broker."""

    s3: Any
    kms: Any
    cloudformation: Any
    elbv2: Any


@dataclass(frozen=True)
class BrokerConfig:
    """Immutable deployment boundary derived exclusively from environment."""

    evidence_bucket: str
    evidence_prefix: str
    signing_key_arn: str
    agentcore_stack_name: str
    control_plane_stack_name: str
    execution_role_arn: str
    region: str
    partition: str
    account_id: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> BrokerConfig:
        values = os.environ if environ is None else environ
        evidence_bucket = _required_env(values, EVIDENCE_BUCKET_ENV)
        evidence_prefix = _required_env(values, EVIDENCE_PREFIX_ENV)
        signing_key_arn = _required_env(values, SIGNING_KEY_ARN_ENV)
        agentcore_stack_name = _required_env(
            values,
            AGENTCORE_STACK_NAME_ENV,
        )
        control_plane_stack_name = _required_env(
            values,
            CONTROL_PLANE_STACK_NAME_ENV,
        )
        execution_role_arn = _required_env(
            values,
            EXECUTION_ROLE_ARN_ENV,
        )
        region = _required_env(values, AWS_REGION_ENV)

        role_match = _ROLE_ARN.fullmatch(execution_role_arn)
        key_match = _KMS_KEY_ARN.fullmatch(signing_key_arn)
        if (
            _BUCKET.fullmatch(evidence_bucket) is None
            or ".." in evidence_bucket
            or ".-" in evidence_bucket
            or "-." in evidence_bucket
        ):
            raise MutationBrokerError("evidence bucket environment value is malformed")
        if (
            _PREFIX.fullmatch(evidence_prefix) is None
            or evidence_prefix.endswith("/")
            or "//" in evidence_prefix
            or any(segment in {"", ".", ".."} for segment in evidence_prefix.split("/"))
        ):
            raise MutationBrokerError("evidence prefix environment value is malformed")
        if (
            _STACK_NAME.fullmatch(agentcore_stack_name) is None
            or _STACK_NAME.fullmatch(control_plane_stack_name) is None
            or agentcore_stack_name == control_plane_stack_name
        ):
            raise MutationBrokerError("production stack environment values are malformed")
        if _REGION.fullmatch(region) is None:
            raise MutationBrokerError("AWS region is malformed")
        if role_match is None or key_match is None:
            raise MutationBrokerError("production role or signing key ARN is malformed")
        if (
            role_match.group("partition") != key_match.group("partition")
            or role_match.group("account") != key_match.group("account")
            or key_match.group("region") != region
        ):
            raise MutationBrokerError("production role, signing key, and region are not co-located")
        return cls(
            evidence_bucket=evidence_bucket,
            evidence_prefix=evidence_prefix,
            signing_key_arn=signing_key_arn,
            agentcore_stack_name=agentcore_stack_name,
            control_plane_stack_name=control_plane_stack_name,
            execution_role_arn=execution_role_arn,
            region=region,
            partition=role_match.group("partition"),
            account_id=role_match.group("account"),
        )


@dataclass(frozen=True)
class Invocation:
    repository: str
    run_id: str
    run_attempt: str
    versions: Mapping[str, str]
    has_commit: bool


@dataclass(frozen=True)
class TransitionEvidence:
    intent: dict[str, Any]
    setup: dict[str, Any]
    binding: dict[str, Any]
    intent_raw: bytes
    setup_raw: bytes
    runtime_image: str
    control_image: str
    rollback_not_before: datetime
    commit_valid: bool

    @property
    def transition(self) -> dict[str, str]:
        value = self.intent["transition"]
        if not isinstance(value, dict):  # validated on construction
            raise AssertionError("validated transition is not an object")
        return value


@dataclass
class _MutationBudget:
    operation: str | None = None

    def consume(self, operation: str) -> None:
        if self.operation is not None:
            raise MutationBrokerError("one invocation attempted more than one mutation")
        self.operation = operation


def _required_env(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise MutationBrokerError(f"required broker environment variable is invalid: {name}")
    return value


def _normalize_now(value: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise MutationBrokerError("broker clock must be timezone-aware")
    return current.astimezone(timezone.utc)


def _parse_invocation(event: Any) -> Invocation:
    if type(event) is not dict:
        raise MutationBrokerError("broker event must be a JSON object")
    fields = frozenset(event)
    has_commit = bool(fields & _COMMIT_EVENT_FIELDS)
    expected = _BASE_EVENT_FIELDS | _COMMIT_EVENT_FIELDS if has_commit else _BASE_EVENT_FIELDS
    if fields != expected:
        raise MutationBrokerError("broker event fields do not match the strict schema")
    repository = event.get("repository")
    run_id = event.get("runId")
    run_attempt = event.get("runAttempt")
    if (
        not isinstance(repository, str)
        or _REPOSITORY.fullmatch(repository) is None
        or not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or not isinstance(run_attempt, str)
        or _RUN_ID.fullmatch(run_attempt) is None
    ):
        raise MutationBrokerError("broker event identity is malformed")
    versions: dict[str, str] = {}
    for name in sorted(expected - {"repository", "runId", "runAttempt"}):
        value = event.get(name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 1024
            or value != value.strip()
            or any(ord(character) < 0x21 for character in value)
        ):
            raise MutationBrokerError(f"broker event has an invalid S3 VersionId: {name}")
        versions[name] = value
    return Invocation(
        repository=repository,
        run_id=run_id,
        run_attempt=run_attempt,
        versions=versions,
        has_commit=has_commit,
    )


def _unique_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise MutationBrokerError(f"signed JSON contains duplicate field: {name}")
        value[name] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise MutationBrokerError(f"signed JSON constant is invalid: {value}")


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > _MAX_JSON_DEPTH:
        return depth
    if isinstance(value, dict):
        return max(
            (_json_depth(item, depth + 1) for item in value.values()),
            default=depth,
        )
    if isinstance(value, list):
        return max(
            (_json_depth(item, depth + 1) for item in value),
            default=depth,
        )
    return depth


def _strict_json(raw: bytes, location: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        MutationBrokerError,
        RecursionError,
    ) as exc:
        raise MutationBrokerError(f"{location} is not strict UTF-8 JSON") from exc
    if type(value) is not dict or _json_depth(value) > _MAX_JSON_DEPTH:
        raise MutationBrokerError(f"{location} must be a bounded JSON object")
    return value


def _aws_error(exc: Exception) -> tuple[str | None, str | None, bool]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None, None, False
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, dict) else None
    message = error.get("Message") if isinstance(error, dict) else None
    metadata = response.get("ResponseMetadata")
    headers = metadata.get("HTTPHeaders") if isinstance(metadata, dict) else None
    delete_marker = isinstance(headers, dict) and str(headers.get("x-amz-delete-marker", "")).lower() == "true"
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
        delete_marker,
    )


def _retention_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_base64(value: Any, location: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 65536:
        raise MutationBrokerError(f"{location} is not canonical base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MutationBrokerError(f"{location} is not canonical base64") from exc
    if not decoded or base64.b64encode(decoded).decode("ascii") != value:
        raise MutationBrokerError(f"{location} is not canonical base64")
    return decoded


def _fetch_exact_version(
    client: Any,
    *,
    config: BrokerConfig,
    key: str,
    version_id: str,
    maximum: int,
    now: datetime,
) -> bytes:
    try:
        response = client.get_object(
            Bucket=config.evidence_bucket,
            Key=key,
            VersionId=version_id,
            ChecksumMode="ENABLED",
        )
    except Exception as exc:
        _code, _message, delete_marker = _aws_error(exc)
        if delete_marker:
            raise MutationBrokerError(f"signed transition object is a delete marker: {key}") from exc
        raise MutationBrokerError(f"cannot fetch exact signed transition object: {key}") from exc
    if not isinstance(response, dict):
        raise MutationBrokerError(f"S3 returned a malformed object response: {key}")
    headers = response.get("ResponseMetadata", {})
    headers = headers.get("HTTPHeaders", {}) if isinstance(headers, dict) else {}
    header_version = headers.get("x-amz-version-id") if isinstance(headers, dict) else None
    response_key = response.get("Key")
    retain_until = _retention_time(response.get("ObjectLockRetainUntilDate"))
    if response.get("DeleteMarker") is True or (
        isinstance(headers, dict) and str(headers.get("x-amz-delete-marker", "")).lower() == "true"
    ):
        raise MutationBrokerError(f"signed transition object is a delete marker: {key}")
    if (
        response.get("VersionId") != version_id
        or (header_version is not None and header_version != version_id)
        or (response_key is not None and response_key != key)
        or response.get("ObjectLockMode") != "COMPLIANCE"
        or retain_until is None
        or retain_until <= now
    ):
        raise MutationBrokerError(f"S3 object version or immutable retention is invalid: {key}")
    length = response.get("ContentLength")
    checksum = response.get("ChecksumSHA256")
    body = response.get("Body")
    if (
        not isinstance(length, int)
        or isinstance(length, bool)
        or length < 0
        or length > maximum
        or body is None
        or not callable(getattr(body, "read", None))
    ):
        raise MutationBrokerError(f"S3 object body metadata is invalid: {key}")
    expected_checksum = _canonical_base64(
        checksum,
        f"S3 checksum for {key}",
    )
    try:
        raw = body.read(maximum + 1)
    except Exception as exc:
        raise MutationBrokerError(f"cannot read signed transition object: {key}") from exc
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if (
        not isinstance(raw, bytes)
        or len(raw) != length
        or len(raw) > maximum
        or not secrets.compare_digest(
            hashlib.sha256(raw).digest(),
            expected_checksum,
        )
    ):
        raise MutationBrokerError(f"S3 object SHA-256 or length is invalid: {key}")
    return raw


def _verify_signature(
    kms_client: Any,
    *,
    artifact: bytes,
    bundle_raw: bytes,
    config: BrokerConfig,
    location: str,
) -> None:
    bundle = _strict_json(bundle_raw, f"{location} signature bundle")
    if set(bundle) != {"schema", "artifact", "signature"}:
        raise MutationBrokerError(f"{location} signature bundle fields are invalid")
    artifact_record = bundle.get("artifact")
    signature_record = bundle.get("signature")
    if (
        bundle.get("schema") != KMS_BUNDLE_SCHEMA
        or not isinstance(artifact_record, dict)
        or set(artifact_record) != {"sha256", "size"}
        or not isinstance(signature_record, dict)
        or set(signature_record)
        != {
            "keyArn",
            "messageType",
            "signingAlgorithm",
            "value",
        }
    ):
        raise MutationBrokerError(f"{location} signature bundle schema is invalid")
    digest_hex = artifact_record.get("sha256")
    size = artifact_record.get("size")
    if (
        not isinstance(digest_hex, str)
        or _SHA256.fullmatch(digest_hex) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or signature_record.get("keyArn") != config.signing_key_arn
        or signature_record.get("messageType") != KMS_MESSAGE_TYPE
        or signature_record.get("signingAlgorithm") != KMS_SIGNING_ALGORITHM
    ):
        raise MutationBrokerError(f"{location} signature bundle binding is invalid")
    digest = hashlib.sha256(artifact).digest()
    if size != len(artifact) or not secrets.compare_digest(digest.hex(), digest_hex):
        raise MutationBrokerError(f"{location} SHA-256 does not match its signature bundle")
    signature = _canonical_base64(
        signature_record.get("value"),
        f"{location} KMS signature",
    )
    try:
        response = kms_client.verify(
            KeyId=config.signing_key_arn,
            Message=digest,
            MessageType=KMS_MESSAGE_TYPE,
            Signature=signature,
            SigningAlgorithm=KMS_SIGNING_ALGORITHM,
        )
    except Exception as exc:
        raise MutationBrokerError(f"KMS could not verify {location}") from exc
    if (
        not isinstance(response, dict)
        or response.get("KeyId") != config.signing_key_arn
        or response.get("SigningAlgorithm") != KMS_SIGNING_ALGORITHM
        or response.get("SignatureValid") is not True
    ):
        raise MutationBrokerError(f"KMS rejected or ambiguously verified {location}")


def _pair(
    clients: BrokerClients,
    *,
    config: BrokerConfig,
    base: str,
    artifact_name: str,
    signature_name: str,
    artifact_version: str,
    signature_version: str,
    now: datetime,
) -> tuple[bytes, bytes]:
    artifact = _fetch_exact_version(
        clients.s3,
        config=config,
        key=f"{base}/{artifact_name}",
        version_id=artifact_version,
        maximum=_MAX_ARTIFACT_BYTES,
        now=now,
    )
    signature = _fetch_exact_version(
        clients.s3,
        config=config,
        key=f"{base}/{signature_name}",
        version_id=signature_version,
        maximum=_MAX_SIGNATURE_BYTES,
        now=now,
    )
    _verify_signature(
        clients.kms,
        artifact=artifact,
        bundle_raw=signature,
        config=config,
        location=artifact_name,
    )
    return artifact, signature


def _timestamp(value: Any, location: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MutationBrokerError(f"{location} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MutationBrokerError(f"{location} is not a timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MutationBrokerError(f"{location} is not a timestamp")
    return parsed.astimezone(timezone.utc)


def _positive_version(value: Any) -> bool:
    return isinstance(value, str) and value.isdigit() and not value.startswith("0") and len(value) <= 32


def _string_map(
    value: Any,
    location: str,
    *,
    allow_empty: bool,
) -> dict[str, str]:
    if not isinstance(value, dict) or (not allow_empty and not value) or len(value) > 256:
        raise MutationBrokerError(f"{location} is malformed")
    result: dict[str, str] = {}
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 255
            or not isinstance(item, str)
            or len(item) > 8192
            or "\x00" in name
            or "\x00" in item
        ):
            raise MutationBrokerError(f"{location} is malformed")
        result[name] = item
    return result


def _validate_image(
    value: Any,
    *,
    config: BrokerConfig,
    location: str,
) -> str:
    if not isinstance(value, str):
        raise MutationBrokerError(f"{location} is malformed")
    match = _IMAGE.fullmatch(value)
    if match is None or match.group("account") != config.account_id or match.group("region") != config.region:
        raise MutationBrokerError(f"{location} is not an immutable in-account image")
    return value


def _expected_stack_id(
    value: Any,
    *,
    config: BrokerConfig,
    stack_name: str,
) -> bool:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return False
    prefix = f"arn:{config.partition}:cloudformation:{config.region}:{config.account_id}:stack/{stack_name}/"
    return value.startswith(prefix) and len(value) > len(prefix)


def _validate_intent(
    value: dict[str, Any],
    *,
    invocation: Invocation,
    config: BrokerConfig,
) -> tuple[datetime, str]:
    if set(value) != _INTENT_FIELDS or value.get("schemaVersion") != 3:
        raise MutationBrokerError("promotion intent fields or schema are invalid")
    transition = value.get("transition")
    if (
        not isinstance(transition, dict)
        or set(transition) != _TRANSITION_FIELDS
        or any(not isinstance(transition.get(name), str) for name in _TRANSITION_FIELDS)
        or _CHANGE_ID.fullmatch(transition["changeId"]) is None
        or _COMMIT.fullmatch(transition["deploymentCommit"]) is None
        or transition["repository"] != invocation.repository
        or transition["runId"] != invocation.run_id
        or transition["runAttempt"] != invocation.run_attempt
        or _TRANSITION_ID.fullmatch(transition["transitionId"]) is None
    ):
        raise MutationBrokerError("promotion intent transition identity is invalid")
    rollback_not_before = _timestamp(
        transition["rollbackNotBefore"],
        "promotion intent rollbackNotBefore",
    )
    candidate = value.get("candidateRuntimeVersion")
    previous = value.get("previousProductionRuntimeVersion")
    endpoint_name = value.get("candidateEndpointName")
    runtime_arn = value.get("runtimeArn")
    if (
        not _positive_version(candidate)
        or (previous is not None and (not _positive_version(previous) or previous == candidate))
        or not isinstance(endpoint_name, str)
        or _CANDIDATE_ENDPOINT.fullmatch(endpoint_name) is None
        or value.get("productionRuntimeVersion") != candidate
        or not isinstance(runtime_arn, str)
        or not runtime_arn.startswith(f"arn:{config.partition}:bedrock-agentcore:{config.region}:{config.account_id}:")
        or any(character.isspace() for character in runtime_arn)
        or value.get("productionEndpointArn") != f"{runtime_arn}/runtime-endpoint/production"
        or not isinstance(value.get("providerSecretVersion"), str)
        or not value["providerSecretVersion"]
        or any(character.isspace() for character in value["providerSecretVersion"])
        or value.get("region") != config.region
    ):
        raise MutationBrokerError("promotion intent runtime binding is invalid")
    shared = value.get("sharedRuntimeConfiguration")
    if (
        not isinstance(shared, dict)
        or set(shared) != _SHARED_RUNTIME_FIELDS
        or any(
            not isinstance(item, str) or item != item.strip() or any(character.isspace() for character in item)
            for item in shared.values()
        )
    ):
        raise MutationBrokerError("promotion intent shared runtime binding is invalid")
    enabled = value.get("enabledProviders")
    if (
        not isinstance(enabled, str)
        or not enabled
        or any(character.isspace() for character in enabled)
        or any(not item for item in enabled.split(","))
    ):
        raise MutationBrokerError("promotion intent provider binding is invalid")
    control = value.get("controlPlane")
    if (
        not isinstance(control, dict)
        or set(control) != _CONTROL_INTENT_FIELDS
        or not isinstance(control.get("stackExisted"), bool)
    ):
        raise MutationBrokerError("promotion intent control-plane binding is invalid")
    _validate_image(
        control.get("targetImage"),
        config=config,
        location="promotion intent control-plane image",
    )
    if control["stackExisted"]:
        _string_map(
            control.get("previousParameters"),
            "promotion intent previous control-plane parameters",
            allow_empty=False,
        )
        if not _expected_stack_id(
            control.get("previousStackId"),
            config=config,
            stack_name=config.control_plane_stack_name,
        ):
            raise MutationBrokerError("promotion intent previous control-plane stack is invalid")
    elif control.get("previousParameters") is not None or control.get("previousStackId") is not None:
        raise MutationBrokerError("first-launch intent contains previous control-plane state")
    return rollback_not_before, transition["transitionId"]


def _validate_setup(
    value: dict[str, Any],
    *,
    intent: dict[str, Any],
    config: BrokerConfig,
) -> tuple[str, str]:
    runtime = value.get("runtime")
    control = value.get("control_plane")
    if (
        value.get("schema_version") != 2
        or value.get("identity_mode") != "managed-cognito"
        or value.get("aws_region") != config.region
        or not isinstance(runtime, dict)
        or not isinstance(control, dict)
    ):
        raise MutationBrokerError("signed recovery setup is not a managed production setup")
    runtime_image = _validate_image(
        runtime.get("verified_image_uri"),
        config=config,
        location="recovery setup runtime image",
    )
    control_image = _validate_image(
        control.get("verified_image_uri"),
        config=config,
        location="recovery setup control-plane image",
    )
    providers = runtime.get("enabled_providers")
    if (
        not isinstance(providers, list)
        or not providers
        or any(
            not isinstance(provider, str)
            or not provider
            or "," in provider
            or any(character.isspace() for character in provider)
            for provider in providers
        )
        or len(set(providers)) != len(providers)
        or ",".join(providers) != intent["enabledProviders"]
        or control_image != intent["controlPlane"]["targetImage"]
    ):
        raise MutationBrokerError("signed recovery setup images or providers do not match intent")
    return runtime_image, control_image


def _validate_recovery_binding(
    value: dict[str, Any],
    *,
    invocation: Invocation,
    intent_raw: bytes,
    setup_raw: bytes,
) -> None:
    expected = {
        "schema",
        "intentSha256",
        "setupConfigSha256",
        "repository",
        "runId",
        "runAttempt",
        "recordedAt",
    }
    if (
        set(value) != expected
        or value.get("schema") != RECOVERY_BINDING_SCHEMA
        or value.get("intentSha256") != hashlib.sha256(intent_raw).hexdigest()
        or value.get("setupConfigSha256") != hashlib.sha256(setup_raw).hexdigest()
        or value.get("repository") != invocation.repository
        or value.get("runId") != invocation.run_id
        or value.get("runAttempt") != invocation.run_attempt
    ):
        raise MutationBrokerError("recovery binding does not match exact signed artifacts")
    _timestamp(value.get("recordedAt"), "recovery binding recordedAt")


def _exact_object(
    value: Any,
    fields: set[str] | frozenset[str],
    location: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise MutationBrokerError(f"{location} fields are invalid")
    return value


def _validate_evidence_runtime(
    outputs: dict[str, Any],
    *,
    evidence: TransitionEvidence,
    config: BrokerConfig,
) -> None:
    intent = evidence.intent
    candidate = intent["candidateRuntimeVersion"]
    endpoint = intent["candidateEndpointName"]
    runtime_arn = intent["runtimeArn"]
    expected = {
        "RuntimeImageUri": evidence.runtime_image,
        "RuntimeArn": runtime_arn,
        "RuntimeVersion": candidate,
        "CandidateRuntimeVersion": candidate,
        "CandidateRuntimeEndpointName": endpoint,
        "CandidateRuntimeEndpointArn": (f"{runtime_arn}/runtime-endpoint/{endpoint}"),
        "RuntimeEndpointName": "production",
        "RuntimeEndpointArn": intent["productionEndpointArn"],
        "ProductionRuntimeVersion": candidate,
        "ProviderSecretVersion": intent["providerSecretVersion"],
        "EnabledProviders": intent["enabledProviders"],
        "RecoveryCutoverMode": "normal",
    }
    expected.update(intent["sharedRuntimeConfiguration"])
    if any(outputs.get(name) != value for name, value in expected.items()):
        raise MutationBrokerError("deployment evidence runtime outputs do not match intent")
    stack_name = outputs.get("AgentCoreStackName")
    if stack_name is not None and stack_name != config.agentcore_stack_name:
        raise MutationBrokerError("deployment evidence runtime stack name is invalid")


def _validate_evidence_control(
    outputs: dict[str, Any],
    *,
    evidence: TransitionEvidence,
    config: BrokerConfig,
) -> None:
    transition_id = evidence.transition["transitionId"]
    expected = {
        "ControlPlaneImageUri": evidence.control_image,
        "AgentCoreStackName": config.agentcore_stack_name,
        "DeploymentTransitionId": transition_id,
    }
    if any(outputs.get(name) != value for name, value in expected.items()):
        raise MutationBrokerError("deployment evidence control-plane outputs do not match intent")


def _validate_deployment_commit(
    evidence_raw: bytes,
    evidence_signature_raw: bytes,
    commit_raw: bytes,
    *,
    transition_evidence: TransitionEvidence,
    invocation: Invocation,
    config: BrokerConfig,
) -> None:
    evidence = _strict_json(
        evidence_raw,
        "deployment evidence",
    )
    commit = _strict_json(
        commit_raw,
        "deployment commit",
    )
    if set(evidence) != _EVIDENCE_FIELDS or evidence.get("schema") != DEPLOYMENT_EVIDENCE_SCHEMA:
        raise MutationBrokerError("deployment evidence schema is invalid")
    deployment = _exact_object(
        evidence.get("deployment"),
        _DEPLOYMENT_FIELDS,
        "deployment identity",
    )
    transition = transition_evidence.transition
    expected_workflow = f"{invocation.repository}/.github/workflows/deploy-agentcore-production.yml@refs/heads/main"
    expected_parent = f"{invocation.repository}/.github/workflows/launch-agentcore-production.yml@refs/heads/main"
    if (
        deployment.get("repository") != invocation.repository
        or deployment.get("runId") != invocation.run_id
        or deployment.get("runAttempt") != invocation.run_attempt
        or deployment.get("commit") != transition["deploymentCommit"]
        or deployment.get("changeId") != transition["changeId"]
        or deployment.get("environment") != "production"
        or deployment.get("operation") not in {"deploy", "rollback"}
        or deployment.get("workflowRef") != expected_workflow
        or deployment.get("parentWorkflowRef") != expected_parent
        or deployment.get("workflowCommit") != transition["deploymentCommit"]
        or deployment.get("parentWorkflowCommit") != transition["deploymentCommit"]
        or deployment.get("awsAccountId") != config.account_id
        or deployment.get("awsRegion") != config.region
    ):
        raise MutationBrokerError("deployment evidence identity does not match intent")
    _timestamp(
        deployment.get("generatedAt"),
        "deployment evidence generatedAt",
    )
    release = evidence.get("release")
    if not isinstance(release, dict) or release.get("commit") != transition["deploymentCommit"]:
        raise MutationBrokerError("deployment evidence release does not match intent")
    images = _exact_object(
        evidence.get("images"),
        {"agentcore", "controlPlane"},
        "deployment evidence images",
    )
    expected_images = {
        "agentcore": transition_evidence.runtime_image,
        "controlPlane": transition_evidence.control_image,
    }
    for name, reference in expected_images.items():
        image = _exact_object(
            images.get(name),
            {"reference", "digest"},
            f"deployment evidence {name} image",
        )
        if image.get("reference") != reference or image.get("digest") != reference.rsplit("@", maxsplit=1)[1]:
            raise MutationBrokerError("deployment evidence image does not match signed setup")
    configuration = evidence.get("configuration")
    if (
        not isinstance(configuration, dict)
        or configuration.get("setupSha256") != hashlib.sha256(transition_evidence.setup_raw).hexdigest()
    ):
        raise MutationBrokerError("deployment evidence is not bound to recovery setup")
    stacks = _exact_object(
        evidence.get("stacks"),
        {"identity", "runtime", "controlPlane"},
        "deployment evidence stacks",
    )
    runtime_outputs = stacks.get("runtime")
    control_outputs = stacks.get("controlPlane")
    if not isinstance(runtime_outputs, dict) or not isinstance(
        control_outputs,
        dict,
    ):
        raise MutationBrokerError("deployment evidence stack outputs are malformed")
    _validate_evidence_runtime(
        runtime_outputs,
        evidence=transition_evidence,
        config=config,
    )
    _validate_evidence_control(
        control_outputs,
        evidence=transition_evidence,
        config=config,
    )

    commit_deployment = {
        "repository": invocation.repository,
        "commit": transition["deploymentCommit"],
        "runId": invocation.run_id,
        "runAttempt": invocation.run_attempt,
    }
    expected_commit = {
        "schema": DEPLOYMENT_COMMIT_SCHEMA,
        "deployment": commit_deployment,
        "release": {"commit": transition["deploymentCommit"]},
        "images": {
            "agentcore": transition_evidence.runtime_image,
            "controlPlane": transition_evidence.control_image,
        },
        "artifacts": {
            "evidence": {
                "name": "agentcore-deployment.json",
                "sha256": hashlib.sha256(evidence_raw).hexdigest(),
            },
            "signature": {
                "name": ("agentcore-deployment-kms-signature.json"),
                "sha256": hashlib.sha256(evidence_signature_raw).hexdigest(),
            },
        },
    }
    if commit != expected_commit:
        raise MutationBrokerError("deployment commit does not bind exact evidence and identity")


def _commit_signal_key(base: str) -> str:
    return f"{base}/agentcore-deployment-commit-kms-signature.json"


def _assert_no_commit_signal(
    s3_client: Any,
    *,
    config: BrokerConfig,
    base: str,
) -> None:
    key = _commit_signal_key(base)
    try:
        response = s3_client.list_object_versions(
            Bucket=config.evidence_bucket,
            Prefix=key,
            MaxKeys=2,
        )
    except Exception as exc:
        raise MutationBrokerError("cannot prove deployment commit signal is absent") from exc
    if not isinstance(response, dict) or response.get("IsTruncated") is True:
        raise MutationBrokerError("deployment commit version state is ambiguous")
    versions = response.get("Versions", [])
    markers = response.get("DeleteMarkers", [])
    if not isinstance(versions, list) or not isinstance(markers, list):
        raise MutationBrokerError("deployment commit version state is malformed")
    for marker in markers:
        if isinstance(marker, dict) and marker.get("Key") == key:
            raise MutationBrokerError("deployment commit signal has a delete marker")
    for version in versions:
        if isinstance(version, dict) and version.get("Key") == key:
            raise MutationBrokerError("deployment commit exists but exact versions were omitted")


def _load_transition_evidence(
    invocation: Invocation,
    *,
    clients: BrokerClients,
    config: BrokerConfig,
    now: datetime,
) -> TransitionEvidence:
    base = f"{config.evidence_prefix}/{invocation.repository}/{invocation.run_id}/{invocation.run_attempt}"
    intent_raw, _intent_signature = _pair(
        clients,
        config=config,
        base=base,
        artifact_name="promotion.json",
        signature_name="promotion-kms-signature.json",
        artifact_version=invocation.versions["intentVersionId"],
        signature_version=invocation.versions["intentSignatureVersionId"],
        now=now,
    )
    setup_raw, _setup_signature = _pair(
        clients,
        config=config,
        base=base,
        artifact_name="transition-recovery-setup.json",
        signature_name=("transition-recovery-setup-kms-signature.json"),
        artifact_version=invocation.versions["recoverySetupVersionId"],
        signature_version=invocation.versions["recoverySetupSignatureVersionId"],
        now=now,
    )
    binding_raw, _binding_signature = _pair(
        clients,
        config=config,
        base=base,
        artifact_name="transition-recovery-binding.json",
        signature_name=("transition-recovery-binding-kms-signature.json"),
        artifact_version=invocation.versions["recoveryBindingVersionId"],
        signature_version=invocation.versions["recoveryBindingSignatureVersionId"],
        now=now,
    )
    intent = _strict_json(intent_raw, "promotion intent")
    setup = _strict_json(setup_raw, "recovery setup")
    binding = _strict_json(binding_raw, "recovery binding")
    rollback_not_before, _transition_id = _validate_intent(
        intent,
        invocation=invocation,
        config=config,
    )
    runtime_image, control_image = _validate_setup(
        setup,
        intent=intent,
        config=config,
    )
    _validate_recovery_binding(
        binding,
        invocation=invocation,
        intent_raw=intent_raw,
        setup_raw=setup_raw,
    )
    result = TransitionEvidence(
        intent=intent,
        setup=setup,
        binding=binding,
        intent_raw=intent_raw,
        setup_raw=setup_raw,
        runtime_image=runtime_image,
        control_image=control_image,
        rollback_not_before=rollback_not_before,
        commit_valid=False,
    )
    if not invocation.has_commit:
        _assert_no_commit_signal(
            clients.s3,
            config=config,
            base=base,
        )
        return result

    evidence_raw, evidence_signature = _pair(
        clients,
        config=config,
        base=base,
        artifact_name="agentcore-deployment.json",
        signature_name="agentcore-deployment-kms-signature.json",
        artifact_version=invocation.versions["deploymentEvidenceVersionId"],
        signature_version=invocation.versions["deploymentEvidenceSignatureVersionId"],
        now=now,
    )
    commit_raw, _commit_signature = _pair(
        clients,
        config=config,
        base=base,
        artifact_name="agentcore-deployment-commit.json",
        signature_name=("agentcore-deployment-commit-kms-signature.json"),
        artifact_version=invocation.versions["deploymentCommitVersionId"],
        signature_version=invocation.versions["deploymentCommitSignatureVersionId"],
        now=now,
    )
    _validate_deployment_commit(
        evidence_raw,
        evidence_signature,
        commit_raw,
        transition_evidence=result,
        invocation=invocation,
        config=config,
    )
    return TransitionEvidence(
        intent=result.intent,
        setup=result.setup,
        binding=result.binding,
        intent_raw=result.intent_raw,
        setup_raw=result.setup_raw,
        runtime_image=result.runtime_image,
        control_image=result.control_image,
        rollback_not_before=result.rollback_not_before,
        commit_valid=True,
    )


def _describe_stack(
    client: Any,
    *,
    config: BrokerConfig,
    stack_name: str,
) -> dict[str, Any] | None:
    try:
        response = client.describe_stacks(StackName=stack_name)
    except Exception as exc:
        code, message, _delete_marker = _aws_error(exc)
        if code == "ValidationError" and isinstance(message, str) and "does not exist" in message:
            return None
        raise MutationBrokerError(f"cannot inspect fixed production stack: {stack_name}") from exc
    stacks = response.get("Stacks") if isinstance(response, dict) else None
    if not isinstance(stacks, list) or len(stacks) != 1 or not isinstance(stacks[0], dict):
        raise MutationBrokerError(f"CloudFormation returned an ambiguous stack: {stack_name}")
    stack = stacks[0]
    if (
        stack.get("StackName") != stack_name
        or not _expected_stack_id(
            stack.get("StackId"),
            config=config,
            stack_name=stack_name,
        )
        or stack.get("RoleARN") != config.execution_role_arn
    ):
        raise MutationBrokerError(f"CloudFormation stack ownership is invalid: {stack_name}")
    return stack


def _stack_status(stack: Mapping[str, Any], location: str) -> str:
    value = stack.get("StackStatus")
    if not isinstance(value, str):
        raise MutationBrokerError(f"{location} stack status is malformed")
    return value


def _stack_parameters(
    stack: Mapping[str, Any],
    location: str,
) -> dict[str, str]:
    raw = stack.get("Parameters")
    if not isinstance(raw, list):
        raise MutationBrokerError(f"{location} stack parameters are unavailable")
    values: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise MutationBrokerError(f"{location} stack parameters are malformed")
        name = item.get("ParameterKey")
        value = item.get("ParameterValue")
        if not isinstance(name, str) or not name or not isinstance(value, str) or name in values:
            raise MutationBrokerError(f"{location} stack parameters are malformed")
        values[name] = value
    return values


def _stack_outputs(
    stack: Mapping[str, Any],
    location: str,
) -> dict[str, str]:
    raw = stack.get("Outputs", [])
    if not isinstance(raw, list):
        raise MutationBrokerError(f"{location} stack outputs are malformed")
    values: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise MutationBrokerError(f"{location} stack outputs are malformed")
        name = item.get("OutputKey")
        value = item.get("OutputValue")
        if not isinstance(name, str) or not name or not isinstance(value, str) or name in values:
            raise MutationBrokerError(f"{location} stack outputs are malformed")
        values[name] = value
    return values


def _validate_runtime_stack(
    stack: dict[str, Any],
    *,
    evidence: TransitionEvidence,
    config: BrokerConfig,
) -> tuple[dict[str, str], dict[str, str]]:
    status = _stack_status(stack, "AgentCore")
    if status not in _IN_PROGRESS_STACK_STATUSES and status not in _HEALTHY_STACK_STATUSES:
        raise MutationBrokerError("AgentCore stack is not in a permitted state")
    parameters = _stack_parameters(stack, "AgentCore")
    outputs = _stack_outputs(stack, "AgentCore")
    intent = evidence.intent
    required_parameters = {
        "VerifiedImageUri": evidence.runtime_image,
        "CandidateEndpointName": intent["candidateEndpointName"],
        "ProviderSecretVersion": intent["providerSecretVersion"],
        "EnabledProviders": intent["enabledProviders"],
    }
    if any(parameters.get(name) != value for name, value in required_parameters.items()):
        raise MutationBrokerError("AgentCore stack parameters do not match signed intent")
    expected_outputs = {
        "RuntimeImageUri": evidence.runtime_image,
        "RuntimeArn": intent["runtimeArn"],
        "RuntimeVersion": intent["candidateRuntimeVersion"],
        "ProviderSecretVersion": intent["providerSecretVersion"],
        "EnabledProviders": intent["enabledProviders"],
        "RecoveryCutoverMode": "normal",
    }
    expected_outputs.update(intent["sharedRuntimeConfiguration"])
    if any(outputs.get(name) != value for name, value in expected_outputs.items()):
        raise MutationBrokerError("AgentCore stack outputs do not match signed intent")
    candidate_fields = {
        "CandidateRuntimeEndpointArn": (f"{intent['runtimeArn']}/runtime-endpoint/{intent['candidateEndpointName']}"),
        "CandidateRuntimeEndpointName": (intent["candidateEndpointName"]),
        "CandidateRuntimeVersion": (intent["candidateRuntimeVersion"]),
    }
    present = set(candidate_fields) & set(outputs)
    if present and (
        present != set(candidate_fields) or any(outputs.get(name) != value for name, value in candidate_fields.items())
    ):
        raise MutationBrokerError("AgentCore candidate endpoint no longer matches intent")
    production_fields = {
        "RuntimeEndpointArn",
        "RuntimeEndpointName",
        "ProductionRuntimeVersion",
    }
    production_present = production_fields & set(outputs)
    if production_present:
        version = outputs.get("ProductionRuntimeVersion")
        if (
            production_present != production_fields
            or outputs.get("RuntimeEndpointArn") != intent["productionEndpointArn"]
            or outputs.get("RuntimeEndpointName") != "production"
            or version
            not in {
                intent["candidateRuntimeVersion"],
                intent["previousProductionRuntimeVersion"],
            }
        ):
            raise MutationBrokerError("AgentCore production endpoint is outside transition")
    return parameters, outputs


def _validate_control_target(
    stack: dict[str, Any],
    *,
    evidence: TransitionEvidence,
    config: BrokerConfig,
    require_outputs: bool,
) -> dict[str, str]:
    parameters = _stack_parameters(stack, "control-plane")
    transition_id = evidence.transition["transitionId"]
    expected = {
        "AgentCoreStackName": config.agentcore_stack_name,
        "ControlPlaneVerifiedImageUri": evidence.control_image,
        "DeploymentTransitionId": transition_id,
    }
    if any(parameters.get(name) != value for name, value in expected.items()):
        raise MutationBrokerError("control-plane stack is not owned by signed transition")
    if require_outputs:
        outputs = _stack_outputs(stack, "control-plane")
        output_expected = {
            "AgentCoreStackName": config.agentcore_stack_name,
            "ControlPlaneImageUri": evidence.control_image,
            "DeploymentTransitionId": transition_id,
        }
        if any(outputs.get(name) != value for name, value in output_expected.items()):
            raise MutationBrokerError("control-plane outputs do not match signed transition")
    return parameters


def _client_token(transition_id: str, operation: str) -> str:
    digest = hashlib.sha256(f"{transition_id}:{operation}".encode("ascii")).hexdigest()
    return f"axonllm-transition-{digest}"


def _pending(
    evidence: TransitionEvidence,
    *,
    operation: str,
    phase: str,
) -> dict[str, str]:
    return {
        "status": "PENDING",
        "operation": operation,
        "phase": phase,
        "transitionId": evidence.transition["transitionId"],
    }


def _complete(
    evidence: TransitionEvidence,
    *,
    operation: str,
) -> dict[str, str]:
    return {
        "status": "COMPLETE",
        "operation": operation,
        "phase": "COMPLETE",
        "transitionId": evidence.transition["transitionId"],
    }


def _runtime_desired_overrides(
    evidence: TransitionEvidence,
    operation: str,
) -> dict[str, str]:
    intent = evidence.intent
    if operation == "FINALIZE":
        production = intent["candidateRuntimeVersion"]
    else:
        production = intent["previousProductionRuntimeVersion"] or ""
    return {
        "CandidateEndpointName": intent["candidateEndpointName"],
        "ProductionRuntimeVersion": production,
        "PublishCandidateEndpoint": "false",
        "PublishProductionEndpoint": ("true" if production else "false"),
    }


def _runtime_outputs_are_desired(
    outputs: Mapping[str, str],
    *,
    evidence: TransitionEvidence,
    operation: str,
) -> bool:
    intent = evidence.intent
    candidate_fields = {
        "CandidateRuntimeEndpointArn",
        "CandidateRuntimeEndpointName",
        "CandidateRuntimeVersion",
    }
    if candidate_fields & set(outputs):
        return False
    desired = (
        intent["candidateRuntimeVersion"] if operation == "FINALIZE" else intent["previousProductionRuntimeVersion"]
    )
    production_fields = {
        "RuntimeEndpointArn",
        "RuntimeEndpointName",
        "ProductionRuntimeVersion",
    }
    if desired is None:
        return not bool(production_fields & set(outputs))
    return (
        outputs.get("RuntimeEndpointArn") == intent["productionEndpointArn"]
        and outputs.get("RuntimeEndpointName") == "production"
        and outputs.get("ProductionRuntimeVersion") == desired
        and production_fields <= set(outputs)
    )


def _runtime_is_promoted_candidate(
    outputs: Mapping[str, str],
    *,
    evidence: TransitionEvidence,
) -> bool:
    intent = evidence.intent
    expected = {
        "CandidateRuntimeEndpointArn": (f"{intent['runtimeArn']}/runtime-endpoint/{intent['candidateEndpointName']}"),
        "CandidateRuntimeEndpointName": intent["candidateEndpointName"],
        "CandidateRuntimeVersion": intent["candidateRuntimeVersion"],
        "RuntimeEndpointArn": intent["productionEndpointArn"],
        "RuntimeEndpointName": "production",
        "ProductionRuntimeVersion": intent["candidateRuntimeVersion"],
    }
    return all(outputs.get(name) == value for name, value in expected.items())


def _update_runtime(
    clients: BrokerClients,
    *,
    config: BrokerConfig,
    stack: dict[str, Any],
    parameters: Mapping[str, str],
    evidence: TransitionEvidence,
    operation: str,
    budget: _MutationBudget,
) -> None:
    overrides = _runtime_desired_overrides(evidence, operation)
    missing = set(overrides) - set(parameters)
    if missing:
        raise MutationBrokerError("AgentCore stack lacks fixed publication parameters")
    request_parameters = [
        (
            {
                "ParameterKey": name,
                "ParameterValue": overrides[name],
            }
            if name in overrides
            else {
                "ParameterKey": name,
                "UsePreviousValue": True,
            }
        )
        for name in sorted(parameters)
    ]
    budget.consume(f"runtime-{operation.lower()}")
    try:
        clients.cloudformation.update_stack(
            StackName=config.agentcore_stack_name,
            UsePreviousTemplate=True,
            Parameters=request_parameters,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            RoleARN=config.execution_role_arn,
            ClientRequestToken=_client_token(
                evidence.transition["transitionId"],
                f"runtime-{operation.lower()}",
            ),
        )
    except Exception as exc:
        code, message, _delete_marker = _aws_error(exc)
        if not (code == "ValidationError" and message == "No updates are to be performed."):
            raise MutationBrokerError("bounded AgentCore stack update failed") from exc
    if stack.get("StackName") != config.agentcore_stack_name:
        raise MutationBrokerError("AgentCore mutation target changed")


def _restore_control_plane(
    clients: BrokerClients,
    *,
    config: BrokerConfig,
    parameters: Mapping[str, str],
    previous: Mapping[str, str],
    evidence: TransitionEvidence,
    budget: _MutationBudget,
) -> None:
    missing = set(previous) - set(parameters)
    if missing:
        raise MutationBrokerError("prior control-plane parameters are unavailable")
    request_parameters = [
        (
            {
                "ParameterKey": name,
                "ParameterValue": previous[name],
            }
            if name in previous
            else {
                "ParameterKey": name,
                "UsePreviousValue": True,
            }
        )
        for name in sorted(parameters)
    ]
    budget.consume("control-plane-restore")
    try:
        clients.cloudformation.update_stack(
            StackName=config.control_plane_stack_name,
            UsePreviousTemplate=True,
            Parameters=request_parameters,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            RoleARN=config.execution_role_arn,
            ClientRequestToken=_client_token(
                evidence.transition["transitionId"],
                "control-plane-restore",
            ),
        )
    except Exception as exc:
        code, message, _delete_marker = _aws_error(exc)
        if not (code == "ValidationError" and message == "No updates are to be performed."):
            raise MutationBrokerError("bounded control-plane restore failed") from exc


def _load_balancer_arn(
    cloudformation_client: Any,
    *,
    config: BrokerConfig,
) -> str | None:
    resources: list[Any] = []
    next_token: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        request = {"StackName": config.control_plane_stack_name}
        if next_token is not None:
            request["NextToken"] = next_token
        try:
            response = cloudformation_client.list_stack_resources(**request)
        except Exception as exc:
            raise MutationBrokerError("cannot inspect first-launch control-plane resources") from exc
        page = response.get("StackResourceSummaries") if isinstance(response, dict) else None
        if not isinstance(page, list):
            raise MutationBrokerError("control-plane resource listing is malformed")
        resources.extend(page)
        raw_next = response.get("NextToken")
        if raw_next is None:
            break
        if not isinstance(raw_next, str) or not raw_next or raw_next in seen:
            raise MutationBrokerError("control-plane resource pagination is malformed")
        seen.add(raw_next)
        next_token = raw_next
    else:
        raise MutationBrokerError("control-plane resource listing exceeds bounded pages")
    arns = [
        item.get("PhysicalResourceId")
        for item in resources
        if isinstance(item, dict)
        and item.get("ResourceType") == "AWS::ElasticLoadBalancingV2::LoadBalancer"
        and item.get("ResourceStatus") != "DELETE_COMPLETE"
    ]
    if not arns:
        return None
    expected_prefix = (
        f"arn:{config.partition}:elasticloadbalancing:{config.region}:{config.account_id}:loadbalancer/app/"
    )
    if len(arns) != 1 or not isinstance(arns[0], str) or not arns[0].startswith(expected_prefix):
        raise MutationBrokerError("control-plane load balancer ownership is ambiguous")
    return arns[0]


def _deletion_protection_enabled(
    elbv2_client: Any,
    load_balancer_arn: str,
) -> bool:
    try:
        response = elbv2_client.describe_load_balancer_attributes(
            LoadBalancerArn=load_balancer_arn,
        )
    except Exception as exc:
        raise MutationBrokerError("cannot inspect first-launch deletion protection") from exc
    attributes = response.get("Attributes") if isinstance(response, dict) else None
    if not isinstance(attributes, list):
        raise MutationBrokerError("load balancer attributes are malformed")
    values = [
        item.get("Value")
        for item in attributes
        if isinstance(item, dict) and item.get("Key") == "deletion_protection.enabled"
    ]
    if len(values) != 1 or values[0] not in {"true", "false"}:
        raise MutationBrokerError("load balancer deletion protection is ambiguous")
    return values[0] == "true"


def _rollback_control_plane(
    clients: BrokerClients,
    *,
    config: BrokerConfig,
    evidence: TransitionEvidence,
    base: str,
    budget: _MutationBudget,
) -> str:
    control = evidence.intent["controlPlane"]
    stack = _describe_stack(
        clients.cloudformation,
        config=config,
        stack_name=config.control_plane_stack_name,
    )
    if control["stackExisted"]:
        if stack is None or stack.get("StackId") != control["previousStackId"]:
            raise MutationBrokerError("prior control-plane stack identity changed")
        status = _stack_status(stack, "control-plane")
        if status in _IN_PROGRESS_STACK_STATUSES:
            return "CONTROL_PLANE_WAIT"
        if status not in _HEALTHY_STACK_STATUSES:
            raise MutationBrokerError("existing control-plane stack is not healthy")
        parameters = _stack_parameters(stack, "control-plane")
        previous = control["previousParameters"]
        if all(parameters.get(name) == value for name, value in previous.items()):
            outputs = _stack_outputs(stack, "control-plane")
            prior_image = previous.get("ControlPlaneVerifiedImageUri")
            prior_agentcore_stack = previous.get("AgentCoreStackName")
            prior_transition = previous.get("DeploymentTransitionId")
            if (
                not isinstance(prior_image, str)
                or outputs.get("ControlPlaneImageUri") != prior_image
                or prior_agentcore_stack != config.agentcore_stack_name
                or outputs.get("AgentCoreStackName") != config.agentcore_stack_name
                or (prior_transition is not None and outputs.get("DeploymentTransitionId") != prior_transition)
            ):
                raise MutationBrokerError("restored control-plane outputs do not match prior signed parameters")
            return "DONE"
        _validate_control_target(
            stack,
            evidence=evidence,
            config=config,
            require_outputs=True,
        )
        _assert_no_commit_signal(
            clients.s3,
            config=config,
            base=base,
        )
        _restore_control_plane(
            clients,
            config=config,
            parameters=parameters,
            previous=previous,
            evidence=evidence,
            budget=budget,
        )
        return "CONTROL_PLANE_RESTORE"

    if stack is None:
        return "DONE"
    status = _stack_status(stack, "control-plane")
    parameters = _validate_control_target(
        stack,
        evidence=evidence,
        config=config,
        require_outputs=status in _HEALTHY_STACK_STATUSES,
    )
    if parameters.get("DeploymentTransitionId") != evidence.transition["transitionId"]:
        raise MutationBrokerError("first-launch control-plane deletion lacks transition ownership")
    if status in _IN_PROGRESS_STACK_STATUSES:
        return "CONTROL_PLANE_WAIT"
    if status not in _DELETABLE_FIRST_LAUNCH_STATUSES:
        raise MutationBrokerError("first-launch control-plane stack cannot be deleted")
    load_balancer_arn = _load_balancer_arn(
        clients.cloudformation,
        config=config,
    )
    if load_balancer_arn is not None and _deletion_protection_enabled(
        clients.elbv2,
        load_balancer_arn,
    ):
        _assert_no_commit_signal(
            clients.s3,
            config=config,
            base=base,
        )
        budget.consume("control-plane-disable-deletion-protection")
        try:
            clients.elbv2.modify_load_balancer_attributes(
                LoadBalancerArn=load_balancer_arn,
                Attributes=[
                    {
                        "Key": "deletion_protection.enabled",
                        "Value": "false",
                    }
                ],
            )
        except Exception as exc:
            raise MutationBrokerError("cannot disable owned control-plane deletion protection") from exc
        return "CONTROL_PLANE_DELETE_PROTECTION"
    _assert_no_commit_signal(
        clients.s3,
        config=config,
        base=base,
    )
    budget.consume("control-plane-delete")
    try:
        clients.cloudformation.delete_stack(
            StackName=config.control_plane_stack_name,
            RoleARN=config.execution_role_arn,
            ClientRequestToken=_client_token(
                evidence.transition["transitionId"],
                "control-plane-delete",
            ),
        )
    except Exception as exc:
        raise MutationBrokerError("bounded first-launch control-plane deletion failed") from exc
    return "CONTROL_PLANE_DELETE"


def _finalize_control_plane(
    clients: BrokerClients,
    *,
    config: BrokerConfig,
    evidence: TransitionEvidence,
) -> str:
    stack = _describe_stack(
        clients.cloudformation,
        config=config,
        stack_name=config.control_plane_stack_name,
    )
    if stack is None:
        raise MutationBrokerError("committed control-plane stack is absent")
    control = evidence.intent["controlPlane"]
    if control["stackExisted"] and stack.get("StackId") != control["previousStackId"]:
        raise MutationBrokerError("committed control-plane stack identity changed")
    status = _stack_status(stack, "control-plane")
    if status in _IN_PROGRESS_STACK_STATUSES:
        return "CONTROL_PLANE_WAIT"
    if status not in _HEALTHY_STACK_STATUSES:
        raise MutationBrokerError("committed control-plane stack is not healthy")
    _validate_control_target(
        stack,
        evidence=evidence,
        config=config,
        require_outputs=True,
    )
    return "DONE"


def handle_event(
    event: Any,
    *,
    clients: BrokerClients,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Validate one invocation and execute at most one bounded mutation."""

    config = BrokerConfig.from_env(environ)
    invocation = _parse_invocation(event)
    current_time = _normalize_now(now)
    evidence = _load_transition_evidence(
        invocation,
        clients=clients,
        config=config,
        now=current_time,
    )
    operation = "FINALIZE" if evidence.commit_valid else "ROLLBACK"
    base = f"{config.evidence_prefix}/{invocation.repository}/{invocation.run_id}/{invocation.run_attempt}"
    if operation == "ROLLBACK" and current_time < evidence.rollback_not_before:
        return _pending(
            evidence,
            operation=operation,
            phase="ROLLBACK_NOT_BEFORE",
        )

    budget = _MutationBudget()
    if operation == "FINALIZE":
        control_phase = _finalize_control_plane(
            clients,
            config=config,
            evidence=evidence,
        )
        if control_phase != "DONE":
            return _pending(
                evidence,
                operation=operation,
                phase=control_phase,
            )
    else:
        control_phase = _rollback_control_plane(
            clients,
            config=config,
            evidence=evidence,
            base=base,
            budget=budget,
        )
        if control_phase != "DONE":
            return _pending(
                evidence,
                operation=operation,
                phase=control_phase,
            )

    runtime_stack = _describe_stack(
        clients.cloudformation,
        config=config,
        stack_name=config.agentcore_stack_name,
    )
    if runtime_stack is None:
        raise MutationBrokerError("AgentCore production stack is absent")
    parameters, outputs = _validate_runtime_stack(
        runtime_stack,
        evidence=evidence,
        config=config,
    )
    status = _stack_status(runtime_stack, "AgentCore")
    if status in _IN_PROGRESS_STACK_STATUSES:
        return _pending(
            evidence,
            operation=operation,
            phase="RUNTIME_WAIT",
        )
    overrides = _runtime_desired_overrides(evidence, operation)
    parameters_desired = all(parameters.get(name) == value for name, value in overrides.items())
    outputs_desired = _runtime_outputs_are_desired(
        outputs,
        evidence=evidence,
        operation=operation,
    )
    if parameters_desired and outputs_desired:
        return _complete(evidence, operation=operation)
    if parameters_desired != outputs_desired:
        raise MutationBrokerError("AgentCore parameters and outputs disagree on transition state")
    if not _runtime_is_promoted_candidate(
        outputs,
        evidence=evidence,
    ):
        raise MutationBrokerError("AgentCore no longer exposes the exact promoted candidate transition")
    if operation == "ROLLBACK":
        _assert_no_commit_signal(
            clients.s3,
            config=config,
            base=base,
        )
    _update_runtime(
        clients,
        config=config,
        stack=runtime_stack,
        parameters=parameters,
        evidence=evidence,
        operation=operation,
        budget=budget,
    )
    return _pending(
        evidence,
        operation=operation,
        phase="RUNTIME_UPDATE",
    )


def lambda_handler(
    event: Any,
    _context: Any,
    *,
    clients: BrokerClients | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """AWS Lambda entry point; optional arguments support isolated tests."""

    config = BrokerConfig.from_env(environ)
    if clients is None:
        try:
            import boto3

            clients = BrokerClients(
                s3=boto3.client("s3", region_name=config.region),
                kms=boto3.client("kms", region_name=config.region),
                cloudformation=boto3.client(
                    "cloudformation",
                    region_name=config.region,
                ),
                elbv2=boto3.client(
                    "elbv2",
                    region_name=config.region,
                ),
            )
        except Exception as exc:
            raise MutationBrokerError("cannot initialize production mutation broker clients") from exc
    return handle_event(
        event,
        clients=clients,
        environ=environ,
        now=now,
    )
