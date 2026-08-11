"""Production discovery collector coverage with isolated AWS clients."""

from __future__ import annotations

import json

import pytest
from control_plane.discovery_collectors import (
    AwsBedrockAgentCollector,
    AwsCloudTrailLakeCollector,
    AwsTaggedResourceCollector,
    clear_discovery_cache,
    default_collectors,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_discovery_cache()
    yield
    clear_discovery_cache()


class _Session:
    region_name = "us-east-1"

    def __init__(self, clients):
        self.clients = clients

    def client(self, service, region_name=None):
        return self.clients[(service, region_name)]


class TestCloudTrailLakeCollector:
    def test_emits_model_calls_and_matching_secret_reads(self, monkeypatch):
        monkeypatch.setenv(
            "OSTIARI_DISCOVERY_CLOUDTRAIL_DATA_STORES",
            "us-east-1=eds-123",
        )
        monkeypatch.setenv("OSTIARI_DISCOVERY_SECRET_PATTERNS", "openai,anthropic")
        captured = {}

        class _CloudTrail:
            def start_query(self, **kwargs):
                captured["query"] = kwargs["QueryStatement"]
                return {"QueryId": "00000000-0000-0000-0000-000000000000"}

            def get_query_results(self, **_kwargs):
                return {
                    "QueryStatus": "FINISHED",
                    "QueryResultRows": [
                        [
                            {"eventTime": "2026-08-11 12:00:00"},
                            {"eventName": "InvokeModel"},
                            {"eventSource": "bedrock.amazonaws.com"},
                            {
                                "userIdentity": json.dumps({
                                    "type": "AssumedRole",
                                    "sessionContext": {
                                        "sessionIssuer": {
                                            "userName": "research-agent-role",
                                        },
                                    },
                                }),
                            },
                            {
                                "requestParameters": json.dumps({
                                    "modelId": "anthropic.claude-3-sonnet",
                                }),
                            },
                            {"resources": "[]"},
                            {"awsRegion": "us-east-1"},
                        ],
                        [
                            {"eventTime": "2026-08-11 12:01:00"},
                            {"eventName": "GetSecretValue"},
                            {"eventSource": "secretsmanager.amazonaws.com"},
                            {
                                "userIdentity": json.dumps({
                                    "userName": "nightly-report-bot",
                                }),
                            },
                            {
                                "requestParameters": json.dumps({
                                    "secretId": "prod/openai-key",
                                }),
                            },
                            {"resources": "[]"},
                            {"awsRegion": "us-east-1"},
                        ],
                        [
                            {"eventTime": "2026-08-11 12:02:00"},
                            {"eventName": "GetSecretValue"},
                            {"eventSource": "secretsmanager.amazonaws.com"},
                            {
                                "userIdentity": json.dumps({
                                    "userName": "ordinary-service",
                                }),
                            },
                            {
                                "requestParameters": json.dumps({
                                    "secretId": "prod/database-password",
                                }),
                            },
                            {"resources": "[]"},
                            {"awsRegion": "us-east-1"},
                        ],
                    ],
                }

        session = _Session({("cloudtrail", "us-east-1"): _CloudTrail()})
        collector = AwsCloudTrailLakeCollector(
            "org-a",
            session_factory=lambda: session,
        )
        sightings = collector.collect()
        by_id = {item.agent_id: item for item in sightings}

        assert set(by_id) == {"research-agent-role", "nightly-report-bot"}
        assert by_id["research-agent-role"].source == "aws-cloudtrail-bedrock"
        assert by_id["research-agent-role"].call_count == 1
        assert "anthropic.claude-3-sonnet" in by_id["research-agent-role"].evidence
        assert by_id["nightly-report-bot"].source == "aws-cloudtrail-secrets"
        assert by_id["nightly-report-bot"].call_count == 0
        assert "FROM eds-123" in captured["query"]
        assert "GetSecretValue" in captured["query"]

    def test_failure_is_reported_without_raising(self, monkeypatch):
        monkeypatch.setenv(
            "OSTIARI_DISCOVERY_CLOUDTRAIL_DATA_STORES",
            "us-east-1=eds-123",
        )

        class _BrokenSession:
            region_name = "us-east-1"

            @staticmethod
            def client(_service, region_name=None):
                raise RuntimeError(f"access denied in {region_name}")

        collector = AwsCloudTrailLakeCollector(
            "org-a",
            session_factory=_BrokenSession,
        )
        assert collector.collect() == []
        assert "access denied" in collector.last_error


class TestAwsInventoryCollectors:
    def test_bedrock_agent_inventory_paginates(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_DISCOVERY_AWS_REGIONS", "us-east-1")

        class _Bedrock:
            def list_agents(self, **kwargs):
                if "nextToken" not in kwargs:
                    return {
                        "agentSummaries": [{
                            "agentId": "A1",
                            "agentName": "order-triage",
                            "agentStatus": "PREPARED",
                            "updatedAt": "2026-08-11T12:00:00Z",
                        }],
                        "nextToken": "more",
                    }
                return {
                    "agentSummaries": [{
                        "agentId": "A2",
                        "agentName": "refund-review",
                        "agentStatus": "NOT_PREPARED",
                    }],
                }

        session = _Session({("bedrock-agent", "us-east-1"): _Bedrock()})
        collector = AwsBedrockAgentCollector(
            "org-a",
            session_factory=lambda: session,
        )
        sightings = collector.collect()

        assert [item.agent_id for item in sightings] == [
            "order-triage",
            "refund-review",
        ]
        assert all(item.source == "aws-bedrock-agents" for item in sightings)

    def test_tagged_resource_inventory_uses_explicit_agent_id(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_DISCOVERY_AWS_REGIONS", "us-east-1")
        monkeypatch.setenv("OSTIARI_DISCOVERY_AGENT_TAG_KEY", "company:agent-id")

        class _Tagging:
            @staticmethod
            def get_resources(**kwargs):
                assert kwargs["TagFilters"] == [{"Key": "company:agent-id"}]
                return {
                    "ResourceTagMappingList": [{
                        "ResourceARN": "arn:aws:lambda:us-east-1:123:function:report",
                        "Tags": [
                            {"Key": "company:agent-id", "Value": "report-agent"},
                            {"Key": "Environment", "Value": "prod"},
                        ],
                    }],
                    "PaginationToken": "",
                }

        session = _Session({
            ("resourcegroupstaggingapi", "us-east-1"): _Tagging(),
        })
        collector = AwsTaggedResourceCollector(
            "org-a",
            session_factory=lambda: session,
        )
        sightings = collector.collect()

        assert len(sightings) == 1
        assert sightings[0].agent_id == "report-agent"
        assert sightings[0].source == "aws-tagged-resources"
        assert "lambda" in sightings[0].evidence


def test_default_collectors_bind_aws_account_to_one_org(monkeypatch):
    monkeypatch.setenv("OSTIARI_DISCOVERY_AWS", "1")
    monkeypatch.setenv("OSTIARI_DISCOVERY_AWS_ORG", "org-a")
    monkeypatch.setenv(
        "OSTIARI_DISCOVERY_CLOUDTRAIL_DATA_STORES",
        "us-east-1=eds-123",
    )
    monkeypatch.delenv("OSTIARI_DISCOVERY_MOCK", raising=False)

    org_a = {collector.source for collector in default_collectors("org-a")}
    org_b = {collector.source for collector in default_collectors("org-b")}

    assert org_a == {
        "gateway-traces",
        "aws-cloudtrail-lake",
        "aws-bedrock-agents",
        "aws-tagged-resources",
    }
    assert org_b == {"gateway-traces"}
