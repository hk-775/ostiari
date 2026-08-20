"""Bounded CloudFormation selector mutations for managed qualification stacks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


EVENT_SCHEMA = "axonllm.qualification-selector-mutation"
EVENT_VERSION = 1
AUTHORIZATION_SCHEMA = "axonllm.qualification-selector-authorization"
AUTHORIZATION_VERSION = 1

AUTHORIZATION_TABLE_ENV = "AXON_QUALIFICATION_MUTATION_AUTHORIZATION_TABLE"
PRIMARY_TABLE_NAME_ENV = "AXON_QUALIFICATION_PRIMARY_TABLE_NAME"
EXECUTION_ROLE_ARN_ENV = "AXON_QUALIFICATION_CLOUDFORMATION_EXECUTION_ROLE_ARN"

MANAGED_AGENTCORE_STACK_NAME = "AxonLLMAgentCoreStack-managed"
MANAGED_CONTROL_PLANE_STACK_NAME = "AxonLLMControlPlaneStack-managed"
MANAGED_PRIMARY_TABLE_NAME = "axonllm-agentcore-state-managed"

STACK_KINDS = frozenset({"agentcore", "control-plane"})
LEGAL_EDGES = frozenset(
    {
        "quiesce-primary",
        "quiesce-restored",
        "cutover-to-primary",
        "cutover-to-restored",
        "resume-primary",
        "resume-restored",
    }
)

_EVENT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "authorizationId",
        "ownerId",
        "fenceToken",
        "stackKind",
        "legalEdge",
    }
)
_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema",
        "version",
        "authorizationId",
        "ownerId",
        "fenceToken",
        "stackKind",
        "legalEdge",
        "status",
        "expiresAtEpoch",
        "stackArn",
        "primaryTableName",
        "restoredTableName",
        "approvalId",
        "executionRoleArn",
    }
)
_RECOVERY_PARAMETERS = frozenset(
    {
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "RuntimeStateTableName",
    }
)
_STABLE_STACK_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})
_UPDATING_STACK_STATUSES = frozenset(
    {
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_IN_PROGRESS",
    }
)
_AGENTCORE_MODES = frozenset({"normal", "quiesced", "selected", "validation"})
_CONTROL_PLANE_MODES = frozenset({"normal", "quiesced", "selected"})

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", re.ASCII)
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$", re.ASCII)
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$", re.ASCII)
_DDB_INTEGER = re.compile(r"^(?:0|[1-9][0-9]{0,18})$", re.ASCII)
_STACK_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov|-cn)?):cloudformation:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):stack/"
    r"(?P<name>[A-Za-z][A-Za-z0-9-]{0,127})/"
    r"(?P<id>[A-Za-z0-9-]{8,64})$",
    re.ASCII,
)
_ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov|-cn)?):iam::"
    r"(?P<account>[0-9]{12}):role/"
    r"(?P<name>[A-Za-z0-9+=,.@_/-]{1,128})$",
    re.ASCII,
)
_TABLE_ARN = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov|-cn)?):dynamodb:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):table/"
    r"(?P<name>[A-Za-z0-9_.-]{3,255})$",
    re.ASCII,
)


class QualificationMutationError(RuntimeError):
    """A fail-closed qualification mutation rejection."""


@dataclass(frozen=True)
class MutationEvent:
    authorization_id: str
    owner_id: str
    fence_token: int
    stack_kind: str
    legal_edge: str


@dataclass(frozen=True)
class BrokerConfig:
    authorization_table: str
    primary_table_name: str
    execution_role_arn: str
    partition: str
    region: str
    account: str


@dataclass(frozen=True)
class Authorization:
    authorization_id: str
    owner_id: str
    fence_token: int
    stack_kind: str
    legal_edge: str
    expires_at_epoch: int
    stack_arn: str
    primary_table_name: str
    restored_table_name: str
    approval_id: str
    execution_role_arn: str


@dataclass(frozen=True)
class StackState:
    status: str
    mode: str
    selected_table_name: str
    approval_id: str
    parameter_names: tuple[str, ...]


@dataclass(frozen=True)
class TransitionStep:
    complete: bool
    next_mode: str | None = None
    next_table_name: str | None = None


def _strict_object(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise QualificationMutationError(f"{label} schema is invalid")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise QualificationMutationError(f"{label} is invalid")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise QualificationMutationError(f"{label} is invalid")
    return value


def _parse_event(raw_event: Any) -> MutationEvent:
    event = _strict_object(raw_event, _EVENT_FIELDS, "event")
    if event["schema"] != EVENT_SCHEMA or type(event["version"]) is not int or event["version"] != EVENT_VERSION:
        raise QualificationMutationError("event schema is invalid")
    stack_kind = event["stackKind"]
    legal_edge = event["legalEdge"]
    if (
        not isinstance(stack_kind, str)
        or stack_kind not in STACK_KINDS
        or not isinstance(legal_edge, str)
        or legal_edge not in LEGAL_EDGES
    ):
        raise QualificationMutationError("event transition is invalid")
    return MutationEvent(
        authorization_id=_safe_id(event["authorizationId"], "authorization ID"),
        owner_id=_safe_id(event["ownerId"], "owner ID"),
        fence_token=_positive_integer(event["fenceToken"], "fence token"),
        stack_kind=stack_kind,
        legal_edge=legal_edge,
    )


def _required_environment_value(
    environ: Mapping[str, str],
    name: str,
) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        raise QualificationMutationError(f"{name} is not configured")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise QualificationMutationError(f"{name} is invalid") from exc
    return value


def _load_config(environ: Mapping[str, str]) -> BrokerConfig:
    authorization_table = _required_environment_value(
        environ,
        AUTHORIZATION_TABLE_ENV,
    )
    primary_table_name = _required_environment_value(
        environ,
        PRIMARY_TABLE_NAME_ENV,
    )
    execution_role_arn = _required_environment_value(
        environ,
        EXECUTION_ROLE_ARN_ENV,
    )

    table_match = _TABLE_ARN.fullmatch(authorization_table)
    role_match = _ROLE_ARN.fullmatch(execution_role_arn)
    if table_match is None or role_match is None:
        raise QualificationMutationError("qualification infrastructure configuration is invalid")
    partition = table_match.group("partition")
    region = table_match.group("region")
    account = table_match.group("account")
    if (
        role_match.group("partition") != partition
        or role_match.group("account") != account
        or role_match.group("name") != f"cdk-axqual-cfn-exec-role-{account}-{region}"
    ):
        raise QualificationMutationError("qualification execution role is invalid")
    if primary_table_name != MANAGED_PRIMARY_TABLE_NAME:
        raise QualificationMutationError("qualification table configuration is invalid")

    return BrokerConfig(
        authorization_table=authorization_table,
        primary_table_name=primary_table_name,
        execution_role_arn=execution_role_arn,
        partition=partition,
        region=region,
        account=account,
    )


def _ddb_string(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if type(value) is not dict or set(value) != {"S"} or not isinstance(value["S"], str):
        raise QualificationMutationError("authorization record is malformed")
    return value["S"]


def _ddb_integer(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    if type(value) is not dict or set(value) != {"N"}:
        raise QualificationMutationError("authorization record is malformed")
    raw = value["N"]
    if not isinstance(raw, str) or _DDB_INTEGER.fullmatch(raw) is None:
        raise QualificationMutationError("authorization record is malformed")
    parsed = int(raw)
    if parsed > 2**63 - 1:
        raise QualificationMutationError("authorization record is malformed")
    return parsed


def _read_authorization(
    dynamodb_client: Any,
    *,
    event: MutationEvent,
    config: BrokerConfig,
    now_epoch: float,
) -> Authorization:
    try:
        response = dynamodb_client.get_item(
            TableName=config.authorization_table,
            Key={"authorizationId": {"S": event.authorization_id}},
            ConsistentRead=True,
        )
    except Exception as exc:
        raise QualificationMutationError("authorization record could not be read") from exc
    if not isinstance(response, Mapping):
        raise QualificationMutationError("authorization response is malformed")
    item = response.get("Item")
    if type(item) is not dict or set(item) != _AUTHORIZATION_FIELDS:
        raise QualificationMutationError("authorization record is missing or malformed")

    schema = _ddb_string(item, "schema")
    version = _ddb_integer(item, "version")
    authorization_id = _ddb_string(item, "authorizationId")
    owner_id = _ddb_string(item, "ownerId")
    fence_token = _ddb_integer(item, "fenceToken")
    stack_kind = _ddb_string(item, "stackKind")
    legal_edge = _ddb_string(item, "legalEdge")
    status = _ddb_string(item, "status")
    expires_at_epoch = _ddb_integer(item, "expiresAtEpoch")
    stack_arn = _ddb_string(item, "stackArn")
    primary_table_name = _ddb_string(item, "primaryTableName")
    restored_table_name = _ddb_string(item, "restoredTableName")
    approval_id = _ddb_string(item, "approvalId")
    execution_role_arn = _ddb_string(item, "executionRoleArn")

    if (
        schema != AUTHORIZATION_SCHEMA
        or version != AUTHORIZATION_VERSION
        or status != "ACTIVE"
        or expires_at_epoch <= now_epoch
    ):
        raise QualificationMutationError("authorization record is not active")
    if (
        authorization_id != event.authorization_id
        or owner_id != event.owner_id
        or fence_token != event.fence_token
        or stack_kind != event.stack_kind
        or legal_edge != event.legal_edge
    ):
        raise QualificationMutationError("authorization binding does not match")
    stack_match = _STACK_ARN.fullmatch(stack_arn)
    expected_stack_name = (
        MANAGED_AGENTCORE_STACK_NAME if event.stack_kind == "agentcore" else MANAGED_CONTROL_PLANE_STACK_NAME
    )
    restored_prefix = f"{config.primary_table_name}-restore-validation-"
    if (
        stack_match is None
        or stack_match.group("partition") != config.partition
        or stack_match.group("region") != config.region
        or stack_match.group("account") != config.account
        or stack_match.group("name") != expected_stack_name
        or primary_table_name != config.primary_table_name
        or _TABLE_NAME.fullmatch(restored_table_name) is None
        or len(restored_table_name) <= len(restored_prefix)
        or not restored_table_name.startswith(restored_prefix)
        or execution_role_arn != config.execution_role_arn
    ):
        raise QualificationMutationError("authorization infrastructure binding does not match")
    if (
        _SAFE_ID.fullmatch(authorization_id) is None
        or _SAFE_ID.fullmatch(owner_id) is None
        or not 1 <= fence_token <= 2**63 - 1
        or _APPROVAL_ID.fullmatch(approval_id) is None
    ):
        raise QualificationMutationError("authorization record contains invalid values")

    return Authorization(
        authorization_id=authorization_id,
        owner_id=owner_id,
        fence_token=fence_token,
        stack_kind=stack_kind,
        legal_edge=legal_edge,
        expires_at_epoch=expires_at_epoch,
        stack_arn=stack_arn,
        primary_table_name=primary_table_name,
        restored_table_name=restored_table_name,
        approval_id=approval_id,
        execution_role_arn=execution_role_arn,
    )


def _stack_values(raw_items: Any, key_name: str, value_name: str) -> dict[str, str]:
    if type(raw_items) is not list:
        raise QualificationMutationError("qualification stack metadata is malformed")
    values: dict[str, str] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise QualificationMutationError("qualification stack metadata is malformed")
        key = raw_item.get(key_name)
        value = raw_item.get(value_name)
        if not isinstance(key, str) or not isinstance(value, str) or key in values:
            raise QualificationMutationError("qualification stack metadata is malformed")
        values[key] = value
    return values


def _parameter_names(raw_parameters: Any) -> tuple[str, ...]:
    if type(raw_parameters) is not list:
        raise QualificationMutationError("qualification stack parameters are malformed")
    names: set[str] = set()
    for parameter in raw_parameters:
        name = parameter.get("ParameterKey") if isinstance(parameter, Mapping) else None
        if not isinstance(name, str) or not name or name in names:
            raise QualificationMutationError("qualification stack parameters are malformed")
        names.add(name)
    if not _RECOVERY_PARAMETERS.issubset(names):
        raise QualificationMutationError("qualification stack lacks recovery parameters")
    return tuple(sorted(names))


def _describe_stack(
    cloudformation_client: Any,
    *,
    authorization: Authorization,
) -> StackState:
    try:
        response = cloudformation_client.describe_stacks(
            StackName=authorization.stack_arn,
        )
    except Exception as exc:
        raise QualificationMutationError("qualification stack could not be described") from exc
    if not isinstance(response, Mapping):
        raise QualificationMutationError("qualification stack response is malformed")
    stacks = response.get("Stacks")
    if type(stacks) is not list or len(stacks) != 1 or not isinstance(stacks[0], Mapping):
        raise QualificationMutationError("qualification stack response is ambiguous")
    stack = stacks[0]
    expected_name = (
        MANAGED_AGENTCORE_STACK_NAME if authorization.stack_kind == "agentcore" else MANAGED_CONTROL_PLANE_STACK_NAME
    )
    if (
        stack.get("StackId") != authorization.stack_arn
        or stack.get("RoleARN") != authorization.execution_role_arn
        or ("StackName" in stack and stack.get("StackName") != expected_name)
    ):
        raise QualificationMutationError("qualification stack binding does not match")

    status = stack.get("StackStatus")
    if not isinstance(status, str) or status not in (_STABLE_STACK_STATUSES | _UPDATING_STACK_STATUSES):
        raise QualificationMutationError("qualification stack is not safely mutable")
    outputs = _stack_values(stack.get("Outputs"), "OutputKey", "OutputValue")
    primary_output = "StateTableName" if authorization.stack_kind == "agentcore" else "PrimaryStateTableName"
    required_outputs = {
        primary_output,
        "RecoveryApprovalId",
        "RecoveryCutoverMode",
        "SelectedRuntimeStateTableName",
    }
    if not required_outputs.issubset(outputs):
        raise QualificationMutationError("qualification stack outputs are incomplete")
    if outputs[primary_output] != authorization.primary_table_name or outputs["SelectedRuntimeStateTableName"] not in {
        authorization.primary_table_name,
        authorization.restored_table_name,
    }:
        raise QualificationMutationError("qualification selector table is not authorized")
    if (
        authorization.stack_kind == "control-plane"
        and outputs.get("AgentCoreStackName") != MANAGED_AGENTCORE_STACK_NAME
    ):
        raise QualificationMutationError("control-plane stack is not linked to qualification")

    mode = outputs["RecoveryCutoverMode"]
    allowed_modes = _AGENTCORE_MODES if authorization.stack_kind == "agentcore" else _CONTROL_PLANE_MODES
    approval_id = outputs["RecoveryApprovalId"]
    if mode not in allowed_modes or (approval_id and _APPROVAL_ID.fullmatch(approval_id) is None):
        raise QualificationMutationError("qualification selector state is malformed")

    return StackState(
        status=status,
        mode=mode,
        selected_table_name=outputs["SelectedRuntimeStateTableName"],
        approval_id=approval_id,
        parameter_names=_parameter_names(stack.get("Parameters")),
    )


def _edge_table(authorization: Authorization) -> str:
    if authorization.legal_edge.endswith("-primary"):
        return authorization.primary_table_name
    if authorization.legal_edge.endswith("-restored"):
        return authorization.restored_table_name
    raise QualificationMutationError("authorization edge is invalid")


def _transition_step(
    authorization: Authorization,
    stack: StackState,
) -> TransitionStep:
    target_table = _edge_table(authorization)
    if authorization.legal_edge.startswith("quiesce-"):
        if stack.selected_table_name != target_table:
            raise QualificationMutationError("quiesce source table does not match")
        if stack.mode == "normal":
            return TransitionStep(
                complete=False,
                next_mode="quiesced",
                next_table_name=target_table,
            )
        if stack.mode == "quiesced" and stack.approval_id == authorization.approval_id:
            return TransitionStep(complete=True)
        raise QualificationMutationError("quiesce transition is not legal")

    if authorization.legal_edge.startswith("cutover-to-"):
        source_table = (
            authorization.restored_table_name
            if target_table == authorization.primary_table_name
            else authorization.primary_table_name
        )
        if stack.approval_id != authorization.approval_id:
            raise QualificationMutationError("cutover approval does not match")
        if stack.mode == "quiesced" and stack.selected_table_name == source_table:
            return TransitionStep(
                complete=False,
                next_mode="selected",
                next_table_name=target_table,
            )
        if stack.mode == "selected" and stack.selected_table_name == target_table:
            if authorization.stack_kind == "agentcore":
                return TransitionStep(
                    complete=False,
                    next_mode="validation",
                    next_table_name=target_table,
                )
            return TransitionStep(complete=True)
        if (
            authorization.stack_kind == "agentcore"
            and stack.mode == "validation"
            and stack.selected_table_name == target_table
        ):
            return TransitionStep(complete=True)
        raise QualificationMutationError("cutover transition is not legal")

    if authorization.legal_edge.startswith("resume-"):
        if stack.selected_table_name != target_table or stack.approval_id != authorization.approval_id:
            raise QualificationMutationError("resume binding does not match")
        if stack.mode == "normal":
            return TransitionStep(complete=True)
        if authorization.stack_kind == "agentcore":
            if stack.mode == "validation":
                return TransitionStep(
                    complete=False,
                    next_mode="normal",
                    next_table_name=target_table,
                )
        elif stack.mode == "selected":
            return TransitionStep(
                complete=False,
                next_mode="normal",
                next_table_name=target_table,
            )
        raise QualificationMutationError("resume transition is not legal")

    raise QualificationMutationError("authorization edge is invalid")


def _update_parameters(
    authorization: Authorization,
    stack: StackState,
    step: TransitionStep,
) -> list[dict[str, Any]]:
    if step.complete or step.next_mode is None or step.next_table_name is None:
        raise QualificationMutationError("completed transition cannot be updated")
    changes = {
        "RecoveryApprovalId": authorization.approval_id,
        "RecoveryCutoverMode": step.next_mode,
        "RuntimeStateTableName": (
            "" if step.next_table_name == authorization.primary_table_name else authorization.restored_table_name
        ),
    }
    return [
        (
            {"ParameterKey": name, "ParameterValue": changes[name]}
            if name in changes
            else {"ParameterKey": name, "UsePreviousValue": True}
        )
        for name in stack.parameter_names
    ]


def _client_request_token(
    authorization: Authorization,
    *,
    parameters: list[dict[str, Any]],
) -> str:
    payload = {
        "schema": AUTHORIZATION_SCHEMA,
        "version": AUTHORIZATION_VERSION,
        "authorizationId": authorization.authorization_id,
        "ownerId": authorization.owner_id,
        "fenceToken": authorization.fence_token,
        "stackKind": authorization.stack_kind,
        "legalEdge": authorization.legal_edge,
        "expiresAtEpoch": authorization.expires_at_epoch,
        "stackArn": authorization.stack_arn,
        "primaryTableName": authorization.primary_table_name,
        "restoredTableName": authorization.restored_table_name,
        "approvalId": authorization.approval_id,
        "executionRoleArn": authorization.execution_role_arn,
        "parameters": parameters,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"axonllm-qsm-{hashlib.sha256(canonical).hexdigest()}"


def _now_epoch(clock: Callable[[], float]) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualificationMutationError("clock returned an invalid value")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise QualificationMutationError("clock returned an invalid value")
    return value


def handle_event(
    event: Any,
    *,
    dynamodb_client: Any,
    cloudformation_client: Any,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, str]:
    """Validate and advance one authorized selector edge by at most one update."""

    parsed_event = _parse_event(event)
    config = _load_config(os.environ if environ is None else environ)
    authorization = _read_authorization(
        dynamodb_client,
        event=parsed_event,
        config=config,
        now_epoch=_now_epoch(time.time if clock is None else clock),
    )
    stack = _describe_stack(
        cloudformation_client,
        authorization=authorization,
    )
    step = _transition_step(authorization, stack)
    if stack.status in _UPDATING_STACK_STATUSES:
        return {"status": "PENDING"}
    if step.complete:
        return {"status": "COMPLETE"}

    parameters = _update_parameters(authorization, stack, step)
    try:
        response = cloudformation_client.update_stack(
            StackName=authorization.stack_arn,
            UsePreviousTemplate=True,
            RoleARN=authorization.execution_role_arn,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Parameters=parameters,
            ClientRequestToken=_client_request_token(
                authorization,
                parameters=parameters,
            ),
        )
    except Exception as exc:
        raise QualificationMutationError("qualification stack update could not be started") from exc
    if not isinstance(response, Mapping) or response.get("StackId") != authorization.stack_arn:
        raise QualificationMutationError("qualification stack update response is invalid")
    return {"status": "PENDING"}


def lambda_handler(
    event: Any,
    context: Any,
    *,
    dynamodb_client: Any | None = None,
    cloudformation_client: Any | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, str]:
    """AWS Lambda entry point with optional client injection for unit tests."""

    del context
    if dynamodb_client is None or cloudformation_client is None:
        import boto3

        if dynamodb_client is None:
            dynamodb_client = boto3.client("dynamodb")
        if cloudformation_client is None:
            cloudformation_client = boto3.client("cloudformation")
    return handle_event(
        event,
        dynamodb_client=dynamodb_client,
        cloudformation_client=cloudformation_client,
        environ=environ,
        clock=clock,
    )
