#!/usr/bin/env python3
"""CDK app entry point bundled with the AxonLLM AgentCore launcher.

``cdk.json`` invokes this as ``.venv/bin/python3 app.py`` rather than ``python3
app.py``, and the explicit interpreter is the point. ``aws-cdk-lib`` is installed
into the launcher's content-addressed cache rather than made a dependency of
the AxonLLM runtime.

The relative path is deliberate: the CDK CLI runs this command with the directory
containing ``cdk.json`` as the working directory, so ``.venv/bin/python3``
resolves against ``infra/`` no matter where the caller was.
"""

import re

import aws_cdk as cdk
from aws_cdk import aws_iam as iam


_NAMESPACE_PATTERN = re.compile(
    r"^[a-z](?:[a-z0-9-]{0,14}[a-z0-9])?$"
)
_CDK_QUALIFIERS = {
    "production": "axprod",
    "qualification": "axqual",
    "external": "axext",
}


def deployment_namespace(app: cdk.App) -> str:
    value = app.node.try_get_context("deployment_namespace") or ""
    if (
        not isinstance(value, str)
        or (
            value
            and _NAMESPACE_PATTERN.fullmatch(value) is None
        )
    ):
        raise ValueError(
            "deployment_namespace must be 1-16 lowercase letters, digits, "
            "or internal hyphens, start with a letter, and end with a letter "
            "or digit"
        )
    return value


def stack_name(base: str, namespace: str) -> str:
    return f"{base}-{namespace}" if namespace else base


def cdk_qualifier(app: cdk.App, namespace: str) -> str:
    domain = (
        "production"
        if not namespace
        else "external"
        if namespace in {"external", "external-oidc"}
        else "qualification"
    )
    expected = _CDK_QUALIFIERS[domain]
    configured = app.node.try_get_context("cdk_qualifier")
    if configured is not None and configured != expected:
        raise ValueError(
            f"cdk_qualifier must be {expected!r} for the selected namespace"
        )
    return expected


def apply_service_boundary(
    stack: cdk.Stack,
    *,
    qualifier: str,
    region: str,
) -> None:
    boundary = iam.ManagedPolicy.from_managed_policy_arn(
        stack,
        "RequiredServiceRoleBoundary",
        (
            f"arn:{cdk.Aws.PARTITION}:iam::{cdk.Aws.ACCOUNT_ID}:policy/"
            "AxonLLMAgentCoreServiceBoundary-"
            f"{qualifier}-{region}"
        ),
    )
    iam.PermissionsBoundary.of(stack).apply(boundary)
    required_tags = {
        "Application": "AxonLLM",
        "AxonLLMTrustDomain": qualifier,
    }
    for construct in stack.node.find_all():
        if not isinstance(construct, iam.CfnRole):
            continue
        for key, value in required_tags.items():
            construct.tags.set_tag(key, value, priority=1_000)


app = cdk.App()
namespace = deployment_namespace(app)
qualifier = cdk_qualifier(app, namespace)

environment = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or "us-east-1",
)
region = app.node.try_get_context("region") or "us-east-1"
deployment_target = (
    app.node.try_get_context("deployment_target") or ""
).lower()

if deployment_target == "agentcore":
    from agentcore_stack import AxonLLMAgentCoreStack

    stack = AxonLLMAgentCoreStack(
        app,
        stack_name("AxonLLMAgentCoreStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target == "application-state":
    from application_state_stack import AxonLLMApplicationStateStack

    stack = AxonLLMApplicationStateStack(
        app,
        stack_name("AxonLLMApplicationStateStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
        termination_protection=not bool(namespace),
    )
elif deployment_target == "managed-network":
    from managed_network_stack import AxonLLMManagedNetworkStack

    stack = AxonLLMManagedNetworkStack(
        app,
        stack_name("AxonLLMManagedNetworkStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target in {
    "agentcore-parked",
    "managed-network-parked",
}:
    from parked_stack import AxonLLMParkedStack

    parked_component = (
        "agentcore-runtime"
        if deployment_target == "agentcore-parked"
        else "managed-network"
    )
    parked_stack_name = (
        "AxonLLMAgentCoreStack"
        if deployment_target == "agentcore-parked"
        else "AxonLLMManagedNetworkStack"
    )
    stack = AxonLLMParkedStack(
        app,
        stack_name(parked_stack_name, namespace),
        parked_component=parked_component,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(
            qualifier=qualifier
        ),
    )
elif deployment_target == "identity":
    from identity_stack import AxonLLMIdentityStack

    stack = AxonLLMIdentityStack(
        app,
        stack_name("AxonLLMIdentityStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target == "control-plane":
    from control_plane_stack import AxonLLMControlPlaneStack

    stack = AxonLLMControlPlaneStack(
        app,
        stack_name("AxonLLMControlPlaneStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target == "serverless-control-plane":
    from serverless_control_plane_stack import (
        AxonLLMServerlessControlPlaneStack,
    )

    stack = AxonLLMServerlessControlPlaneStack(
        app,
        stack_name("AxonLLMServerlessControlPlaneStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
elif deployment_target == "serverless-workers":
    from serverless_workers_stack import AxonLLMServerlessWorkersStack

    stack = AxonLLMServerlessWorkersStack(
        app,
        stack_name("AxonLLMServerlessWorkersStack", namespace),
        deployment_namespace=namespace,
        env=environment,
        synthesizer=cdk.DefaultStackSynthesizer(qualifier=qualifier),
    )
else:
    raise ValueError(
        "deployment_target must be 'agentcore', 'application-state', "
        "'agentcore-parked', 'managed-network', "
        "'managed-network-parked', 'identity', 'control-plane', or "
        "'serverless-control-plane' or 'serverless-workers'"
    )

apply_service_boundary(
    stack,
    qualifier=qualifier,
    region=region,
)

app.synth()
