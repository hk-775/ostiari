"""Retained AxonLLM application state with no runtime or network resources."""

from aws_cdk import CfnOutput, Stack
from constructs import Construct

if __package__:
    from .application_state import build_application_state_resources
else:
    from application_state import build_application_state_resources


class AxonLLMApplicationStateStack(Stack):
    """Own durable state independently from runtime and network lifecycle."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        deployment_namespace: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        backup_vault_name = self.node.try_get_context(
            "application_state_backup_vault_name"
        )
        security_event_topic_name = self.node.try_get_context(
            "application_state_security_event_topic_name"
        )
        state = build_application_state_resources(
            self,
            deployment_namespace=deployment_namespace,
            backup_vault_name=backup_vault_name,
            security_event_topic_name=security_event_topic_name,
        )

        outputs = {
            "ApplicationStateStackName": self.stack_name,
            "StateTableName": state.state_table.table_name,
            "SelectedRuntimeStateTableName": (
                state.selected_state_table_name
            ),
            "DataKeyArn": state.data_key.key_arn,
            "RoutingConfigSigningKeyArn": (
                state.routing_config_signing_key.key_arn
            ),
            "ProviderSecretArn": state.provider_secret.secret_arn,
            "SecurityEventOutboxQueueUrl": (
                state.event_outbox_queue.queue_url
            ),
            "SecurityEventOutboxQueueArn": (
                state.event_outbox_queue.queue_arn
            ),
            "SecurityEventDeadLetterQueueUrl": (
                state.event_dead_letter_queue.queue_url
            ),
            "SecurityEventDeadLetterQueueArn": (
                state.event_dead_letter_queue.queue_arn
            ),
            "SecurityEventTopicArn": state.security_event_topic.topic_arn,
            "SecurityEventLogGroupArn": (
                state.security_event_log_group.log_group_arn
            ),
            "StateBackupVaultArn": state.backup_vault.backup_vault_arn,
            "StateBackupRoleArn": state.backup_service_role.role_arn,
        }
        for output_name, value in outputs.items():
            CfnOutput(self, output_name, value=value)


__all__ = ["AxonLLMApplicationStateStack"]
