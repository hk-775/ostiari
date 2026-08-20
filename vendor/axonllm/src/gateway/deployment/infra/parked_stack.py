"""Empty CloudFormation shell used for reviewed runtime parking."""

from __future__ import annotations

from aws_cdk import CfnCondition, CfnResource, Fn, Stack
from constructs import Construct


class AxonLLMParkedStack(Stack):
    """Synthesize the same stack identity with no application resources."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        parked_component: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if parked_component not in {
            "agentcore-runtime",
            "managed-network",
        }:
            raise ValueError("parked_component must be agentcore-runtime or managed-network")
        self.template_options.description = (
            "AxonLLM parked CloudFormation shell for "
            f"{parked_component}; application resources are intentionally "
            "absent"
        )
        never_create = CfnCondition(
            self,
            "NeverCreateParkedSentinel",
            expression=Fn.condition_equals("parked", "active"),
        )
        sentinel = CfnResource(
            self,
            "ParkedSentinel",
            type="AWS::CloudFormation::WaitConditionHandle",
        )
        sentinel.cfn_options.condition = never_create


__all__ = ["AxonLLMParkedStack"]
