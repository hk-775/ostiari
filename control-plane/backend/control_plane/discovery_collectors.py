"""Discovery collectors — concrete signal sources feeding the reconciliation
engine (discovery.py).

Each reads infrastructure WE control and emits Sightings; none touches an agent.

Available now:
  - TraceCollector   : REAL. Distinct agent_ids in our own gateway trace buffer.
  - CloudSignalCollector : MOCK stand-in for the AWS-native lenses (CloudTrail
      Bedrock calls, Secrets Manager key reads, Cost Explorer spend, resource
      inventory). Emits seeded sightings so multi-source discovery is demoable
      without a live AWS account. Replace with real boto3 collectors (below)
      when deployed in-account — same Sighting shape, drops into the same seam.

Planned real collectors (documented, not yet built — they need boto3 + IAM read
perms + a live account, so they return nothing in the local demo):
  - CloudTrailCollector : bedrock:InvokeModel events → agent_id from IAM principal
  - SecretsAccessCollector : GetSecretValue on LLM-key secrets → IAM principal
  - BillingCollector : new/spiking model spend as a discovery tripwire
  - ResourceCollector : list Bedrock Agents / AgentCore Registry / tagged workloads
"""

from __future__ import annotations

import os

from control_plane.discovery import Sighting
from control_plane.models.database import DEFAULT_ORG


class TraceCollector:
    """REAL source: agents seen in our own gateway trace buffer.

    Honest scope: only finds agents that have ALREADY routed at least one call
    through an Ostiari gateway. It cannot find agents that never touched us —
    that's what the cloud/egress collectors are for.
    """

    source = "gateway-traces"

    def __init__(self, org: str = DEFAULT_ORG) -> None:
        self._org = org

    def collect(self) -> list[Sighting]:
        # Read only the caller's org trace buffer: unscoped, one tenant's
        # Discovered view listed another tenant's agent ids and gateway names.
        from control_plane.routers.traces import recent_traces_for

        agg: dict[str, dict] = {}
        for t in recent_traces_for(self._org):
            aid = t.get("agent_id")
            if not aid or aid == "unknown":
                continue
            e = agg.setdefault(aid, {"count": 0, "gws": set(), "last": ""})
            e["count"] += 1
            gw = t.get("gateway_id") or t.get("sidecar_id")
            if gw:
                e["gws"].add(gw)
            ts = t.get("timestamp")
            if ts:
                e["last"] = str(ts)

        return [
            Sighting(
                agent_id=aid, source=self.source,
                evidence=f"{e['count']} governed call(s) observed",
                gateways=sorted(e["gws"]), call_count=e["count"],
                last_seen=e["last"], confidence=1.0,  # it really called us
            )
            for aid, e in agg.items()
        ]


# Seeded stand-in sightings for the AWS lenses. Includes a couple of agents that
# ARE registered (to show cross-source corroboration → "governed") and several
# that are NOT (the shadow-AI finds a trace-only lens would miss).
_MOCK_CLOUD_SIGHTINGS = [
    # source, agent_id, evidence, confidence
    ("cloudtrail", "research-agent",
     "bedrock:InvokeModel by IAM role research-agent-role (corroborates traces)", 0.95),
    ("cloudtrail", "batch-summarizer",
     "bedrock:InvokeModel by IAM role batch-jobs-role — never seen at any gateway", 0.9),
    ("cloudtrail", "notebook-explorer",
     "bedrock:InvokeModel from SageMaker notebook exec role — likely a dev agent", 0.6),
    ("secrets", "nightly-report-bot",
     "GetSecretValue on prod/openai-key by role nightly-cron — off-gateway agent", 0.85),
    ("billing", "unknown-openai-spend",
     "new OpenAI line item (~$430/mo) with no registered agent — investigate", 0.4),
    ("resources", "bedrock-flow-agent",
     "Bedrock Agent 'order-triage' exists in account; not in Ostiari registry", 0.8),
]


class CloudSignalCollector:
    """MOCK stand-in for the AWS-native discovery lenses.

    Emits seeded sightings so the multi-source Discovered view is real and
    demoable without a live AWS account. In-account, this is replaced by the
    real boto3 collectors (CloudTrail/Secrets/Billing/Resource), each emitting
    the same Sighting shape into the same engine — no reconciliation changes.

    OFF by default — emits nothing unless OSTIARI_DISCOVERY_MOCK is explicitly
    truthy (the demo sets it). This prevents fabricated "shadow agents" from
    appearing in a real deployment; production shows only real collectors until
    the boto3 cloud collectors are wired in.
    """

    source = "cloud-signals(mock)"

    def collect(self) -> list[Sighting]:
        if os.environ.get("OSTIARI_DISCOVERY_MOCK", "").lower() not in ("1", "true", "yes"):
            return []
        return [
            Sighting(
                agent_id=aid, source=f"{src}(mock)", evidence=ev,
                gateways=[], call_count=0, confidence=conf,
            )
            for (src, aid, ev, conf) in _MOCK_CLOUD_SIGHTINGS
        ]


def default_collectors(org: str = DEFAULT_ORG) -> list:
    """The collectors active in this deployment, scoped to one org."""
    return [TraceCollector(org), CloudSignalCollector()]
