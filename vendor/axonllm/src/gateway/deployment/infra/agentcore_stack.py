"""Production Bedrock AgentCore deployment for the AxonLLM agent entrypoint."""

import hashlib
import json
import math
import re
from dataclasses import dataclass

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    CfnResource,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
    aws_bedrockagentcore as agentcore,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    custom_resources as cr,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
)
from constructs import Construct

if __package__:
    from .application_state import (
        application_state_mode,
        build_application_state_resources,
        external_agentcore_application_state_access,
        managed_application_state_access,
    )
    from .runtime_network import (
        build_runtime_network,
        runtime_network_requires_prefix_list,
    )
else:
    from application_state import (
        application_state_mode,
        build_application_state_resources,
        external_agentcore_application_state_access,
        managed_application_state_access,
    )
    from runtime_network import (
        build_runtime_network,
        runtime_network_requires_prefix_list,
    )


_ATHENA_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ATHENA_ROLE_ARN = re.compile(
    r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:"
    r"role/[A-Za-z0-9+=,.@_/-]{1,512}$"
)
ATHENA_QUERY_ACTIONS = [
    "athena:GetQueryExecution",
    "athena:GetQueryResults",
    "athena:GetWorkGroup",
    "athena:StartQueryExecution",
    "athena:StopQueryExecution",
]
ATHENA_ASSUME_ROLE_ACTIONS = [
    "sts:AssumeRole",
    "sts:SetSourceIdentity",
    "sts:TagSession",
]
_MAX_ATHENA_BINDINGS_CHARACTERS = 2_048
_AGENTCORE_MAX_SESSION_SECONDS = 4 * 60 * 60
_RECOVERY_PROPAGATION_MARGIN_SECONDS = 5 * 60
_RECOVERY_MIN_QUIESCENCE_SECONDS = (
    _AGENTCORE_MAX_SESSION_SECONDS
    + _RECOVERY_PROPAGATION_MARGIN_SECONDS
)
_RUNTIME_DYNAMODB_STANDARD_ACTIONS = [
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:UpdateItem",
]
_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS = [
    "dynamodb:TransactWriteItems",
]
_RUNTIME_DYNAMODB_ACTIONS = [
    *_RUNTIME_DYNAMODB_STANDARD_ACTIONS,
    *_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS,
]
_RUNTIME_SQS_ACTIONS = [
    "sqs:ChangeMessageVisibility",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
    "sqs:SendMessage",
]
_AGENTCORE_ENABLED_PROVIDERS = ",".join(
    (
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
)
_PROVIDER_NAME_PATTERN = (
    r"^(?:ai21|anthropic|azure_openai|bedrock|bedrock-mantle|cohere|"
    r"fireworks|google_ai|groq|openai|together|vertex_ai|xai)"
    r"(?:,(?:ai21|anthropic|azure_openai|bedrock|bedrock-mantle|cohere|"
    r"fireworks|google_ai|groq|openai|together|vertex_ai|xai))*$"
)


_AGENTCORE_RECOVERY_GUARD = """\
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


control = boto3.client("bedrock-agentcore-control")
cloudformation = boto3.client("cloudformation")
ecs = boto3.client("ecs")
autoscaling = boto3.client("application-autoscaling")

_PHYSICAL_ID = "AxonLLMAgentCoreRecoveryGuard"
_BLOCKED_MODES = {"quiesced", "selected"}
_SUSPENSION_KEYS = (
    "DynamicScalingInSuspended",
    "DynamicScalingOutSuspended",
    "ScheduledScalingSuspended",
)


def _pages(client, method_name, result_name, **arguments):
    results = []
    token = None
    while True:
        request = dict(arguments)
        if token:
            request["nextToken"] = token
        response = getattr(client, method_name)(**request)
        page = response.get(result_name, [])
        if not isinstance(page, list):
            raise RuntimeError(f"{method_name} returned malformed results")
        results.extend(page)
        token = response.get("nextToken")
        if not token:
            return results


def _runtime_id(runtime_name):
    matches = [
        runtime
        for runtime in _pages(
            control,
            "list_agent_runtimes",
            "agentRuntimes",
            maxResults=100,
        )
        if runtime.get("agentRuntimeName") == runtime_name
    ]
    if len(matches) != 1 or not matches[0].get("agentRuntimeId"):
        raise RuntimeError(
            "recovery guard could not resolve exactly one AgentCore runtime"
        )
    return matches[0]["agentRuntimeId"]


def _assert_no_runtime_endpoints(runtime_name):
    runtime_id = _runtime_id(runtime_name)
    endpoints = _pages(
        control,
        "list_agent_runtime_endpoints",
        "runtimeEndpoints",
        agentRuntimeId=runtime_id,
        maxResults=100,
    )
    if endpoints:
        summary = sorted(
            f"{item.get('name', 'unknown')}:{item.get('status', 'unknown')}"
            for item in endpoints
        )
        raise RuntimeError(
            "AgentCore recovery requires every runtime endpoint removed: "
            + ", ".join(summary)
        )


def _control_plane_outputs(stack_name):
    try:
        response = cloudformation.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if (
            error.get("Code") == "ValidationError"
            and "does not exist" in error.get("Message", "")
        ):
            return None
        raise
    stacks = response.get("Stacks", [])
    if len(stacks) != 1:
        raise RuntimeError(
            "recovery guard could not resolve the control-plane stack"
        )
    outputs = {
        item.get("OutputKey"): item.get("OutputValue")
        for item in stacks[0].get("Outputs", [])
    }
    required = {
        "AgentCoreStackName",
        "ClusterName",
        "PrimaryStateTableName",
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "SelectedRuntimeStateTableName",
        "ServiceName",
    }
    missing = sorted(
        name for name in required if name not in outputs
    )
    if missing:
        raise RuntimeError(
            "control-plane stack is missing recovery outputs: "
            + ", ".join(missing)
        )
    return outputs


def _assert_table_namespace(primary, selected):
    if selected == primary:
        return
    if not selected.startswith(f"{primary}-restore-validation-"):
        raise RuntimeError(
            "AgentCore recovery table is outside the restore-validation "
            "namespace"
        )


def _assert_control_plane_quiesced(stack_name):
    outputs = _control_plane_outputs(stack_name)
    if outputs is None:
        return None
    resource_id = (
        f"service/{outputs['ClusterName']}/{outputs['ServiceName']}"
    )
    targets = autoscaling.describe_scalable_targets(
        ServiceNamespace="ecs",
        ResourceIds=[resource_id],
        ScalableDimension="ecs:service:DesiredCount",
    ).get("ScalableTargets", [])
    if len(targets) != 1:
        raise RuntimeError(
            "recovery requires exactly one control-plane scalable target"
        )
    target = targets[0]
    suspended = target.get("SuspendedState", {})
    if target.get("MinCapacity") != 0 or not all(
        suspended.get(key) is True for key in _SUSPENSION_KEYS
    ):
        raise RuntimeError(
            "recovery requires the control plane at minimum capacity zero "
            "with every scaling path suspended"
        )
    response = ecs.describe_services(
        cluster=outputs["ClusterName"],
        services=[outputs["ServiceName"]],
    )
    if response.get("failures") or len(response.get("services", [])) != 1:
        raise RuntimeError(
            "recovery guard could not resolve the control-plane service"
        )
    service = response["services"][0]
    counts = {
        name: service.get(name)
        for name in ("desiredCount", "pendingCount", "runningCount")
    }
    if any(value != 0 for value in counts.values()):
        raise RuntimeError(
            "recovery requires a fully quiesced control plane: "
            f"{counts}"
        )
    return outputs


def _assert_control_plane_recovery_state(
    current,
    previous,
    transition,
    outputs,
):
    if outputs is None:
        return
    if (
        outputs["AgentCoreStackName"] != current["AgentCoreStackName"]
        or outputs["PrimaryStateTableName"] != current["PrimaryTable"]
    ):
        raise RuntimeError(
            "control-plane recovery ownership does not match AgentCore"
        )

    mode = current["Mode"]
    selected = current["SelectedTable"]
    approval = current["ApprovalId"]
    if transition == ("selected", "quiesced"):
        expected = (
            "selected",
            previous["SelectedTable"],
            previous["ApprovalId"],
        )
    elif transition == ("validation", "normal"):
        expected = (
            "selected",
            selected,
            approval,
        )
    elif transition == ("quiesced", "normal"):
        expected = (
            "quiesced",
            selected,
            previous["ApprovalId"],
        )
    else:
        expected_mode = {
            "normal": "normal",
            "quiesced": "quiesced",
            "selected": "selected",
            "validation": "selected",
        }[mode]
        expected = (expected_mode, selected, approval)
    actual = (
        outputs["RecoveryCutoverMode"],
        outputs["SelectedRuntimeStateTableName"],
        outputs["RecoveryApprovalId"],
    )
    if actual != expected:
        raise RuntimeError(
            "control-plane recovery state does not authorize this "
            f"AgentCore transition: expected {expected}, found {actual}"
        )


def _quiesced_epoch(physical_id):
    prefix = f"{_PHYSICAL_ID}:"
    if not physical_id.startswith(prefix):
        raise RuntimeError("recovery quiescence evidence is missing")
    try:
        value = int(physical_id[len(prefix):])
    except ValueError as exc:
        raise RuntimeError(
            "recovery quiescence evidence is malformed"
        ) from exc
    if value < 1:
        raise RuntimeError("recovery quiescence evidence is malformed")
    return value


def _result(physical_id, quiesced_at=None):
    if quiesced_at is None:
        timestamp = "not-quiesced"
    else:
        timestamp = datetime.fromtimestamp(
            quiesced_at,
            tz=timezone.utc,
        ).isoformat()
    return {
        "PhysicalResourceId": physical_id,
        "Data": {"QuiescedAt": timestamp},
    }


def handler(event, _context):
    if event["RequestType"] == "Delete":
        return _result(event.get("PhysicalResourceId", _PHYSICAL_ID))

    current = event["ResourceProperties"]
    mode = current.get("Mode")
    primary = current.get("PrimaryTable", "")
    target = current.get("SelectedTable", "")
    approval = current.get("ApprovalId", "")
    if not primary or not target:
        raise RuntimeError("AgentCore recovery table ownership is missing")
    _assert_table_namespace(primary, target)
    if event["RequestType"] == "Create":
        if mode != "normal" or target != primary or approval:
            raise RuntimeError(
                "a new AgentCore stack must start on the primary table "
                "in normal mode without a recovery approval"
            )
        return _result(_PHYSICAL_ID)

    previous = event.get("OldResourceProperties", {})
    for immutable in ("AgentCoreStackName", "PrimaryTable"):
        if current.get(immutable) != previous.get(immutable):
            raise RuntimeError(
                f"AgentCore recovery ownership changed: {immutable}"
            )
    old_mode = previous.get("Mode")
    old_target = previous.get("SelectedTable", "")
    old_approval = previous.get("ApprovalId", "")
    target_changed = target != old_target
    transition = (old_mode, mode)
    allowed = {
        ("normal", "normal"),
        ("normal", "quiesced"),
        ("quiesced", "quiesced"),
        ("quiesced", "normal"),
        ("quiesced", "selected"),
        ("selected", "selected"),
        ("selected", "quiesced"),
        ("selected", "validation"),
        ("validation", "validation"),
        ("validation", "normal"),
    }
    if transition not in allowed:
        raise RuntimeError(
            f"unsupported AgentCore recovery transition: "
            f"{old_mode} -> {mode}"
        )

    if target_changed and transition not in {
        ("quiesced", "selected"),
        ("selected", "quiesced"),
    }:
        raise RuntimeError(
            "AgentCore state table changes require a blocked "
            "quiesced -> selected transition"
        )
    if transition == ("quiesced", "selected") and not target_changed:
        raise RuntimeError(
            "selected mode requires an approved state table change"
        )
    if transition == ("selected", "validation") and target_changed:
        raise RuntimeError(
            "validation must use the table already fixed in selected mode"
        )

    if mode == "quiesced" and old_mode == "normal":
        if not approval or approval == old_approval:
            raise RuntimeError(
                "entering recovery requires a new non-empty approval ID"
            )
    elif mode in {"selected", "validation"}:
        if not approval or approval != old_approval:
            raise RuntimeError(
                "recovery approval ID changed during a protected transition"
            )

    must_be_quiesced = (
        mode in _BLOCKED_MODES
        or old_mode in _BLOCKED_MODES
        or old_mode == "validation"
    )
    if must_be_quiesced:
        control_plane = _assert_control_plane_quiesced(
            current["ControlPlaneStackName"]
        )
    else:
        control_plane = _control_plane_outputs(
            current["ControlPlaneStackName"]
        )
    _assert_control_plane_recovery_state(
        current,
        previous,
        transition,
        control_plane,
    )
    if mode in _BLOCKED_MODES:
        _assert_no_runtime_endpoints(current["RuntimeName"])

    physical_id = event.get("PhysicalResourceId", _PHYSICAL_ID)
    quiesced_at = None
    if mode == "quiesced" and old_mode == "normal":
        quiesced_at = int(time.time())
        physical_id = f"{_PHYSICAL_ID}:{quiesced_at}"
    elif old_mode in _BLOCKED_MODES:
        quiesced_at = _quiesced_epoch(physical_id)

    if transition == ("quiesced", "selected"):
        minimum = int(current["MinimumQuiescenceSeconds"])
        elapsed = int(time.time()) - quiesced_at
        if elapsed < minimum:
            raise RuntimeError(
                "AgentCore recovery quiescence is too recent: "
                f"{elapsed}s elapsed, {minimum}s required"
            )

    if mode == "normal":
        physical_id = _PHYSICAL_ID
        quiesced_at = None
    return _result(physical_id, quiesced_at)
"""

_ROUTING_CONFIG_SEEDER = """\
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import re
import zlib

import boto3
from botocore.exceptions import ClientError


dynamodb = boto3.client("dynamodb")
kms = boto3.client("kms")

_PHYSICAL_ID = "AxonLLMRoutingConfigSeeder"
_MAX_DOCUMENT_BYTES = 350 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE_SCHEMA = "axonllm.routing-config-signature/v1"
_CONFIG_SCHEMA = "axonllm.routing-config/v1"
_ALGORITHM = "ECDSA_SHA_256"


def _string(item, name):
    value = item.get(name)
    if not isinstance(value, dict) or set(value) != {"S"}:
        raise RuntimeError("routing configuration row is malformed")
    result = value["S"]
    if not isinstance(result, str):
        raise RuntimeError("routing configuration row is malformed")
    return result


def _number(item, name):
    value = item.get(name)
    if not isinstance(value, dict) or set(value) != {"N"}:
        raise RuntimeError("routing configuration row is malformed")
    raw = value["N"]
    if (
        not isinstance(raw, str)
        or not raw.isdigit()
        or int(raw) < 1
    ):
        raise RuntimeError("routing configuration row is malformed")
    return int(raw)


def _canonical_document(document, expected_digest):
    try:
        encoded = document.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise RuntimeError(
            "routing configuration document is malformed"
        ) from exc
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise RuntimeError("routing configuration document is too large")
    try:
        value = json.loads(document)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "routing configuration document is malformed"
        ) from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("models"), list)
        or json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        != document
    ):
        raise RuntimeError(
            "routing configuration document is not canonical"
        )
    digest = hashlib.sha256(encoded).hexdigest()
    if digest != expected_digest or _SHA256.fullmatch(digest) is None:
        raise RuntimeError(
            "routing configuration document checksum mismatch"
        )
    return digest


def _signing_digest(revision, document_digest):
    payload = json.dumps(
        {
            "document_sha256": document_digest,
            "revision": revision,
            "schema": _CONFIG_SCHEMA,
            "signature_schema": _SIGNATURE_SCHEMA,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _signature_bytes(value):
    try:
        signature = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError(
            "routing configuration signature is malformed"
        ) from exc
    if (
        not signature
        or len(signature) > 512
        or base64.b64encode(signature).decode("ascii") != value
    ):
        raise RuntimeError(
            "routing configuration signature is malformed"
        )
    return signature


def _load(table_name):
    response = dynamodb.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": "MODEL_REGISTRY"},
            "SK": {"S": "CONFIG"},
        },
        ConsistentRead=True,
    )
    item = response.get("Item")
    if item is None:
        return None
    if not isinstance(item, dict):
        raise RuntimeError("routing configuration row is malformed")
    return item


def _verify_existing(item, key_arn):
    if (
        _string(item, "entity_type") != "model_registry"
        or _number(item, "schema_version") != 2
        or _string(item, "signature_schema") != _SIGNATURE_SCHEMA
        or _string(item, "signing_key_arn") != key_arn
        or _string(item, "signing_algorithm") != _ALGORITHM
    ):
        raise RuntimeError("routing configuration signature is malformed")
    revision = _number(item, "revision")
    document = _string(item, "document")
    document_digest = _string(item, "document_sha256")
    _canonical_document(document, document_digest)
    signature = _signature_bytes(_string(item, "signature"))
    response = kms.verify(
        KeyId=key_arn,
        Message=_signing_digest(revision, document_digest),
        MessageType="DIGEST",
        Signature=signature,
        SigningAlgorithm=_ALGORITHM,
    )
    if (
        response.get("KeyId") != key_arn
        or response.get("SigningAlgorithm") != _ALGORITHM
        or response.get("SignatureValid") is not True
    ):
        raise RuntimeError(
            "routing configuration signature verification failed"
        )
    return revision


def _initial_document(encoded):
    try:
        compressed = base64.b64decode(encoded, validate=True)
        decompressor = zlib.decompressobj()
        document_bytes = decompressor.decompress(
            compressed,
            _MAX_DOCUMENT_BYTES + 1,
        )
        if (
            len(document_bytes) > _MAX_DOCUMENT_BYTES
            or not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise ValueError("invalid compressed payload")
        document_bytes += decompressor.flush()
        document = document_bytes.decode("utf-8")
    except (
        ValueError,
        binascii.Error,
        UnicodeError,
        zlib.error,
    ) as exc:
        raise RuntimeError(
            "initial routing configuration is malformed"
        ) from exc
    digest = hashlib.sha256(document_bytes).hexdigest()
    _canonical_document(document, digest)
    return document, digest


def _candidate(item, encoded):
    if item is None:
        document, digest = _initial_document(encoded)
        return document, digest, 1, None
    if _string(item, "entity_type") != "model_registry":
        raise RuntimeError("routing configuration row is malformed")
    schema_version = _number(item, "schema_version")
    if schema_version == 2:
        return None
    if schema_version != 1:
        raise RuntimeError("routing configuration row is malformed")
    revision = _number(item, "revision")
    document = _string(item, "document")
    digest = _string(item, "document_sha256")
    _canonical_document(document, digest)
    return document, digest, revision, item


def _sign(key_arn, revision, document_digest):
    response = kms.sign(
        KeyId=key_arn,
        Message=_signing_digest(revision, document_digest),
        MessageType="DIGEST",
        SigningAlgorithm=_ALGORITHM,
    )
    signature = response.get("Signature")
    if (
        response.get("KeyId") != key_arn
        or response.get("SigningAlgorithm") != _ALGORITHM
        or not isinstance(signature, bytes)
        or not signature
    ):
        raise RuntimeError("KMS returned invalid routing signature metadata")
    return base64.b64encode(signature).decode("ascii")


def _put(
    table_name,
    key_arn,
    document,
    document_digest,
    revision,
    signature,
    legacy,
):
    request = {
        "TableName": table_name,
        "Item": {
            "PK": {"S": "MODEL_REGISTRY"},
            "SK": {"S": "CONFIG"},
            "entity_type": {"S": "model_registry"},
            "schema_version": {"N": "2"},
            "revision": {"N": str(revision)},
            "document": {"S": document},
            "document_sha256": {"S": document_digest},
            "signature_schema": {"S": _SIGNATURE_SCHEMA},
            "signing_key_arn": {"S": key_arn},
            "signing_algorithm": {"S": _ALGORITHM},
            "signature": {"S": signature},
            "updated_at": {
                "S": datetime.now(timezone.utc).isoformat()
            },
        },
    }
    if legacy is None:
        request["ConditionExpression"] = (
            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
        )
    else:
        request.update(
            {
                "ConditionExpression": (
                    "entity_type = :entity_type AND "
                    "#revision = :revision AND "
                    "schema_version = :legacy_schema AND "
                    "document_sha256 = :document_sha256"
                ),
                "ExpressionAttributeNames": {
                    "#revision": "revision",
                },
                "ExpressionAttributeValues": {
                    ":entity_type": {"S": "model_registry"},
                    ":revision": {"N": str(revision)},
                    ":legacy_schema": {"N": "1"},
                    ":document_sha256": {"S": document_digest},
                },
            }
        )
    dynamodb.put_item(**request)


def handler(event, _context):
    properties = event.get("ResourceProperties", {})
    table_name = properties.get("TableName")
    key_arn = properties.get("KeyArn")
    encoded = properties.get("InitialRoutingConfigZlibBase64")
    if event.get("RequestType") == "Delete":
        return {"PhysicalResourceId": _PHYSICAL_ID}
    if not all(
        isinstance(value, str) and value
        for value in (table_name, key_arn, encoded)
    ):
        raise RuntimeError("routing configuration seed properties are invalid")

    current = _load(table_name)
    candidate = _candidate(current, encoded)
    if candidate is None:
        revision = _verify_existing(current, key_arn)
        return {
            "PhysicalResourceId": _PHYSICAL_ID,
            "Data": {"Revision": str(revision), "Status": "verified"},
        }

    document, document_digest, revision, legacy = candidate
    signature = _sign(key_arn, revision, document_digest)
    try:
        _put(
            table_name,
            key_arn,
            document,
            document_digest,
            revision,
            signature,
            legacy,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != (
            "ConditionalCheckFailedException"
        ):
            raise
        revision = _verify_existing(_load(table_name), key_arn)
        status = "verified_after_conflict"
    else:
        status = "seeded" if legacy is None else "migrated"
    return {
        "PhysicalResourceId": _PHYSICAL_ID,
        "Data": {"Revision": str(revision), "Status": status},
    }
"""


@dataclass(frozen=True)
class AthenaInfrastructureConfig:
    """Deployment-bound query role allow-list and execution limits."""

    bindings_json: str
    role_arns: tuple[str, ...]
    timeout_seconds: str
    max_rows: str
    max_result_bytes: str
    max_bytes_scanned: str
    poll_interval_seconds: str
    project_rpm: str
    principal_rpm: str
    project_concurrency: str
    principal_concurrency: str
    project_scan_bytes_per_minute: str
    principal_scan_bytes_per_minute: str
    max_datasources_per_tenant: str

    @property
    def enabled(self) -> bool:
        return bool(self.role_arns)

    def fingerprint(self) -> str:
        """Bind shared Athena IAM, endpoint, and runtime configuration."""
        values: dict[str, object] = {"enabled": self.enabled}
        if self.enabled:
            values.update(
                {
                    "athena_query_bindings": self.bindings_json,
                    "athena_query_max_bytes_scanned": self.max_bytes_scanned,
                    "athena_query_max_datasources_per_tenant": (
                        self.max_datasources_per_tenant
                    ),
                    "athena_query_max_result_bytes": self.max_result_bytes,
                    "athena_query_max_rows": self.max_rows,
                    "athena_query_poll_interval_seconds": (
                        self.poll_interval_seconds
                    ),
                    "athena_query_principal_concurrency": (
                        self.principal_concurrency
                    ),
                    "athena_query_principal_rpm": self.principal_rpm,
                    "athena_query_principal_scan_bytes_per_minute": (
                        self.principal_scan_bytes_per_minute
                    ),
                    "athena_query_project_concurrency": (
                        self.project_concurrency
                    ),
                    "athena_query_project_rpm": self.project_rpm,
                    "athena_query_project_scan_bytes_per_minute": (
                        self.project_scan_bytes_per_minute
                    ),
                    "athena_query_timeout_seconds": self.timeout_seconds,
                }
            )
        encoded = json.dumps(
            values,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def environment(self) -> dict[str, str]:
        return {
            "AXON_ATHENA_QUERY_ENABLED": (
                "true" if self.enabled else "false"
            ),
            "AXON_ATHENA_QUERY_BINDINGS": self.bindings_json,
            "AXON_ATHENA_QUERY_TIMEOUT_SECONDS": self.timeout_seconds,
            "AXON_ATHENA_QUERY_MAX_ROWS": self.max_rows,
            "AXON_ATHENA_QUERY_MAX_RESULT_BYTES": self.max_result_bytes,
            "AXON_ATHENA_QUERY_MAX_BYTES_SCANNED": (
                self.max_bytes_scanned
            ),
            "AXON_ATHENA_QUERY_POLL_INTERVAL_SECONDS": (
                self.poll_interval_seconds
            ),
            "AXON_ATHENA_QUERY_PROJECT_RPM": self.project_rpm,
            "AXON_ATHENA_QUERY_PRINCIPAL_RPM": self.principal_rpm,
            "AXON_ATHENA_QUERY_PROJECT_CONCURRENCY": (
                self.project_concurrency
            ),
            "AXON_ATHENA_QUERY_PRINCIPAL_CONCURRENCY": (
                self.principal_concurrency
            ),
            "AXON_ATHENA_QUERY_PROJECT_SCAN_BYTES_PER_MINUTE": (
                self.project_scan_bytes_per_minute
            ),
            "AXON_ATHENA_QUERY_PRINCIPAL_SCAN_BYTES_PER_MINUTE": (
                self.principal_scan_bytes_per_minute
            ),
            "AXON_ATHENA_QUERY_MAX_DATASOURCES_PER_TENANT": (
                self.max_datasources_per_tenant
            ),
            "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        }


def load_athena_infrastructure_config(
    construct: Construct,
) -> AthenaInfrastructureConfig:
    """Validate CDK context before it can become runtime authority."""

    raw_bindings = construct.node.try_get_context("athena_query_bindings")
    if raw_bindings in (None, ""):
        bindings: object = []
    elif isinstance(raw_bindings, str):
        try:
            bindings = json.loads(raw_bindings)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "athena_query_bindings must be valid JSON"
            ) from exc
    else:
        bindings = raw_bindings
    if not isinstance(bindings, list):
        raise ValueError("athena_query_bindings must be a JSON array")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError(
                f"athena_query_bindings[{index}] must be an object"
            )
        expected = {"tenant_id", "project_id", "role_arn"}
        if set(binding) != expected:
            raise ValueError(
                f"athena_query_bindings[{index}] must contain exactly "
                "tenant_id, project_id, and role_arn"
            )
        tenant_id = binding["tenant_id"]
        project_id = binding["project_id"]
        role_arn = binding["role_arn"]
        if (
            not isinstance(tenant_id, str)
            or _ATHENA_IDENTIFIER.fullmatch(tenant_id) is None
        ):
            raise ValueError(
                f"athena_query_bindings[{index}].tenant_id is invalid"
            )
        if (
            not isinstance(project_id, str)
            or _ATHENA_IDENTIFIER.fullmatch(project_id) is None
        ):
            raise ValueError(
                f"athena_query_bindings[{index}].project_id is invalid"
            )
        if (
            not isinstance(role_arn, str)
            or "*" in role_arn
            or _ATHENA_ROLE_ARN.fullmatch(role_arn) is None
        ):
            raise ValueError(
                f"athena_query_bindings[{index}].role_arn must be a "
                "concrete IAM role ARN"
            )
        identity = (tenant_id, project_id, role_arn)
        if identity in seen:
            raise ValueError("athena_query_bindings contains a duplicate")
        seen.add(identity)
        normalized.append(
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "role_arn": role_arn,
            }
        )

    bindings_json = json.dumps(
        normalized,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(bindings_json) > _MAX_ATHENA_BINDINGS_CHARACTERS:
        raise ValueError(
            "athena_query_bindings exceeds the AgentCore "
            "2,048-character environment value limit"
        )

    def integer_limit(
        context_name: str,
        default: int,
        minimum: int,
        maximum: int | None = None,
    ) -> str:
        value = construct.node.try_get_context(context_name)
        resolved = default if value in (None, "") else value
        if (
            isinstance(resolved, str)
            and re.fullmatch(r"[0-9]+", resolved) is not None
        ):
            resolved = int(resolved)
        if (
            isinstance(resolved, bool)
            or not isinstance(resolved, int)
            or resolved < minimum
            or (maximum is not None and resolved > maximum)
        ):
            if maximum is None:
                raise ValueError(
                    f"{context_name} must be at least {minimum}"
                )
            raise ValueError(
                f"{context_name} must be between {minimum} and {maximum}"
            )
        return str(resolved)

    def float_limit(
        context_name: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> str:
        value = construct.node.try_get_context(context_name)
        resolved = default if value in (None, "") else value
        if isinstance(resolved, str):
            try:
                resolved = float(resolved)
            except ValueError:
                pass
        if (
            isinstance(resolved, bool)
            or not isinstance(resolved, (int, float))
            or not math.isfinite(resolved)
            or not minimum <= resolved <= maximum
        ):
            raise ValueError(
                f"{context_name} must be between {minimum} and {maximum}"
            )
        return f"{resolved:g}"

    max_bytes_scanned = integer_limit(
        "athena_query_max_bytes_scanned",
        1024 * 1024 * 1024,
        1,
    )
    project_rpm = integer_limit(
        "athena_query_project_rpm",
        30,
        1,
        10_000,
    )
    principal_rpm = integer_limit(
        "athena_query_principal_rpm",
        10,
        1,
        10_000,
    )
    project_concurrency = integer_limit(
        "athena_query_project_concurrency",
        5,
        1,
        100,
    )
    principal_concurrency = integer_limit(
        "athena_query_principal_concurrency",
        2,
        1,
        100,
    )
    project_scan_bytes_per_minute = integer_limit(
        "athena_query_project_scan_bytes_per_minute",
        5 * 1024 * 1024 * 1024,
        1,
    )
    principal_scan_bytes_per_minute = integer_limit(
        "athena_query_principal_scan_bytes_per_minute",
        2 * 1024 * 1024 * 1024,
        1,
    )
    max_datasources_per_tenant = integer_limit(
        "athena_query_max_datasources_per_tenant",
        500,
        1,
        10_000,
    )
    if int(principal_rpm) > int(project_rpm):
        raise ValueError(
            "athena_query_principal_rpm must not exceed "
            "athena_query_project_rpm"
        )
    if int(principal_concurrency) > int(project_concurrency):
        raise ValueError(
            "athena_query_principal_concurrency must not exceed "
            "athena_query_project_concurrency"
        )
    if int(principal_scan_bytes_per_minute) > int(
        project_scan_bytes_per_minute
    ):
        raise ValueError(
            "principal query scan budget must not exceed project budget"
        )
    if int(max_bytes_scanned) > int(
        principal_scan_bytes_per_minute
    ):
        raise ValueError(
            "athena_query_max_bytes_scanned must fit within the "
            "principal aggregate scan budget"
        )

    return AthenaInfrastructureConfig(
        bindings_json=bindings_json,
        role_arns=tuple(
            sorted({binding["role_arn"] for binding in normalized})
        ),
        timeout_seconds=float_limit(
            "athena_query_timeout_seconds",
            30.0,
            0.001,
            300.0,
        ),
        max_rows=integer_limit(
            "athena_query_max_rows",
            1000,
            1,
            10_000,
        ),
        max_result_bytes=integer_limit(
            "athena_query_max_result_bytes",
            1024 * 1024,
            1024,
            16 * 1024 * 1024,
        ),
        max_bytes_scanned=max_bytes_scanned,
        poll_interval_seconds=float_limit(
            "athena_query_poll_interval_seconds",
            0.25,
            0.05,
            5.0,
        ),
        project_rpm=project_rpm,
        principal_rpm=principal_rpm,
        project_concurrency=project_concurrency,
        principal_concurrency=principal_concurrency,
        project_scan_bytes_per_minute=(
            project_scan_bytes_per_minute
        ),
        principal_scan_bytes_per_minute=(
            principal_scan_bytes_per_minute
        ),
        max_datasources_per_tenant=max_datasources_per_tenant,
    )


class AxonLLMAgentCoreStack(Stack):
    """Contained AgentCore runtime with tenant-safe identity and state."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        physical_suffix = (
            f"-{deployment_namespace}" if deployment_namespace else ""
        )
        removal_policy = (
            RemovalPolicy.DESTROY
            if deployment_namespace
            else RemovalPolicy.RETAIN
        )
        runtime_suffix = deployment_namespace.replace("-", "_")
        runtime_name = (
            f"axonllm_{runtime_suffix}"
            if runtime_suffix
            else "axonllm"
        )
        control_plane_stack_name = (
            f"AxonLLMControlPlaneStack{physical_suffix}"
        )
        state_stack_default = (
            f"AxonLLMApplicationStateStack{physical_suffix}"
        )
        state_mode = application_state_mode(self)
        query_config = load_athena_infrastructure_config(self)
        rehearsal_control_table_arn = (
            CfnParameter(
                self,
                "RehearsalControlTableArn",
                type="String",
                allowed_pattern=(
                    rf"^arn:aws:dynamodb:{re.escape(self.region)}:"
                    rf"{('[0-9]{12}' if Token.is_unresolved(self.account) else re.escape(self.account))}:"
                    r"table/axonllm-rehearsal-control-ledger$"
                ),
                constraint_description=(
                    "must be the retained rehearsal-control ledger ARN in "
                    "this stack's AWS region and account"
                ),
                description=(
                    "Exact retained rehearsal-control ledger ARN used only "
                    "by an isolated qualification runtime"
                ),
            )
            if deployment_namespace
            else None
        )

        oidc_issuer = CfnParameter(
            self,
            "OidcIssuer",
            type="String",
            min_length=1,
            allowed_pattern=r"^https://[^?#\s]+$",
            description="Exact issuer accepted by AxonLLM for OIDC bearer tokens",
        )
        oidc_discovery_url = CfnParameter(
            self,
            "OidcDiscoveryUrl",
            type="String",
            min_length=1,
            allowed_pattern=(
                r"^https://[^?#\s]+/\.well-known/openid-configuration$"
            ),
            description="OIDC discovery URL used by the AgentCore JWT authorizer",
        )
        oidc_client_ids = CfnParameter(
            self,
            "OidcClientIds",
            type="CommaDelimitedList",
            allowed_pattern=r"^[^\s,]+$",
            description="OIDC client IDs allowed to invoke the runtime",
        )
        oidc_audiences = CfnParameter(
            self,
            "OidcAudiences",
            type="CommaDelimitedList",
            allowed_pattern=r"^[^\s,]+$",
            description="OIDC audiences allowed to invoke the runtime",
        )
        oidc_tenant_claim = CfnParameter(
            self,
            "OidcTenantClaim",
            type="String",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^\S+$",
            description=(
                "Signed OIDC claim containing the AxonLLM tenant hint"
            ),
        )
        oidc_project_claim = CfnParameter(
            self,
            "OidcProjectClaim",
            type="String",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^\S+$",
            description=(
                "Signed OIDC claim containing the AxonLLM project hint"
            ),
        )
        approved_https_prefix_list_id = (
            CfnParameter(
                self,
                "ApprovedHttpsPrefixListId",
                type="String",
                allowed_pattern=r"^pl-[0-9a-fA-F]+$",
                constraint_description=(
                    "must be an EC2 managed prefix list ID"
                ),
                description=(
                    "Managed prefix list containing approved OIDC and "
                    "provider HTTPS destinations, including the regional "
                    "Bedrock Mantle API endpoint and every configured HTTP "
                    "provider"
                ),
            )
            if runtime_network_requires_prefix_list(self)
            else None
        )
        bedrock_invoke_resource_arns = CfnParameter(
            self,
            "BedrockInvokeResourceArns",
            type="CommaDelimitedList",
            allowed_pattern=(
                r"^arn:[a-z0-9-]+:bedrock:[a-z0-9-]+:"
                r"(?:[0-9]{12})?:(?:foundation-model|inference-profile|"
                r"application-inference-profile|custom-model|provisioned-model|"
                r"imported-model)/[A-Za-z0-9][A-Za-z0-9._:/+-]*$"
            ),
            constraint_description=(
                "each value must be a concrete Bedrock model or inference-profile "
                "ARN without wildcards"
            ),
            description=(
                "Comma-separated Bedrock model or inference-profile ARNs "
                "that AxonLLM may invoke. Cross-region inference profiles "
                "must include every concrete destination foundation-model ARN."
            ),
        )
        verified_image_uri = CfnParameter(
            self,
            "VerifiedImageUri",
            type="String",
            allowed_pattern=(
                rf"^[0-9]{{12}}\.dkr\.ecr\.{self.region}\.amazonaws\.com/"
                r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@"
                r"sha256:[0-9a-f]{64}$"
            ),
            constraint_description=(
                f"must be an immutable private ECR URI in {self.region} "
                "ending in @sha256:<64 lowercase hex characters>"
            ),
            description=(
                "Immutable ARM64 AgentCore image emitted by the release "
                "deployment verification gate"
            ),
        )
        initial_routing_config = CfnParameter(
            self,
            "InitialRoutingConfigZlibBase64",
            type="String",
            min_length=1,
            max_length=4096,
            allowed_pattern=r"^[A-Za-z0-9+/]+={0,2}$",
            constraint_description=(
                "must be canonical base64 containing the zlib-compressed "
                "initial routing configuration"
            ),
            description=(
                "Validated packaged routing defaults used only when the "
                "signed model registry has not been initialized"
            ),
        )
        enabled_providers = CfnParameter(
            self,
            "EnabledProviders",
            type="String",
            default=_AGENTCORE_ENABLED_PROVIDERS,
            min_length=1,
            max_length=512,
            allowed_pattern=_PROVIDER_NAME_PATTERN,
            constraint_description=(
                "must be a comma-separated list of supported provider names"
            ),
            description=(
                "Exact provider allowlist certified for this runtime version"
            ),
        )
        provider_secret_version = CfnParameter(
            self,
            "ProviderSecretVersion",
            type="String",
            default="bootstrap",
            min_length=1,
            max_length=256,
            allowed_pattern=r"^[A-Za-z0-9-]+$",
            constraint_description=(
                "must be a Secrets Manager version identifier or bootstrap"
            ),
            description=(
                "Provider secret version bound into this AgentCore runtime "
                "revision; changing it forces a fresh runtime version"
            ),
        )
        alarm_notification_email = CfnParameter(
            self,
            "AlarmNotificationEmail",
            type="String",
            min_length=3,
            max_length=320,
            allowed_pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            constraint_description="must be a valid notification email",
            description=(
                "Required confirmed destination for production alarms"
            ),
        )
        candidate_endpoint_name = CfnParameter(
            self,
            "CandidateEndpointName",
            type="String",
            min_length=42,
            max_length=42,
            allowed_pattern=r"^candidate_[0-9a-f]{32}$",
            constraint_description=(
                "must be a fresh high-entropy candidate endpoint name"
            ),
            description=(
                "Unpredictable endpoint qualifier generated for this "
                "certification deployment"
            ),
        )
        publish_candidate_endpoint = CfnParameter(
            self,
            "PublishCandidateEndpoint",
            type="String",
            default="true",
            allowed_values=["true", "false"],
            description=(
                "Publish the candidate endpoint for the current runtime "
                "version after provider credentials have been synchronized"
            ),
        )
        publish_production_endpoint = CfnParameter(
            self,
            "PublishProductionEndpoint",
            type="String",
            default="false",
            allowed_values=["true", "false"],
            description=(
                "Publish the production endpoint only for an explicitly "
                "certified runtime version"
            ),
        )
        production_runtime_version = CfnParameter(
            self,
            "ProductionRuntimeVersion",
            type="String",
            default="",
            max_length=32,
            allowed_pattern=r"^$|^[1-9][0-9]{0,31}$",
            constraint_description=(
                "must be empty or an exact positive AgentCore runtime version"
            ),
            description=(
                "Exact certified runtime version targeted by production"
            ),
        )
        image_account_id = Fn.select(
            0,
            Fn.split(".", verified_image_uri.value_as_string),
        )
        image_repository_name = Fn.select(
            0,
            Fn.split(
                "@",
                Fn.select(
                    1,
                    Fn.split(
                        ".amazonaws.com/",
                        verified_image_uri.value_as_string,
                    ),
                ),
            ),
        )
        verified_image_repository_arn = self.format_arn(
            service="ecr",
            region=self.region,
            account=image_account_id,
            resource="repository",
            resource_name=image_repository_name,
        )

        runtime_network = build_runtime_network(
            self,
            approved_https_prefix_list_id=(
                approved_https_prefix_list_id.value_as_string
                if approved_https_prefix_list_id is not None
                else None
            ),
            query_enabled=query_config.enabled,
        )

        if state_mode == "embedded":
            state = build_application_state_resources(
                self,
                deployment_namespace=deployment_namespace,
                backup_vault_name=self.node.try_get_context(
                    "application_state_backup_vault_name"
                ),
                security_event_topic_name=self.node.try_get_context(
                    "application_state_security_event_topic_name"
                ),
            )
            state_access = managed_application_state_access(
                stack_name=self.stack_name,
                resources=state,
            )
        else:
            primary_state_table_name_parameter = CfnParameter(
                self,
                "PrimaryStateTableName",
                type="String",
                min_length=3,
                max_length=255,
                allowed_pattern=r"^[A-Za-z0-9_.-]{3,255}$",
                constraint_description=(
                    "must be a valid DynamoDB table name"
                ),
                description=(
                    "Primary table from the reviewed application-state "
                    "descriptor"
                ),
            )
            runtime_state_table_name = CfnParameter(
                self,
                "RuntimeStateTableName",
                type="String",
                default="",
                min_length=0,
                max_length=255,
                allowed_pattern=r"^$|^[A-Za-z0-9_.-]{3,255}$",
                constraint_description=(
                    "must be blank or a valid DynamoDB table name; the "
                    "recovery guard enforces the restore namespace"
                ),
                description=(
                    "Optional restored table selected through the reviewed "
                    "AgentCore recovery workflow"
                ),
            )
            use_recovered_state = CfnCondition(
                self,
                "UseRecoveredState",
                expression=Fn.condition_not(
                    Fn.condition_equals(
                        runtime_state_table_name.value_as_string,
                        "",
                    )
                ),
            )
            primary_state_table_name = (
                primary_state_table_name_parameter.value_as_string
            )
            selected_state_table_name = Token.as_string(
                Fn.condition_if(
                    use_recovered_state.logical_id,
                    runtime_state_table_name.value_as_string,
                    primary_state_table_name,
                )
            )
            selected_state_table_arn = self.format_arn(
                service="dynamodb",
                resource="table",
                resource_name=selected_state_table_name,
            )
            state_access = external_agentcore_application_state_access(
                self,
                default_stack_name=state_stack_default,
                primary_state_table_name=primary_state_table_name,
                selected_state_table_name=selected_state_table_name,
                selected_state_table_arn=selected_state_table_arn,
            )

        primary_state_table_name = state_access.primary_state_table_name
        selected_state_table_name = state_access.selected_state_table_name
        selected_state_table_arn = state_access.selected_state_table_arn
        data_key = state_access.data_key
        routing_config_signing_key = (
            state_access.routing_config_signing_key
        )
        provider_secret = state_access.provider_secret
        event_dead_letter_queue = state_access.event_dead_letter_queue
        event_outbox_queue = state_access.event_outbox_queue
        security_event_topic = state_access.security_event_topic
        security_event_log_group = state_access.security_event_log_group
        security_event_log_group_arn = (
            state_access.security_event_log_group_arn
        )
        backup_vault_arn = state_access.backup_vault_arn
        backup_service_role_arn = state_access.backup_service_role_arn

        runtime_network.add_endpoint_policy(
            "secrets_endpoint",
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[provider_secret.secret_arn],
            )
        )
        recovery_cutover_mode = CfnParameter(
            self,
            "RecoveryCutoverMode",
            type="String",
            default="normal",
            allowed_values=[
                "normal",
                "quiesced",
                "selected",
                "validation",
            ],
            description=(
                "AgentCore recovery phase; table changes are accepted only "
                "from quiesced to selected"
            ),
        )
        recovery_approval_id = CfnParameter(
            self,
            "RecoveryApprovalId",
            type="String",
            default="",
            max_length=128,
            allowed_pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$",
            constraint_description=(
                "must be blank or a 3-128 character change/incident ID"
            ),
            description=(
                "Reviewed change or incident identifier bound to a recovery "
                "cutover and rollback"
            ),
        )
        production_endpoint_enabled = CfnCondition(
            self,
            "ProductionEndpointEnabled",
            expression=Fn.condition_and(
                Fn.condition_equals(
                    recovery_cutover_mode.value_as_string,
                    "normal",
                ),
                Fn.condition_equals(
                    publish_production_endpoint.value_as_string,
                    "true",
                ),
                Fn.condition_not(
                    Fn.condition_equals(
                        production_runtime_version.value_as_string,
                        "",
                    )
                ),
            ),
        )
        candidate_endpoint_enabled = CfnCondition(
            self,
            "CandidateEndpointEnabled",
            expression=Fn.condition_and(
                Fn.condition_equals(
                    recovery_cutover_mode.value_as_string,
                    "normal",
                ),
                Fn.condition_equals(
                    publish_candidate_endpoint.value_as_string,
                    "true",
                ),
            ),
        )
        recovery_quiesced = CfnCondition(
            self,
            "RecoveryQuiesced",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "quiesced",
            ),
        )
        recovery_selected = CfnCondition(
            self,
            "RecoverySelected",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "selected",
            ),
        )
        recovery_validation = CfnCondition(
            self,
            "RecoveryValidation",
            expression=Fn.condition_equals(
                recovery_cutover_mode.value_as_string,
                "validation",
            ),
        )
        recovery_access_blocked = CfnCondition(
            self,
            "RecoveryAccessBlocked",
            expression=Fn.condition_or(
                recovery_quiesced,
                recovery_selected,
            ),
        )
        runtime_network.add_endpoint_policy(
            "sqs_endpoint",
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "sqs:ChangeMessageVisibility",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                    "sqs:ReceiveMessage",
                    "sqs:SendMessage",
                ],
                resources=[event_outbox_queue.queue_arn],
            )
        )
        runtime_network.add_endpoint_policy(
            "sns_endpoint",
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[security_event_topic.topic_arn],
            )
        )
        runtime_network.add_endpoint_policy(
            "logs_endpoint",
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    security_event_log_group_arn,
                    f"{security_event_log_group_arn}:*",
                ],
            )
        )
        runtime_network.add_endpoint_policy(
            "dynamodb_endpoint",
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "dynamodb:BatchGetItem",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:ConditionCheckItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:TransactWriteItems",
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        runtime_network.add_endpoint_policy(
            "bedrock_endpoint",
            iam.PolicyStatement(
                principals=[iam.AnyPrincipal()],
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=bedrock_invoke_resource_arns.value_as_list,
            )
        )

        application_logs = logs.LogGroup(
            self,
            "ApplicationLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        usage_logs = logs.LogGroup(
            self,
            "UsageLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )

        runtime_execution_role = iam.Role(
            self,
            "RuntimeExecutionRole",
            role_name=Fn.join(
                "-",
                [
                    "axonllm-agentcore-runtime",
                    *(
                        [deployment_namespace]
                        if deployment_namespace
                        else []
                    ),
                    self.region,
                ],
            ),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{self.partition}:bedrock-agentcore:"
                            f"{self.region}:{self.account}:"
                            f"runtime/{runtime_name}*"
                        )
                    },
                },
            ),
            description="Execution role for Bedrock Agent Core Runtime",
            max_session_duration=Duration.hours(8),
        )
        runtime_artifact = agentcore.AgentRuntimeArtifact.from_image_uri(
            verified_image_uri.value_as_string
        )
        blocked_authorizer_value = Fn.join(
            ":",
            [
                "axonllm-recovery-blocked",
                Fn.select(2, Fn.split("/", self.stack_id)),
            ],
        )
        selected_oidc_client_ids = Token.as_list(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                [blocked_authorizer_value],
                oidc_client_ids.value_as_list,
            )
        )
        selected_oidc_audiences = Token.as_list(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                [blocked_authorizer_value],
                oidc_audiences.value_as_list,
            )
        )
        runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name=runtime_name,
            description="Tenant-isolated AxonLLM production runtime",
            agent_runtime_artifact=runtime_artifact,
            execution_role=runtime_execution_role,
            authorizer_configuration=(
                agentcore.RuntimeAuthorizerConfiguration.using_jwt(
                    oidc_discovery_url.value_as_string,
                    selected_oidc_client_ids,
                    selected_oidc_audiences,
                )
            ),
            environment_variables={
                "AWS_DEFAULT_REGION": self.region,
                "AXON_AWS_ACCOUNT_ID": self.account,
                "AXON_BEDROCK_REGION": self.region,
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                "AXON_DYNAMODB_TABLE": selected_state_table_name,
                "AXON_ROUTING_CONFIG_SIGNING_MODE": "verify",
                "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN": (
                    routing_config_signing_key.key_arn
                ),
                "AXON_EVENT_OUTBOX_QUEUE_URL": event_outbox_queue.queue_url,
                "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (
                    security_event_topic.topic_arn
                ),
                "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (
                    security_event_log_group_arn
                ),
                "AXON_AUTH_MODE": "ENFORCE",
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_LOAD_DEMO_DATA": "false",
                "AXON_OIDC_ISSUER": oidc_issuer.value_as_string,
                "AXON_OIDC_AUDIENCE": Fn.join(
                    ",",
                    oidc_audiences.value_as_list,
                ),
                "AXON_OIDC_TENANT_CLAIM": (
                    oidc_tenant_claim.value_as_string
                ),
                "AXON_OIDC_PROJECT_CLAIM": (
                    oidc_project_claim.value_as_string
                ),
                "AXON_REQUIRE_CANONICAL_IDENTITY": "true",
                "AXON_ENABLED_PROVIDERS": (
                    enabled_providers.value_as_string
                ),
                "AXON_PROVIDER_SECRET_ARN": provider_secret.secret_arn,
                "AXON_PROVIDER_SECRET_VERSION": (
                    provider_secret_version.value_as_string
                ),
                **(
                    {
                        "AXON_LAUNCH_REHEARSAL_TABLE": (
                            rehearsal_control_table_arn.value_as_string
                        ),
                        "AXON_LAUNCH_REHEARSAL_ALLOW_PROCESS_EXIT": "true",
                    }
                    if rehearsal_control_table_arn is not None
                    else {}
                ),
                **query_config.environment(),
            },
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(10),
                max_lifetime=Duration.hours(4),
            ),
            logging_configs=[
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.APPLICATION_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(
                        application_logs
                    ),
                ),
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.USAGE_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(
                        usage_logs
                    ),
                ),
            ],
            network_configuration=runtime_network.configuration,
            protocol_configuration=agentcore.ProtocolType.HTTP,
            request_header_configuration=(
                agentcore.RequestHeaderConfiguration(
                    allowlisted_headers=["Authorization"]
                )
            ),
            tracing_enabled=True,
            tags={
                "Application": "AxonLLM",
                "Environment": "production",
            },
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[verified_image_repository_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSelectedStateTable",
                actions=_RUNTIME_DYNAMODB_STANDARD_ACTIONS,
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="VerifyRoutingConfiguration",
                actions=["kms:Verify"],
                resources=[routing_config_signing_key.key_arn],
            )
        )
        runtime_network.add_endpoint_policy(
            "kms_endpoint",
            iam.PolicyStatement(
                principals=[runtime.role],
                actions=["kms:Verify"],
                resources=[routing_config_signing_key.key_arn],
            )
        )
        if rehearsal_control_table_arn is not None:
            runtime.add_to_role_policy(
                iam.PolicyStatement(
                    sid="UseLaunchRehearsalControlLedger",
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                    ],
                    resources=[
                        rehearsal_control_table_arn.value_as_string
                    ],
                )
            )
            runtime_network.add_endpoint_policy(
                "dynamodb_endpoint",
                iam.PolicyStatement(
                    principals=[runtime.role],
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                    ],
                    resources=[
                        rehearsal_control_table_arn.value_as_string
                    ],
                )
            )
        transaction_policy = iam.Policy(
            self,
            "RuntimeDynamoTransactionPolicy",
            statements=[
                iam.PolicyStatement(
                    sid="TransactWithSelectedStateTable",
                    actions=_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[
                        selected_state_table_arn,
                        f"{selected_state_table_arn}/index/*",
                    ],
                )
            ],
        )
        transaction_policy.attach_to_role(runtime.role)
        cfn_transaction_policy = transaction_policy.node.default_child
        if not isinstance(cfn_transaction_policy, iam.CfnPolicy):
            raise TypeError("runtime transaction policy has no CfnPolicy child")
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_transaction_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadProviderCredentials",
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                ],
                resources=[provider_secret.secret_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="DecryptProviderCredentials",
                actions=["kms:Decrypt"],
                resources=[data_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (
                            f"secretsmanager.{self.region}.{self.url_suffix}"
                        ),
                    }
                },
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventOutbox",
                actions=_RUNTIME_SQS_ACTIONS,
                resources=[event_outbox_queue.queue_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventOutboxKey",
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=[data_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (
                            f"sqs.{self.region}.{self.url_suffix}"
                        ),
                    }
                },
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="PublishSecurityEvents",
                actions=["sns:Publish"],
                resources=[security_event_topic.topic_arn],
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventTopicKey",
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=[data_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (
                            f"sns.{self.region}.{self.url_suffix}"
                        ),
                        "kms:EncryptionContext:aws:sns:topicArn": (
                            security_event_topic.topic_arn
                        ),
                    }
                },
            )
        )
        security_event_log_group.grant_write(runtime.role)
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=bedrock_invoke_resource_arns.value_as_list,
            )
        )
        runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-mantle:CreateInference",
                    "bedrock-mantle:ListModels",
                ],
                resources=["*"],
            )
        )
        if query_config.enabled:
            runtime.add_to_role_policy(
                iam.PolicyStatement(
                    actions=ATHENA_ASSUME_ROLE_ACTIONS,
                    resources=list(query_config.role_arns),
                )
            )
            runtime_network.add_endpoint_policy(
                "sts_endpoint",
                iam.PolicyStatement(
                    principals=[runtime.role],
                    actions=ATHENA_ASSUME_ROLE_ACTIONS,
                    resources=list(query_config.role_arns),
                )
            )
            runtime_network.add_endpoint_policy(
                "athena_endpoint",
                iam.PolicyStatement(
                    principals=[
                        iam.ArnPrincipal(role_arn)
                        for role_arn in query_config.role_arns
                    ],
                    actions=ATHENA_QUERY_ACTIONS,
                    resources=["*"],
                )
            )
        recovery_deny_resource = Token.as_string(
            Fn.condition_if(
                recovery_access_blocked.logical_id,
                "*",
                self.format_arn(
                    service="dynamodb",
                    resource="table",
                    resource_name=(
                        "__axonllm_recovery_access_not_blocked__"
                    ),
                ),
            )
        )
        recovery_deny_policy = iam.Policy(
            self,
            "RecoveryStateAccessDeny",
            statements=[
                iam.PolicyStatement(
                    sid="BlockStateAccessDuringRecoveryTransition",
                    effect=iam.Effect.DENY,
                    actions=_RUNTIME_DYNAMODB_STANDARD_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_deny_policy.attach_to_role(runtime.role)
        cfn_recovery_deny_policy = (
            recovery_deny_policy.node.default_child
        )
        if not isinstance(cfn_recovery_deny_policy, iam.CfnPolicy):
            raise TypeError("recovery deny policy has no CfnPolicy child")
        recovery_transaction_deny_policy = iam.Policy(
            self,
            "RecoveryStateTransactionAccessDeny",
            statements=[
                iam.PolicyStatement(
                    sid="BlockStateTransactionsDuringRecoveryTransition",
                    effect=iam.Effect.DENY,
                    actions=_RUNTIME_DYNAMODB_TRANSACTION_ACTIONS,
                    resources=[recovery_deny_resource],
                )
            ],
        )
        recovery_transaction_deny_policy.attach_to_role(runtime.role)
        cfn_recovery_transaction_deny_policy = (
            recovery_transaction_deny_policy.node.default_child
        )
        if not isinstance(
            cfn_recovery_transaction_deny_policy,
            iam.CfnPolicy,
        ):
            raise TypeError(
                "recovery transaction deny policy has no CfnPolicy child"
            )
        # cfn-lint 1.52.1 omits this valid DynamoDB IAM action.
        cfn_recovery_transaction_deny_policy.add_metadata(
            "cfn-lint",
            {"config": {"ignore_checks": ["W3037"]}},
        )

        recovery_guard_handler_logs = logs.LogGroup(
            self,
            "RecoveryGuardHandlerLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        recovery_guard_handler = lambda_.Function(
            self,
            "RecoveryGuardHandler",
            description=(
                "Blocks unsafe AgentCore DynamoDB recovery transitions"
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_AGENTCORE_RECOVERY_GUARD),
            timeout=Duration.seconds(60),
            log_group=recovery_guard_handler_logs,
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:ListAgentRuntimeEndpoints",
                    "bedrock-agentcore:ListAgentRuntimes",
                ],
                resources=["*"],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=(
                            f"{control_plane_stack_name}/*"
                        ),
                    )
                ],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices"],
                resources=[
                    self.format_arn(
                        service="ecs",
                        resource="service",
                        resource_name="*/*",
                    )
                ],
            )
        )
        recovery_guard_handler.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "application-autoscaling:DescribeScalableTargets"
                ],
                resources=["*"],
            )
        )
        recovery_guard_provider_logs = logs.LogGroup(
            self,
            "RecoveryGuardProviderLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        recovery_guard_provider = cr.Provider(
            self,
            "RecoveryGuardProvider",
            on_event_handler=recovery_guard_handler,
            log_group=recovery_guard_provider_logs,
        )
        recovery_guard = CustomResource(
            self,
            "RecoveryGuard",
            service_token=recovery_guard_provider.service_token,
            properties={
                "AgentCoreStackName": self.stack_name,
                "ApprovalId": recovery_approval_id.value_as_string,
                "ControlPlaneStackName": control_plane_stack_name,
                "MinimumQuiescenceSeconds": str(
                    _RECOVERY_MIN_QUIESCENCE_SECONDS
                ),
                "Mode": recovery_cutover_mode.value_as_string,
                "PrimaryTable": primary_state_table_name,
                "RuntimeName": runtime_name,
                "SelectedTable": selected_state_table_name,
            },
        )
        recovery_guard_resource = recovery_guard.node.default_child
        if not isinstance(recovery_guard_resource, CfnResource):
            raise TypeError("recovery guard has no CloudFormation child")
        recovery_guard_resource.add_dependency(
            cfn_recovery_deny_policy
        )
        recovery_guard_resource.add_dependency(
            cfn_recovery_transaction_deny_policy
        )
        routing_seed_network_options = (
            runtime_network.routing_seeder_lambda_options(self)
        )
        routing_seed_handler_logs = logs.LogGroup(
            self,
            "RoutingConfigSeederHandlerLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        routing_seed_handler = lambda_.Function(
            self,
            "RoutingConfigSeederHandler",
            description=(
                "Seeds or migrates the KMS-signed routing configuration"
            ),
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_ROUTING_CONFIG_SEEDER),
            timeout=Duration.seconds(60),
            memory_size=256,
            log_group=routing_seed_handler_logs,
            **routing_seed_network_options,
        )
        routing_seed_handler.add_to_role_policy(
            iam.PolicyStatement(
                sid="SeedRoutingConfiguration",
                actions=["dynamodb:GetItem", "dynamodb:PutItem"],
                resources=[selected_state_table_arn],
            )
        )
        routing_seed_handler.add_to_role_policy(
            iam.PolicyStatement(
                sid="SignAndVerifyRoutingConfiguration",
                actions=["kms:Sign", "kms:Verify"],
                resources=[routing_config_signing_key.key_arn],
            )
        )
        runtime_network.add_endpoint_policy(
            "kms_endpoint",
            iam.PolicyStatement(
                principals=[routing_seed_handler.role],
                actions=["kms:Sign", "kms:Verify"],
                resources=[routing_config_signing_key.key_arn],
            )
        )
        routing_seed_provider_logs = logs.LogGroup(
            self,
            "RoutingConfigSeederProviderLogs",
            encryption_key=data_key,
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        routing_seed_provider = cr.Provider(
            self,
            "RoutingConfigSeederProvider",
            on_event_handler=routing_seed_handler,
            log_group=routing_seed_provider_logs,
        )
        routing_seed = CustomResource(
            self,
            "RoutingConfigSeeder",
            service_token=routing_seed_provider.service_token,
            properties={
                "DeploymentToken": (
                    candidate_endpoint_name.value_as_string
                ),
                "InitialRoutingConfigZlibBase64": (
                    initial_routing_config.value_as_string
                ),
                "KeyArn": routing_config_signing_key.key_arn,
                "TableName": selected_state_table_name,
            },
        )
        routing_seed_resource = routing_seed.node.default_child
        if not isinstance(routing_seed_resource, CfnResource):
            raise TypeError(
                "routing configuration seeder has no CloudFormation child"
            )
        routing_seed_resource.add_dependency(recovery_guard_resource)
        runtime_network.add_routing_seeder_dependencies(
            routing_seed_resource
        )
        cfn_runtime = runtime.node.default_child
        if not isinstance(cfn_runtime, agentcore.CfnRuntime):
            raise TypeError("AgentCore runtime has no CfnRuntime child")
        cfn_runtime.add_dependency(recovery_guard_resource)
        cfn_runtime.add_dependency(routing_seed_resource)

        production_endpoint = runtime.add_endpoint(
            "production",
            description="AxonLLM production endpoint",
            version=production_runtime_version.value_as_string,
        )
        candidate_endpoint = agentcore.RuntimeEndpoint(
            self,
            "CandidateRuntimeEndpoint",
            agent_runtime_id=runtime.agent_runtime_id,
            endpoint_name=candidate_endpoint_name.value_as_string,
            description="AxonLLM pre-production certification endpoint",
            agent_runtime_version=runtime.agent_runtime_version,
        )
        recovery_endpoint = runtime.add_endpoint(
            "recovery",
            description="AxonLLM recovery-validation endpoint",
            version=runtime.agent_runtime_version,
        )
        cfn_production_endpoint = production_endpoint.node.default_child
        cfn_candidate_endpoint = candidate_endpoint.node.default_child
        cfn_recovery_endpoint = recovery_endpoint.node.default_child
        if not isinstance(
            cfn_production_endpoint,
            agentcore.CfnRuntimeEndpoint,
        ) or not isinstance(
            cfn_candidate_endpoint,
            agentcore.CfnRuntimeEndpoint,
        ) or not isinstance(
            cfn_recovery_endpoint,
            agentcore.CfnRuntimeEndpoint,
        ):
            raise TypeError(
                "AgentCore endpoint has no CfnRuntimeEndpoint child"
            )
        cfn_production_endpoint.cfn_options.condition = (
            production_endpoint_enabled
        )
        cfn_candidate_endpoint.cfn_options.condition = (
            candidate_endpoint_enabled
        )
        cfn_recovery_endpoint.cfn_options.condition = recovery_validation
        cfn_production_endpoint.add_dependency(recovery_guard_resource)
        cfn_candidate_endpoint.add_dependency(recovery_guard_resource)
        cfn_recovery_endpoint.add_dependency(recovery_guard_resource)

        alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            display_name="AxonLLM AgentCore production alarms",
            master_key=data_key,
        )
        alarm_topic.add_subscription(
            sns_subscriptions.EmailSubscription(
                alarm_notification_email.value_as_string,
            )
        )
        alarm_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowAccountCloudWatchAlarms",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[alarm_topic.topic_arn],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{self.partition}:cloudwatch:{self.region}:"
                            f"{self.account}:alarm:*"
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            )
        )
        system_errors = runtime.metric_system_errors(
            period=Duration.minutes(5),
            statistic="Sum",
        )
        throttles = runtime.metric_throttles(
            period=Duration.minutes(5),
            statistic="Sum",
        )
        latency = runtime.metric_latency(
            period=Duration.minutes(5),
            statistic="p95",
        )
        state_operations = [
            "GetItem",
            "Query",
            "Scan",
            "PutItem",
            "UpdateItem",
            "DeleteItem",
            "TransactGetItems",
            "TransactWriteItems",
        ]
        throttle_metrics = {
            operation.lower(): cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name="ThrottledRequests",
                dimensions_map={
                    "Operation": operation,
                    "TableName": selected_state_table_name,
                },
                period=Duration.minutes(5),
                statistic="Sum",
            )
            for operation in state_operations
        }
        dynamodb_throttles = cloudwatch.MathExpression(
            expression=" + ".join(throttle_metrics),
            using_metrics=throttle_metrics,
            period=Duration.minutes(5),
            label="Sum of throttled requests across all operations",
        )
        alarms = [
            cloudwatch.Alarm(
                self,
                "RuntimeSystemErrorsAlarm",
                metric=system_errors,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AgentCore reported AxonLLM system errors",
            ),
            cloudwatch.Alarm(
                self,
                "RuntimeThrottlesAlarm",
                metric=throttles,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AgentCore throttled AxonLLM invocations",
            ),
            cloudwatch.Alarm(
                self,
                "DynamoDbThrottlesAlarm",
                metric=dynamodb_throttles,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description="AgentCore state requests were throttled",
            ),
            cloudwatch.Alarm(
                self,
                "SecurityEventDeadLettersAlarm",
                metric=(
                    event_dead_letter_queue
                    .metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(1),
                        statistic="Maximum",
                    )
                ),
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=(
                    cloudwatch.TreatMissingData.NOT_BREACHING
                ),
                alarm_description=(
                    "A security event exhausted delivery retries"
                ),
            ),
        ]
        security_event_dead_letters_alarm = alarms[-1]
        alarm_action = cloudwatch_actions.SnsAction(alarm_topic)
        for alarm in alarms:
            alarm.add_alarm_action(alarm_action)
            alarm.add_ok_action(alarm_action)

        dashboard = cloudwatch.Dashboard(
            self,
            "OperationsDashboard",
            dashboard_name=(
                f"AxonLLM-AgentCore-Production{physical_suffix}"
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="AgentCore invocations",
                left=[
                    runtime.metric_invocations(
                        period=Duration.minutes(5),
                        statistic="Sum",
                    )
                ],
                right=[system_errors, throttles],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="AgentCore latency",
                left=[latency],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="DynamoDB throttles",
                left=[dynamodb_throttles],
                width=12,
            ),
        )

        CfnOutput(
            self,
            "RuntimeArn",
            value=runtime.agent_runtime_arn,
        )
        CfnOutput(
            self,
            "RuntimeVersion",
            value=runtime.agent_runtime_version,
        )
        runtime_endpoint_name_output = CfnOutput(
            self,
            "RuntimeEndpointName",
            value="production",
        )
        runtime_endpoint_name_output.condition = production_endpoint_enabled
        candidate_endpoint_name_output = CfnOutput(
            self,
            "CandidateRuntimeEndpointName",
            value=candidate_endpoint_name.value_as_string,
        )
        candidate_endpoint_name_output.condition = candidate_endpoint_enabled
        enabled_providers_output = CfnOutput(
            self,
            "EnabledProvidersOutput",
            value=enabled_providers.value_as_string,
        )
        enabled_providers_output.override_logical_id("EnabledProviders")
        provider_secret_version_output = CfnOutput(
            self,
            "ProviderSecretVersionOutput",
            value=provider_secret_version.value_as_string,
        )
        provider_secret_version_output.override_logical_id(
            "ProviderSecretVersion"
        )
        if approved_https_prefix_list_id is not None:
            approved_https_prefix_list_output = CfnOutput(
                self,
                "ApprovedHttpsPrefixListIdOutput",
                value=approved_https_prefix_list_id.value_as_string,
            )
            approved_https_prefix_list_output.override_logical_id(
                "ApprovedHttpsPrefixListId"
            )
        bedrock_invoke_resource_arns_output = CfnOutput(
            self,
            "BedrockInvokeResourceArnsOutput",
            value=Fn.join(
                ",",
                bedrock_invoke_resource_arns.value_as_list,
            ),
        )
        bedrock_invoke_resource_arns_output.override_logical_id(
            "BedrockInvokeResourceArns"
        )
        CfnOutput(
            self,
            "AthenaConfigurationFingerprint",
            value=query_config.fingerprint(),
        )
        alarm_notification_email_output = CfnOutput(
            self,
            "AlarmNotificationEmailOutput",
            value=alarm_notification_email.value_as_string,
        )
        alarm_notification_email_output.override_logical_id(
            "AlarmNotificationEmail"
        )
        CfnOutput(
            self,
            "RuntimeExecutionRoleArn",
            value=runtime.role.role_arn,
            description=(
                "Exact principal that approved Athena datasource roles "
                "must trust"
            ),
        )
        runtime_endpoint_output = CfnOutput(
            self,
            "RuntimeEndpointArn",
            value=production_endpoint.agent_runtime_endpoint_arn,
        )
        runtime_endpoint_output.condition = production_endpoint_enabled
        production_runtime_version_output = CfnOutput(
            self,
            "ProductionRuntimeVersionOutput",
            value=production_runtime_version.value_as_string,
        )
        production_runtime_version_output.override_logical_id(
            "ProductionRuntimeVersion"
        )
        production_runtime_version_output.condition = (
            production_endpoint_enabled
        )
        candidate_runtime_version_output = CfnOutput(
            self,
            "CandidateRuntimeVersion",
            value=runtime.agent_runtime_version,
        )
        candidate_runtime_version_output.condition = (
            candidate_endpoint_enabled
        )
        candidate_endpoint_output = CfnOutput(
            self,
            "CandidateRuntimeEndpointArn",
            value=candidate_endpoint.agent_runtime_endpoint_arn,
        )
        candidate_endpoint_output.condition = candidate_endpoint_enabled
        recovery_endpoint_output = CfnOutput(
            self,
            "RecoveryRuntimeEndpointArn",
            value=recovery_endpoint.agent_runtime_endpoint_arn,
        )
        recovery_endpoint_output.condition = recovery_validation
        CfnOutput(
            self,
            "StateTableName",
            value=primary_state_table_name,
            export_name=(
                Fn.join(
                    ":",
                    [self.stack_name, "StateTableName"],
                )
                if state_mode == "embedded"
                else None
            ),
        )
        CfnOutput(
            self,
            "AgentCoreStackName",
            value=self.stack_name,
        )
        CfnOutput(
            self,
            "SelectedRuntimeStateTableName",
            value=selected_state_table_name,
        )
        recovery_mode_output = CfnOutput(
            self,
            "RecoveryCutoverModeOutput",
            value=recovery_cutover_mode.value_as_string,
        )
        recovery_mode_output.override_logical_id("RecoveryCutoverMode")
        recovery_approval_output = CfnOutput(
            self,
            "RecoveryApprovalIdOutput",
            value=recovery_approval_id.value_as_string,
        )
        recovery_approval_output.override_logical_id("RecoveryApprovalId")
        CfnOutput(
            self,
            "RecoveryQuiescedAt",
            value=recovery_guard.get_att_string("QuiescedAt"),
        )
        CfnOutput(
            self,
            "RecoveryMinimumQuiescenceSeconds",
            value=str(_RECOVERY_MIN_QUIESCENCE_SECONDS),
        )
        CfnOutput(
            self,
            "DataKeyArn",
            value=data_key.key_arn,
            export_name=(
                Fn.join(
                    ":",
                    [self.stack_name, "DataKeyArn"],
                )
                if state_mode == "embedded"
                else None
            ),
        )
        CfnOutput(
            self,
            "RoutingConfigSigningKeyArn",
            value=routing_config_signing_key.key_arn,
            export_name=(
                Fn.join(
                    ":",
                    [self.stack_name, "RoutingConfigSigningKeyArn"],
                )
                if state_mode == "embedded"
                else None
            ),
        )
        CfnOutput(
            self,
            "SecurityEventOutboxQueueUrl",
            value=event_outbox_queue.queue_url,
            export_name=(
                Fn.join(
                    ":",
                    [self.stack_name, "SecurityEventOutboxQueueUrl"],
                )
                if state_mode == "embedded"
                else None
            ),
        )
        CfnOutput(
            self,
            "SecurityEventOutboxQueueArn",
            value=event_outbox_queue.queue_arn,
            export_name=(
                Fn.join(
                    ":",
                    [self.stack_name, "SecurityEventOutboxQueueArn"],
                )
                if state_mode == "embedded"
                else None
            ),
        )
        CfnOutput(
            self,
            "SecurityEventDeadLetterQueueUrl",
            value=event_dead_letter_queue.queue_url,
        )
        CfnOutput(
            self,
            "SecurityEventDeadLetterQueueArn",
            value=event_dead_letter_queue.queue_arn,
        )
        CfnOutput(
            self,
            "SecurityEventDeadLettersAlarmArn",
            value=security_event_dead_letters_alarm.alarm_arn,
        )
        CfnOutput(
            self,
            "SecurityEventTopicArn",
            value=security_event_topic.topic_arn,
            export_name=(
                Fn.join(
                    ":",
                    [self.stack_name, "SecurityEventTopicArn"],
                )
                if state_mode == "embedded"
                else None
            ),
        )
        CfnOutput(
            self,
            "SecurityEventLogGroupArn",
            value=security_event_log_group_arn,
            export_name=(
                Fn.join(
                    ":",
                    [self.stack_name, "SecurityEventLogGroupArn"],
                )
                if state_mode == "embedded"
                else None
            ),
        )
        CfnOutput(
            self,
            "ProviderSecretArn",
            value=provider_secret.secret_arn,
        )
        CfnOutput(
            self,
            "StateBackupVaultArn",
            value=backup_vault_arn,
        )
        CfnOutput(
            self,
            "StateBackupRoleArn",
            value=backup_service_role_arn,
        )
        if state_mode == "external":
            application_state_stack_output = CfnOutput(
                self,
                "ApplicationStateStackNameOutput",
                value=state_access.stack_name,
            )
            application_state_stack_output.override_logical_id(
                "ApplicationStateStackName"
            )
        CfnOutput(
            self,
            "AlarmTopicArn",
            value=alarm_topic.topic_arn,
        )
        CfnOutput(
            self,
            "OperationsDashboardName",
            value=dashboard.dashboard_name,
        )
        CfnOutput(
            self,
            "RuntimeImageUri",
            value=verified_image_uri.value_as_string,
        )
