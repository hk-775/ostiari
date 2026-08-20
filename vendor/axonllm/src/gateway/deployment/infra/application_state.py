"""Durable AxonLLM application-state resources shared by AWS hosts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from aws_cdk import (
    CfnCondition,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
    aws_backup as backup,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sqs as sqs,
)


PROVIDER_SECRET_FIELDS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "GCP_CREDENTIALS_JSON",
    "GCP_PROJECT_ID",
    "GCP_LOCATION",
    "VERTEX_AI_ENDPOINT",
    "GOOGLE_AI_API_KEY",
    "COHERE_API_KEY",
    "XAI_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
    "AI21_API_KEY",
)
_BACKUP_VAULT_NAME = re.compile(r"^[A-Za-z0-9._-]{2,50}$")
_FIFO_TOPIC_NAME = re.compile(r"^[A-Za-z0-9_-]{1,251}\.fifo$")


@dataclass(frozen=True)
class ApplicationStateResources:
    """References passed explicitly to runtime and control-plane stacks."""

    data_key: kms.Key
    routing_config_signing_key: kms.Key
    provider_secret: secretsmanager.Secret
    runtime_state_table_name: CfnParameter
    use_recovered_state: CfnCondition
    state_table: dynamodb.Table
    selected_state_table_name: str
    selected_state_table_arn: str
    event_dead_letter_queue: sqs.Queue
    event_outbox_queue: sqs.Queue
    security_event_topic: sns.Topic
    security_event_log_group: logs.LogGroup
    security_event_log_stream: logs.LogStream
    backup_key: kms.Key
    backup_vault: backup.BackupVault
    backup_service_role: iam.Role
    backup_plan: backup.BackupPlan


@dataclass(frozen=True)
class SecurityEventDeliveryStateAccess:
    """Retained resources consumed by request-independent event delivery."""

    stack_name: str
    data_key: kms.IKey
    event_outbox_queue: sqs.IQueue
    security_event_topic: sns.ITopic
    security_event_log_group: logs.ILogGroup
    security_event_log_group_arn: str


@dataclass(frozen=True)
class ApplicationStateAccess(SecurityEventDeliveryStateAccess):
    """State references consumed by both runtime and control-plane stacks."""

    primary_state_table_name: str
    selected_state_table_name: str
    selected_state_table_arn: str
    routing_config_signing_key: kms.IKey


@dataclass(frozen=True)
class AgentCoreApplicationStateAccess(ApplicationStateAccess):
    """Additional retained-state references required by AgentCore."""

    provider_secret: secretsmanager.ISecret
    event_dead_letter_queue: sqs.IQueue
    backup_vault_arn: str
    backup_service_role_arn: str


def application_state_mode(stack: Stack) -> str:
    """Return the explicit state ownership mode for one synthesized stack."""

    value = stack.node.try_get_context("application_state_mode") or "embedded"
    if value not in {"embedded", "external"}:
        raise ValueError(
            "application_state_mode must be 'embedded' or 'external'"
        )
    return value


def managed_application_state_access(
    *,
    stack_name: str,
    resources: ApplicationStateResources,
) -> AgentCoreApplicationStateAccess:
    """Project managed resources onto the same explicit access contract."""

    return AgentCoreApplicationStateAccess(
        stack_name=stack_name,
        primary_state_table_name=resources.state_table.table_name,
        selected_state_table_name=resources.selected_state_table_name,
        selected_state_table_arn=resources.selected_state_table_arn,
        data_key=resources.data_key,
        routing_config_signing_key=(
            resources.routing_config_signing_key
        ),
        event_outbox_queue=resources.event_outbox_queue,
        security_event_topic=resources.security_event_topic,
        security_event_log_group=resources.security_event_log_group,
        security_event_log_group_arn=(
            resources.security_event_log_group.log_group_arn
        ),
        provider_secret=resources.provider_secret,
        event_dead_letter_queue=resources.event_dead_letter_queue,
        backup_vault_arn=resources.backup_vault.backup_vault_arn,
        backup_service_role_arn=resources.backup_service_role.role_arn,
    )


def _external_parameter(
    stack: Stack,
    logical_id: str,
    *,
    description: str,
    allowed_pattern: str,
    default: str | None = None,
) -> CfnParameter:
    kwargs: dict[str, object] = {
        "type": "String",
        "allowed_pattern": allowed_pattern,
        "constraint_description": description,
        "description": description,
    }
    if default is not None:
        kwargs["default"] = default
    return CfnParameter(stack, logical_id, **kwargs)


def external_application_state_access(
    stack: Stack,
    *,
    default_stack_name: str,
    primary_state_table_name: str,
    selected_state_table_name: str,
    selected_state_table_arn: str,
) -> ApplicationStateAccess:
    """Bind validated retained-state identifiers without stack exports."""

    event_delivery = external_security_event_delivery_state_access(
        stack,
        default_stack_name=default_stack_name,
    )
    partition = r"(?:aws|aws-us-gov|aws-cn)"
    account = (
        r"[0-9]{12}"
        if Token.is_unresolved(stack.account)
        else re.escape(stack.account)
    )
    region = re.escape(stack.region)
    routing_key_arn = _external_parameter(
        stack,
        "ApplicationStateRoutingConfigSigningKeyArn",
        description=(
            "must be a complete KMS signing-key ARN in this account and region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:kms:{region}:{account}:"
            r"key/[0-9a-fA-F-]{36}$"
        ),
    )
    routing_config_signing_key = kms.Key.from_key_arn(
        stack,
        "ApplicationStateRoutingConfigSigningKey",
        routing_key_arn.value_as_string,
    )
    return ApplicationStateAccess(
        stack_name=event_delivery.stack_name,
        primary_state_table_name=primary_state_table_name,
        selected_state_table_name=selected_state_table_name,
        selected_state_table_arn=selected_state_table_arn,
        data_key=event_delivery.data_key,
        routing_config_signing_key=routing_config_signing_key,
        event_outbox_queue=event_delivery.event_outbox_queue,
        security_event_topic=event_delivery.security_event_topic,
        security_event_log_group=event_delivery.security_event_log_group,
        security_event_log_group_arn=event_delivery.security_event_log_group_arn,
    )


def external_security_event_delivery_state_access(
    stack: Stack,
    *,
    default_stack_name: str,
    include_queue_url: bool = True,
) -> SecurityEventDeliveryStateAccess:
    """Bind only the retained resources required by event delivery."""

    partition = r"(?:aws|aws-us-gov|aws-cn)"
    account = (
        r"[0-9]{12}"
        if Token.is_unresolved(stack.account)
        else re.escape(stack.account)
    )
    region = re.escape(stack.region)
    stack_name = _external_parameter(
        stack,
        "ApplicationStateStackName",
        description="must be the owning AxonLLM application-state stack",
        allowed_pattern=r"^[A-Za-z][A-Za-z0-9-]{0,127}$",
        default=default_stack_name,
    )
    data_key_arn = _external_parameter(
        stack,
        "ApplicationStateDataKeyArn",
        description=(
            "must be a complete KMS key ARN in this account and region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:kms:{region}:{account}:"
            r"key/[0-9a-fA-F-]{36}$"
        ),
    )
    outbox_queue_arn = _external_parameter(
        stack,
        "ApplicationStateSecurityEventOutboxQueueArn",
        description=(
            "must be the FIFO security-event outbox queue ARN in this account "
            "and region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:sqs:{region}:{account}:"
            r"[A-Za-z0-9_-]{1,75}\.fifo$"
        ),
    )
    outbox_queue_url = (
        _external_parameter(
            stack,
            "ApplicationStateSecurityEventOutboxQueueUrl",
            description=(
                "must be the FIFO security-event outbox queue URL in this "
                "account and region"
            ),
            allowed_pattern=(
                rf"^https://sqs\.{region}\.amazonaws\.com(?:\.cn)?/"
                rf"{account}/[A-Za-z0-9_-]{{1,75}}\.fifo$"
            ),
        )
        if include_queue_url
        else None
    )
    security_topic_arn = _external_parameter(
        stack,
        "ApplicationStateSecurityEventTopicArn",
        description=(
            "must be the FIFO security-event topic ARN in this account and "
            "region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:sns:{region}:{account}:"
            r"[A-Za-z0-9_-]{1,251}\.fifo$"
        ),
    )
    security_log_group_arn = _external_parameter(
        stack,
        "ApplicationStateSecurityEventLogGroupArn",
        description=(
            "must be the security-event log-group ARN in this account and "
            "region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:logs:{region}:{account}:"
            r"log-group:[A-Za-z0-9._/#-]{1,512}$"
        ),
    )

    data_key = kms.Key.from_key_arn(
        stack,
        "ApplicationStateDataKey",
        data_key_arn.value_as_string,
    )
    event_outbox_queue = (
        sqs.Queue.from_queue_attributes(
            stack,
            "ApplicationStateSecurityEventOutbox",
            queue_arn=outbox_queue_arn.value_as_string,
            queue_url=outbox_queue_url.value_as_string,
            key_arn=data_key.key_arn,
            fifo=True,
        )
        if outbox_queue_url is not None
        else sqs.Queue.from_queue_arn(
            stack,
            "ApplicationStateSecurityEventOutbox",
            outbox_queue_arn.value_as_string,
        )
    )
    security_event_topic = sns.Topic.from_topic_arn(
        stack,
        "ApplicationStateSecurityEventTopic",
        security_topic_arn.value_as_string,
    )
    security_event_log_group = logs.LogGroup.from_log_group_arn(
        stack,
        "ApplicationStateSecurityEventLogGroup",
        security_log_group_arn.value_as_string,
    )
    return SecurityEventDeliveryStateAccess(
        stack_name=stack_name.value_as_string,
        data_key=data_key,
        event_outbox_queue=event_outbox_queue,
        security_event_topic=security_event_topic,
        security_event_log_group=security_event_log_group,
        security_event_log_group_arn=(
            security_log_group_arn.value_as_string
        ),
    )


def external_agentcore_application_state_access(
    stack: Stack,
    *,
    default_stack_name: str,
    primary_state_table_name: str,
    selected_state_table_name: str,
    selected_state_table_arn: str,
) -> AgentCoreApplicationStateAccess:
    """Bind the full external-state contract required by AgentCore."""

    common = external_application_state_access(
        stack,
        default_stack_name=default_stack_name,
        primary_state_table_name=primary_state_table_name,
        selected_state_table_name=selected_state_table_name,
        selected_state_table_arn=selected_state_table_arn,
    )
    partition = r"(?:aws|aws-us-gov|aws-cn)"
    account = (
        r"[0-9]{12}"
        if Token.is_unresolved(stack.account)
        else re.escape(stack.account)
    )
    region = re.escape(stack.region)
    provider_secret_arn = _external_parameter(
        stack,
        "ApplicationStateProviderSecretArn",
        description=(
            "must be the complete provider secret ARN in this account and "
            "region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:secretsmanager:{region}:{account}:"
            r"secret:[A-Za-z0-9/_+=.@-]{1,512}$"
        ),
    )
    dead_letter_queue_arn = _external_parameter(
        stack,
        "ApplicationStateSecurityEventDeadLetterQueueArn",
        description=(
            "must be the FIFO security-event dead-letter queue ARN in this "
            "account and region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:sqs:{region}:{account}:"
            r"[A-Za-z0-9_-]{1,75}\.fifo$"
        ),
    )
    dead_letter_queue_url = _external_parameter(
        stack,
        "ApplicationStateSecurityEventDeadLetterQueueUrl",
        description=(
            "must be the FIFO security-event dead-letter queue URL in this "
            "account and region"
        ),
        allowed_pattern=(
            rf"^https://sqs\.{region}\.amazonaws\.com(?:\.cn)?/"
            rf"{account}/[A-Za-z0-9_-]{{1,75}}\.fifo$"
        ),
    )
    backup_vault_arn = _external_parameter(
        stack,
        "ApplicationStateBackupVaultArn",
        description=(
            "must be the application-state backup-vault ARN in this account "
            "and region"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:backup:{region}:{account}:"
            r"backup-vault:[A-Za-z0-9._-]{2,50}$"
        ),
    )
    backup_role_arn = _external_parameter(
        stack,
        "ApplicationStateBackupRoleArn",
        description=(
            "must be the application-state backup service-role ARN in this "
            "account"
        ),
        allowed_pattern=(
            rf"^arn:{partition}:iam::{account}:"
            r"role/[A-Za-z0-9+=,.@_/-]{1,512}$"
        ),
    )
    provider_secret = secretsmanager.Secret.from_secret_complete_arn(
        stack,
        "ApplicationStateProviderSecret",
        provider_secret_arn.value_as_string,
    )
    event_dead_letter_queue = sqs.Queue.from_queue_attributes(
        stack,
        "ApplicationStateSecurityEventDeadLetterQueue",
        queue_arn=dead_letter_queue_arn.value_as_string,
        queue_url=dead_letter_queue_url.value_as_string,
        key_arn=common.data_key.key_arn,
        fifo=True,
    )
    return AgentCoreApplicationStateAccess(
        stack_name=common.stack_name,
        primary_state_table_name=common.primary_state_table_name,
        selected_state_table_name=common.selected_state_table_name,
        selected_state_table_arn=common.selected_state_table_arn,
        data_key=common.data_key,
        routing_config_signing_key=(
            common.routing_config_signing_key
        ),
        event_outbox_queue=common.event_outbox_queue,
        security_event_topic=common.security_event_topic,
        security_event_log_group=common.security_event_log_group,
        security_event_log_group_arn=(
            common.security_event_log_group_arn
        ),
        provider_secret=provider_secret,
        event_dead_letter_queue=event_dead_letter_queue,
        backup_vault_arn=backup_vault_arn.value_as_string,
        backup_service_role_arn=backup_role_arn.value_as_string,
    )


def build_application_state_resources(
    stack: Stack,
    *,
    deployment_namespace: str,
    backup_vault_name: str | None = None,
    security_event_topic_name: str | None = None,
) -> ApplicationStateResources:
    """Create durable resources without runtime or network dependencies."""

    if backup_vault_name is not None and (
        not isinstance(backup_vault_name, str)
        or _BACKUP_VAULT_NAME.fullmatch(backup_vault_name) is None
    ):
        raise ValueError(
            "application_state_backup_vault_name must be a valid "
            "2-50 character AWS Backup vault name"
        )
    if security_event_topic_name is not None and (
        not isinstance(security_event_topic_name, str)
        or _FIFO_TOPIC_NAME.fullmatch(security_event_topic_name) is None
    ):
        raise ValueError(
            "application_state_security_event_topic_name must be a valid "
            "FIFO SNS topic name ending in .fifo"
        )

    physical_suffix = (
        f"-{deployment_namespace}" if deployment_namespace else ""
    )
    removal_policy = (
        RemovalPolicy.DESTROY
        if deployment_namespace
        else RemovalPolicy.RETAIN
    )
    deletion_protection = not bool(deployment_namespace)

    data_key = kms.Key(
        stack,
        "DataKey",
        alias=f"alias/axonllm/agentcore-data{physical_suffix}",
        description="Encrypts AxonLLM AgentCore state and logs",
        enable_key_rotation=True,
        removal_policy=removal_policy,
        pending_window=Duration.days(30),
    )
    routing_config_signing_key = kms.Key(
        stack,
        "RoutingConfigSigningKey",
        alias=(
            f"alias/axonllm/agentcore-routing-config"
            f"{physical_suffix}"
        ),
        description=(
            "Signs AxonLLM AgentCore routing configuration snapshots"
        ),
        key_spec=kms.KeySpec.ECC_NIST_P256,
        key_usage=kms.KeyUsage.SIGN_VERIFY,
        removal_policy=removal_policy,
        pending_window=Duration.days(30),
    )
    data_key.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AllowCloudWatchLogsEncryption",
            principals=[
                iam.ServicePrincipal(
                    f"logs.{stack.region}.{stack.url_suffix}"
                )
            ],
            actions=[
                "kms:Decrypt",
                "kms:DescribeKey",
                "kms:Encrypt",
                "kms:GenerateDataKey*",
                "kms:ReEncrypt*",
            ],
            resources=["*"],
            conditions={
                "ArnLike": {
                    "kms:EncryptionContext:aws:logs:arn": (
                        f"arn:{stack.partition}:logs:{stack.region}:"
                        f"{stack.account}:log-group:*"
                    )
                }
            },
        )
    )
    data_key.add_to_resource_policy(
        iam.PolicyStatement(
            sid="AllowCloudWatchAlarmEncryption",
            principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
            actions=["kms:Decrypt", "kms:GenerateDataKey*"],
            resources=["*"],
        )
    )

    provider_secret = secretsmanager.Secret(
        stack,
        "ProviderCredentials",
        description=(
            "AxonLLM AgentCore HTTP-provider credentials and endpoints"
        ),
        encryption_key=data_key,
        generate_secret_string=secretsmanager.SecretStringGenerator(
            secret_string_template=json.dumps(
                {
                    field_name: ""
                    for field_name in PROVIDER_SECRET_FIELDS
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            generate_string_key="placeholder",
        ),
        removal_policy=removal_policy,
    )

    state_table_name = (
        stack.node.try_get_context("agentcore_table_name")
        or f"axonllm-agentcore-state{physical_suffix}"
    )
    restore_table_marker = "-restore-validation-"
    restore_table_suffix_limit = (
        255
        - len(state_table_name)
        - len(restore_table_marker)
    )
    if restore_table_suffix_limit < 21:
        raise ValueError(
            "AgentCore state table name must be at most 214 characters "
            "to preserve the PITR validation suffix"
        )
    restored_state_table_pattern = (
        rf"^$|^{re.escape(state_table_name)}"
        rf"{restore_table_marker}[A-Za-z0-9_.-]"
        rf"{{1,{restore_table_suffix_limit}}}$"
    )
    runtime_state_table_name = CfnParameter(
        stack,
        "RuntimeStateTableName",
        type="String",
        default="",
        allowed_pattern=restored_state_table_pattern,
        constraint_description=(
            "must be blank or a PITR validation table derived from the "
            f"{state_table_name} primary table"
        ),
        description=(
            "Optional restored state table selected through the "
            "reviewed AgentCore recovery workflow"
        ),
    )
    use_recovered_state = CfnCondition(
        stack,
        "UseRecoveredState",
        expression=Fn.condition_not(
            Fn.condition_equals(
                runtime_state_table_name.value_as_string,
                "",
            )
        ),
    )

    state_table = dynamodb.Table(
        stack,
        "StateTable",
        table_name=state_table_name,
        partition_key=dynamodb.Attribute(
            name="PK",
            type=dynamodb.AttributeType.STRING,
        ),
        sort_key=dynamodb.Attribute(
            name="SK",
            type=dynamodb.AttributeType.STRING,
        ),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=data_key,
        deletion_protection=deletion_protection,
        point_in_time_recovery_specification=(
            dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            )
        ),
        time_to_live_attribute="expires_at",
        removal_policy=removal_policy,
    )
    selected_state_table_name = Token.as_string(
        Fn.condition_if(
            use_recovered_state.logical_id,
            runtime_state_table_name.value_as_string,
            state_table.table_name,
        )
    )
    selected_state_table_arn = stack.format_arn(
        service="dynamodb",
        resource="table",
        resource_name=selected_state_table_name,
    )

    event_dead_letter_queue = sqs.Queue(
        stack,
        "SecurityEventDeadLetterQueue",
        fifo=True,
        content_based_deduplication=False,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=data_key,
        enforce_ssl=True,
        retention_period=Duration.days(14),
        removal_policy=removal_policy,
    )
    event_outbox_queue = sqs.Queue(
        stack,
        "SecurityEventOutboxQueue",
        fifo=True,
        content_based_deduplication=False,
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=data_key,
        enforce_ssl=True,
        retention_period=Duration.days(14),
        receive_message_wait_time=Duration.seconds(20),
        visibility_timeout=Duration.minutes(5),
        dead_letter_queue=sqs.DeadLetterQueue(
            max_receive_count=5,
            queue=event_dead_letter_queue,
        ),
        removal_policy=removal_policy,
    )
    security_event_topic = sns.Topic(
        stack,
        "SecurityEventTopic",
        topic_name=security_event_topic_name,
        display_name="AxonLLM AgentCore durable security events",
        fifo=True,
        content_based_deduplication=False,
        enforce_ssl=True,
        master_key=data_key,
    )
    security_event_topic.apply_removal_policy(removal_policy)
    security_event_log_group = logs.LogGroup(
        stack,
        "SecurityEventLogGroup",
        encryption_key=data_key,
        retention=logs.RetentionDays.ONE_YEAR,
        removal_policy=removal_policy,
    )
    security_event_log_stream = logs.LogStream(
        stack,
        "SecurityEventLogStream",
        log_group=security_event_log_group,
        log_stream_name="events",
    )
    security_event_log_stream.apply_removal_policy(removal_policy)

    backup_key = kms.Key(
        stack,
        "BackupKey",
        alias=f"alias/axonllm/agentcore-backups{physical_suffix}",
        description="Encrypts scheduled AxonLLM AgentCore backups",
        enable_key_rotation=True,
        removal_policy=removal_policy,
        pending_window=Duration.days(30),
    )
    backup_vault = backup.BackupVault(
        stack,
        "StateBackupVault",
        backup_vault_name=(
            backup_vault_name
            if backup_vault_name is not None
            else Fn.join(
                "-",
                [
                    "axon-agent",
                    Fn.select(2, Fn.split("/", stack.stack_id)),
                ],
            )
        ),
        encryption_key=backup_key,
        block_recovery_point_deletion=(
            False if deployment_namespace else None
        ),
        lock_configuration=(
            None
            if deployment_namespace
            else backup.LockConfiguration(
                min_retention=Duration.days(30),
                max_retention=Duration.days(365),
            )
        ),
        removal_policy=removal_policy,
    )
    backup_service_role = iam.Role(
        stack,
        "StateBackupServiceRole",
        assumed_by=iam.ServicePrincipal(
            "backup.amazonaws.com",
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": stack.account,
                }
            },
        ),
        description=(
            "AWS Backup service role scoped to AxonLLM AgentCore state"
        ),
    )
    backup_table_arns = [
        state_table.table_arn,
        stack.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=(
                f"{state_table_name}-restore-validation-*"
            ),
        ),
    ]
    backup_service_role.add_to_policy(
        iam.PolicyStatement(
            sid="BackUpAxonLLMStateTables",
            actions=[
                "dynamodb:DescribeContinuousBackups",
                "dynamodb:DescribeTable",
                "dynamodb:ListTagsOfResource",
                "dynamodb:StartAwsBackupJob",
            ],
            resources=backup_table_arns,
        )
    )
    backup_service_role.add_to_policy(
        iam.PolicyStatement(
            sid="UseAxonLLMStateKeyThroughDynamoDB",
            actions=[
                "kms:Decrypt",
                "kms:GenerateDataKey*",
            ],
            resources=[data_key.key_arn],
            conditions={
                "StringEquals": {
                    "kms:CallerAccount": stack.account,
                    "kms:ViaService": (
                        f"dynamodb.{stack.region}.{stack.url_suffix}"
                    ),
                }
            },
        )
    )
    backup_plan = backup.BackupPlan(
        stack,
        "StateBackupPlan",
        backup_vault=backup_vault,
    )
    backup_plan.apply_removal_policy(removal_policy)
    backup_plan.add_rule(
        backup.BackupPlanRule(
            rule_name="DailyRetainedBackup",
            schedule_expression=events.Schedule.cron(
                minute="30",
                hour="5",
            ),
            start_window=Duration.hours(1),
            completion_window=Duration.hours(4),
            move_to_cold_storage_after=Duration.days(30),
            delete_after=Duration.days(365),
            recovery_point_tags={
                "Application": "AxonLLM",
                "Runtime": "AgentCore",
            },
        )
    )
    backup.CfnBackupSelection(
        stack,
        "StateTableSelection",
        backup_plan_id=backup_plan.backup_plan_id,
        backup_selection=(
            backup.CfnBackupSelection
            .BackupSelectionResourceTypeProperty(
                iam_role_arn=backup_service_role.role_arn,
                selection_name="StateTableSelection",
                resources=[state_table.table_arn],
            )
        ),
    )
    recovered_backup_selection = backup.CfnBackupSelection(
        stack,
        "RecoveredStateTableSelection",
        backup_plan_id=backup_plan.backup_plan_id,
        backup_selection=(
            backup.CfnBackupSelection
            .BackupSelectionResourceTypeProperty(
                iam_role_arn=backup_service_role.role_arn,
                selection_name="RecoveredStateTableSelection",
                resources=[
                    stack.format_arn(
                        service="dynamodb",
                        resource="table",
                        resource_name=(
                            runtime_state_table_name.value_as_string
                        ),
                    )
                ],
            )
        ),
    )
    recovered_backup_selection.cfn_options.condition = use_recovered_state

    return ApplicationStateResources(
        data_key=data_key,
        routing_config_signing_key=routing_config_signing_key,
        provider_secret=provider_secret,
        runtime_state_table_name=runtime_state_table_name,
        use_recovered_state=use_recovered_state,
        state_table=state_table,
        selected_state_table_name=selected_state_table_name,
        selected_state_table_arn=selected_state_table_arn,
        event_dead_letter_queue=event_dead_letter_queue,
        event_outbox_queue=event_outbox_queue,
        security_event_topic=security_event_topic,
        security_event_log_group=security_event_log_group,
        security_event_log_stream=security_event_log_stream,
        backup_key=backup_key,
        backup_vault=backup_vault,
        backup_service_role=backup_service_role,
        backup_plan=backup_plan,
    )


__all__ = [
    "AgentCoreApplicationStateAccess",
    "ApplicationStateAccess",
    "ApplicationStateResources",
    "PROVIDER_SECRET_FIELDS",
    "SecurityEventDeliveryStateAccess",
    "application_state_mode",
    "build_application_state_resources",
    "external_agentcore_application_state_access",
    "external_application_state_access",
    "external_security_event_delivery_state_access",
    "managed_application_state_access",
]
