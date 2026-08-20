"""Request-independent Lambda workers for durable AxonLLM background work."""

from __future__ import annotations

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    Duration,
    Fn,
    RemovalPolicy,
    Size,
    Stack,
    Tags,
    Token,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_sqs as sqs,
)
from constructs import Construct

if __package__:
    from .agentcore_stack import (
        ATHENA_ASSUME_ROLE_ACTIONS,
        load_athena_infrastructure_config,
    )
    from .application_state import (
        external_security_event_delivery_state_access,
    )
else:
    from agentcore_stack import (
        ATHENA_ASSUME_ROLE_ACTIONS,
        load_athena_infrastructure_config,
    )
    from application_state import (
        external_security_event_delivery_state_access,
    )

_DYNAMODB_ACTIONS = [
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
]


def _parameter(
    stack: Stack,
    logical_id: str,
    *,
    description: str,
    allowed_pattern: str,
    default: str | None = None,
) -> CfnParameter:
    options: dict[str, object] = {
        "type": "String",
        "allowed_pattern": allowed_pattern,
        "constraint_description": description,
        "description": description,
    }
    if default is not None:
        options["default"] = default
    return CfnParameter(stack, logical_id, **options)


class AxonLLMServerlessWorkersStack(Stack):
    """Deploy durable workers without an always-on application process."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        physical_suffix = f"-{deployment_namespace}" if deployment_namespace else ""
        removal_policy = RemovalPolicy.DESTROY if deployment_namespace else RemovalPolicy.RETAIN
        state_stack_default = "AxonLLMApplicationStateStack" + (
            f"-{deployment_namespace}" if deployment_namespace else ""
        )
        state = external_security_event_delivery_state_access(
            self,
            default_stack_name=state_stack_default,
            include_queue_url=False,
        )
        query_config = load_athena_infrastructure_config(self)
        primary_state_table = _parameter(
            self,
            "PrimaryStateTableName",
            description="must be the canonical AxonLLM DynamoDB table name",
            allowed_pattern=r"^[A-Za-z0-9_.-]{3,255}$",
        )
        runtime_state_table = _parameter(
            self,
            "RuntimeStateTableName",
            description=("must be blank or an approved restore-validation table"),
            allowed_pattern=r"^$|^[A-Za-z0-9_.-]{3,255}$",
            default="",
        )
        use_recovered_state = CfnCondition(
            self,
            "UseRecoveredState",
            expression=Fn.condition_not(
                Fn.condition_equals(
                    runtime_state_table.value_as_string,
                    "",
                )
            ),
        )
        selected_state_table_name = Token.as_string(
            Fn.condition_if(
                use_recovered_state.logical_id,
                runtime_state_table.value_as_string,
                primary_state_table.value_as_string,
            )
        )
        selected_state_table_arn = self.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=selected_state_table_name,
        )

        source_revision = _parameter(
            self,
            "SourceRevision",
            description="must be the full reviewed source commit SHA",
            allowed_pattern=r"^[0-9a-f]{40}$",
        )
        artifact_bucket_name = _parameter(
            self,
            "ArtifactBucketName",
            description="must be the private release artifact bucket",
            allowed_pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
        )
        worker_code_key = _parameter(
            self,
            "WorkerCodeObjectKey",
            description="must be the content-addressed worker Lambda ZIP key",
            allowed_pattern=r"^[A-Za-z0-9!_.*'()/-]{1,1024}\.zip$",
        )
        worker_code_version = _parameter(
            self,
            "WorkerCodeObjectVersion",
            description="must be the immutable worker S3 object version",
            allowed_pattern=r"^[A-Za-z0-9._~-]{1,1024}$",
        )
        worker_code_sha256 = _parameter(
            self,
            "WorkerCodeSha256",
            description="must be the verified worker ZIP SHA-256",
            allowed_pattern=r"^[0-9a-f]{64}$",
        )

        artifact_bucket = s3.Bucket.from_bucket_attributes(
            self,
            "ArtifactBucket",
            bucket_name=artifact_bucket_name.value_as_string,
            bucket_arn=Fn.join(
                "",
                [
                    "arn:",
                    self.partition,
                    ":s3:::",
                    artifact_bucket_name.value_as_string,
                ],
            ),
        )

        function_name = f"axonllm-security-event-worker{physical_suffix}"
        application_logs = logs.LogGroup(
            self,
            "SecurityEventWorkerLogs",
            encryption_key=state.data_key,
            log_group_name=f"/aws/lambda/{function_name}",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        worker = lambda_.Function(
            self,
            "SecurityEventWorker",
            architecture=lambda_.Architecture.ARM_64,
            code=lambda_.Code.from_bucket(
                artifact_bucket,
                worker_code_key.value_as_string,
                object_version=worker_code_version.value_as_string,
            ),
            description=Fn.join(
                "",
                [
                    "AxonLLM security-event worker artifact ",
                    worker_code_sha256.value_as_string,
                ],
            ),
            environment={
                "AWS_STS_REGIONAL_ENDPOINTS": "regional",
                "AXON_AWS_ACCOUNT_ID": self.account,
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_SECURITY_EVENT_LOG_GROUP_ARN": (state.security_event_log_group_arn),
                "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": (state.security_event_topic.topic_arn),
                "AXON_SOURCE_REVISION": source_revision.value_as_string,
                "HOME": "/tmp",
            },
            function_name=function_name,
            handler=("src.gateway.serverless_workers.security_event_lambda_handler"),
            log_group=application_logs,
            memory_size=512,
            reserved_concurrent_executions=10,
            runtime=lambda_.Runtime.PYTHON_3_12,
            timeout=Duration.seconds(45),
            tracing=lambda_.Tracing.ACTIVE,
        )
        Tags.of(worker).add(
            "AxonLLMArtifactSha256",
            worker_code_sha256.value_as_string,
        )
        Tags.of(worker).add(
            "AxonLLMSourceRevision",
            source_revision.value_as_string,
        )

        worker.add_event_source(
            lambda_event_sources.SqsEventSource(
                state.event_outbox_queue,
                batch_size=1,
                enabled=True,
                max_concurrency=10,
                report_batch_item_failures=True,
            )
        )
        worker.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventOutboxKey",
                actions=["kms:Decrypt"],
                resources=[state.data_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"sqs.{self.region}.{self.url_suffix}"),
                    }
                },
            )
        )
        state.security_event_topic.grant_publish(worker)
        worker.add_to_role_policy(
            iam.PolicyStatement(
                sid="UseSecurityEventTopicKey",
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=[state.data_key.key_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": self.account,
                        "kms:ViaService": (f"sns.{self.region}.{self.url_suffix}"),
                        "kms:EncryptionContext:aws:sns:topicArn": (state.security_event_topic.topic_arn),
                    }
                },
            )
        )
        state.security_event_log_group.grant_write(worker)

        export_dead_letter_queue = sqs.Queue(
            self,
            "ExportDeadLetterQueue",
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=state.data_key,
            enforce_ssl=True,
            fifo=True,
            retention_period=Duration.days(4),
            removal_policy=RemovalPolicy.DESTROY,
        )
        export_queue = sqs.Queue(
            self,
            "ExportQueue",
            content_based_deduplication=False,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=5,
                queue=export_dead_letter_queue,
            ),
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=state.data_key,
            enforce_ssl=True,
            fifo=True,
            receive_message_wait_time=Duration.seconds(20),
            retention_period=Duration.days(1),
            removal_policy=RemovalPolicy.DESTROY,
            visibility_timeout=Duration.hours(1),
        )
        export_bucket = s3.Bucket(
            self,
            "ExportBucket",
            auto_delete_objects=bool(deployment_namespace),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET],
                    allowed_origins=["*"],
                    exposed_headers=[
                        "Content-Disposition",
                        "Content-Length",
                        "ETag",
                    ],
                    max_age=300,
                )
            ],
            encryption=s3.BucketEncryption.KMS,
            encryption_key=state.data_key,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=(Duration.days(1)),
                    enabled=True,
                    expiration=Duration.days(1),
                )
            ],
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal_policy,
        )
        export_function_name = f"axonllm-export-worker{physical_suffix}"
        export_logs = logs.LogGroup(
            self,
            "ExportWorkerLogs",
            encryption_key=state.data_key,
            log_group_name=f"/aws/lambda/{export_function_name}",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=removal_policy,
        )
        export_worker = lambda_.Function(
            self,
            "ExportWorker",
            architecture=lambda_.Architecture.ARM_64,
            code=lambda_.Code.from_bucket(
                artifact_bucket,
                worker_code_key.value_as_string,
                object_version=worker_code_version.value_as_string,
            ),
            description=Fn.join(
                "",
                [
                    "AxonLLM asynchronous export artifact ",
                    worker_code_sha256.value_as_string,
                ],
            ),
            environment={
                "AWS_STS_REGIONAL_ENDPOINTS": "regional",
                "AXON_AWS_ACCOUNT_ID": self.account,
                "AXON_DEPLOYMENT_PROFILE": "production",
                "AXON_DYNAMODB_TABLE": selected_state_table_name,
                "AXON_EXPORT_BUCKET_NAME": export_bucket.bucket_name,
                "AXON_SOURCE_REVISION": source_revision.value_as_string,
                "HOME": "/tmp",
                "LLM_ROUTER_DYNAMODB_ENABLED": "true",
            },
            ephemeral_storage_size=Size.mebibytes(1024),
            function_name=export_function_name,
            handler="src.gateway.serverless_workers.export_lambda_handler",
            log_group=export_logs,
            memory_size=1024,
            reserved_concurrent_executions=2,
            runtime=lambda_.Runtime.PYTHON_3_12,
            timeout=Duration.minutes(10),
            tracing=lambda_.Tracing.ACTIVE,
        )
        Tags.of(export_worker).add(
            "AxonLLMArtifactSha256",
            worker_code_sha256.value_as_string,
        )
        Tags.of(export_worker).add(
            "AxonLLMSourceRevision",
            source_revision.value_as_string,
        )
        export_worker.add_event_source(
            lambda_event_sources.SqsEventSource(
                export_queue,
                batch_size=1,
                enabled=True,
                max_concurrency=2,
                report_batch_item_failures=True,
            )
        )
        export_worker.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadExportSourceAndUpdateJobs",
                actions=[
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:UpdateItem",
                ],
                resources=[
                    selected_state_table_arn,
                    f"{selected_state_table_arn}/index/*",
                ],
            )
        )
        export_bucket.grant_put(export_worker)
        state.data_key.grant_encrypt_decrypt(export_worker)

        query_role = iam.Role(
            self,
            "QueryReconciliationRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Dedicated principal trusted by approved Athena datasource roles for scheduled query reconciliation"
            ),
            role_name=f"axonllm-query-reconciliation{physical_suffix}",
        )
        query_function: lambda_.Function | None = None
        query_schedule: scheduler.CfnSchedule | None = None
        if query_config.enabled:
            query_function_name = f"axonllm-query-reconciliation{physical_suffix}"
            query_logs = logs.LogGroup(
                self,
                "QueryReconciliationLogs",
                encryption_key=state.data_key,
                log_group_name=f"/aws/lambda/{query_function_name}",
                retention=logs.RetentionDays.ONE_YEAR,
                removal_policy=removal_policy,
            )
            query_logs.grant_write(query_role)
            query_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReconcileCanonicalQueryState",
                    actions=_DYNAMODB_ACTIONS,
                    resources=[
                        selected_state_table_arn,
                        f"{selected_state_table_arn}/index/*",
                    ],
                )
            )
            query_role.add_to_policy(
                iam.PolicyStatement(
                    sid="AssumeApprovedAthenaDatasourceRoles",
                    actions=ATHENA_ASSUME_ROLE_ACTIONS,
                    resources=list(query_config.role_arns),
                )
            )
            query_function = lambda_.Function(
                self,
                "QueryReconciliationWorker",
                architecture=lambda_.Architecture.ARM_64,
                code=lambda_.Code.from_bucket(
                    artifact_bucket,
                    worker_code_key.value_as_string,
                    object_version=worker_code_version.value_as_string,
                ),
                description=Fn.join(
                    "",
                    [
                        "AxonLLM query reconciliation artifact ",
                        worker_code_sha256.value_as_string,
                    ],
                ),
                environment={
                    **query_config.environment(),
                    "AXON_DEPLOYMENT_PROFILE": "production",
                    "AXON_DYNAMODB_TABLE": selected_state_table_name,
                    "AXON_QUERY_RECONCILIATION_MAX_PAGES": "1",
                    "AXON_QUERY_RECONCILIATION_PAGE_SIZE": "2",
                    "AXON_SOURCE_REVISION": (source_revision.value_as_string),
                    "HOME": "/tmp",
                    "LLM_ROUTER_DYNAMODB_ENABLED": "true",
                },
                function_name=query_function_name,
                handler=("src.gateway.serverless_workers.query_reconciliation_lambda_handler"),
                log_group=query_logs,
                memory_size=512,
                reserved_concurrent_executions=1,
                role=query_role,
                runtime=lambda_.Runtime.PYTHON_3_12,
                timeout=Duration.seconds(120),
                tracing=lambda_.Tracing.ACTIVE,
            )
            Tags.of(query_function).add(
                "AxonLLMArtifactSha256",
                worker_code_sha256.value_as_string,
            )
            Tags.of(query_function).add(
                "AxonLLMSourceRevision",
                source_revision.value_as_string,
            )
            schedule_role = iam.Role(
                self,
                "QueryReconciliationSchedulerRole",
                assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
                description=("Invokes only the AxonLLM query reconciliation Lambda"),
            )
            query_function.grant_invoke(schedule_role)
            query_schedule = scheduler.CfnSchedule(
                self,
                "QueryReconciliationSchedule",
                flexible_time_window=(scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF")),
                name=f"axonllm-query-reconciliation{physical_suffix}",
                schedule_expression="rate(1 minute)",
                schedule_expression_timezone="UTC",
                state="ENABLED",
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=query_function.function_arn,
                    input=('{"schema":"axonllm.query-reconciliation/v1"}'),
                    retry_policy=(
                        scheduler.CfnSchedule.RetryPolicyProperty(
                            maximum_event_age_in_seconds=60,
                            maximum_retry_attempts=0,
                        )
                    ),
                    role_arn=schedule_role.role_arn,
                ),
            )

        CfnOutput(
            self,
            "ApplicationStateStackNameOutput",
            key="ApplicationStateStackName",
            value=state.stack_name,
        )
        CfnOutput(
            self,
            "ServerlessWorkersStackNameOutput",
            key="ServerlessWorkersStackName",
            value=self.stack_name,
        )
        CfnOutput(
            self,
            "SecurityEventWorkerArtifactSha256Output",
            key="SecurityEventWorkerArtifactSha256",
            value=worker_code_sha256.value_as_string,
        )
        CfnOutput(
            self,
            "SecurityEventWorkerFunctionArnOutput",
            key="SecurityEventWorkerFunctionArn",
            value=worker.function_arn,
        )
        CfnOutput(
            self,
            "ExportBucketArnOutput",
            key="ExportBucketArn",
            value=export_bucket.bucket_arn,
        )
        CfnOutput(
            self,
            "ExportBucketNameOutput",
            key="ExportBucketName",
            value=export_bucket.bucket_name,
        )
        CfnOutput(
            self,
            "ExportQueueArnOutput",
            key="ExportQueueArn",
            value=export_queue.queue_arn,
        )
        CfnOutput(
            self,
            "ExportQueueUrlOutput",
            key="ExportQueueUrl",
            value=export_queue.queue_url,
        )
        CfnOutput(
            self,
            "ExportWorkerFunctionArnOutput",
            key="ExportWorkerFunctionArn",
            value=export_worker.function_arn,
        )
        CfnOutput(
            self,
            "QueryReconciliationEnabledOutput",
            key="QueryReconciliationEnabled",
            value="true" if query_config.enabled else "false",
        )
        CfnOutput(
            self,
            "QueryReconciliationRoleArnOutput",
            key="QueryReconciliationRoleArn",
            value=query_role.role_arn,
        )
        if query_function is not None and query_schedule is not None:
            CfnOutput(
                self,
                "QueryReconciliationFunctionArnOutput",
                key="QueryReconciliationFunctionArn",
                value=query_function.function_arn,
            )
            CfnOutput(
                self,
                "QueryReconciliationScheduleArnOutput",
                key="QueryReconciliationScheduleArn",
                value=query_schedule.attr_arn,
            )
        CfnOutput(
            self,
            "SourceRevisionOutput",
            key="SourceRevision",
            value=source_revision.value_as_string,
        )


__all__ = ["AxonLLMServerlessWorkersStack"]
