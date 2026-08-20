# Deployment Configuration Examples

These files exercise version 1 of the strict AgentCore deployment contract:

- `agentcore-existing-vpc.yaml` reuses customer networking and egress.
- `agentcore-managed-bedrock.yaml` creates a managed private network without
  NAT for AWS-only providers.
- `agentcore-managed-external.yaml` explicitly accepts managed NAT cost for
  public model providers.
- `agentcore-public-development.yaml` uses public runtime networking for
  development only.

The schema is packaged at
[`deployment-v1.schema.json`](../../src/gateway/deployment/schemas/deployment-v1.schema.json).
Unknown fields, duplicate YAML keys, plaintext credential fields, unsafe
network/profile combinations, and missing cost acknowledgements fail
validation.

The configuration contains topology and lifecycle choices only. Provider
credentials must be supplied through Secrets Manager or another secret-safe
bootstrap path.

Validation is local and non-mutating:

```python
from src.gateway.deployment.config_contract import load_deployment_config

config = load_deployment_config(
    "config/deployment/agentcore-existing-vpc.yaml"
)
```

The read-only planners are implemented as:

```text
axon deploy plan
axon deploy edge-plan
axon deploy lifecycle-plan
axon deploy standalone-plan
```

`lifecycle-plan` binds an already prepared CloudFormation change set to the
active or parked templates, immutable image and configuration, retained stack
hashes, and the deployment account and region. It cannot execute the change
set. After a separately approved operation, `axon deploy lifecycle-receipt`
compares explicit observations with the plan and writes a receipt only when
the runtime, optional managed network, retained state, and control plane all
match.

See `agentcore-runtime-lifecycle.example.json` and
`agentcore-runtime-lifecycle-status.example.json`. The current production
runbooks remain authoritative for creating and executing reviewed change
sets.

`standalone-ecs-existing.example.json` is the non-secret context for the
standalone ECS recipe. `standalone-plan` requires a digest-pinned private ECR
image and existing customer cluster, private subnets, security groups, IP
target group, state, KMS, IAM, logging, and OIDC resources. It emits hardened
task/service artifacts, creates no networking, and has no AWS execution path.
