"""Infrastructure-backed agent discovery collectors.

Collectors read systems the operator already controls. They never call an
agent directly, and a failing source returns no sightings instead of breaking
the full discovery report.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from control_plane.discovery import Sighting
from control_plane.models.database import DEFAULT_ORG

log = logging.getLogger("control_plane.discovery")

_TRUTHY = {"1", "true", "yes", "on"}
_MODEL_EVENTS = (
    "InvokeModel",
    "InvokeModelWithResponseStream",
    "Converse",
    "ConverseStream",
)
_LAKE_TABLE = re.compile(r"^[A-Za-z0-9_-]+$")
_REGION = re.compile(r"^[a-z0-9-]+$")
_CACHE: dict[tuple[Any, ...], tuple[float, list[Sighting]]] = {}
_FAILURE_CACHE: dict[tuple[Any, ...], tuple[float, str]] = {}
_CACHE_LOCK = threading.RLock()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _csv(name: str) -> list[str]:
    return [
        value.strip()
        for value in os.environ.get(name, "").split(",")
        if value.strip()
    ]


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def clear_discovery_cache() -> None:
    """Clear cloud results after configuration changes and between tests."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _FAILURE_CACHE.clear()


def _cached(
    key: tuple[Any, ...],
    loader: Callable[[], list[Sighting]],
) -> list[Sighting]:
    now = time.monotonic()
    ttl = _bounded_int("OSTIARI_DISCOVERY_CACHE_SECONDS", 60, 5, 3600)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return list(cached[1])

    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + ttl, list(value))
    return value


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _principal(event: dict[str, Any]) -> str:
    identity = _json_object(event.get("userIdentity"))
    issuer = _json_object(
        _json_object(identity.get("sessionContext")).get("sessionIssuer")
    )
    for candidate in (
        issuer.get("userName"),
        identity.get("userName"),
        event.get("username"),
        event.get("Username"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    arn = str(identity.get("arn") or "")
    if "/assumed-role/" in arn:
        parts = arn.split("/assumed-role/", 1)[1].split("/")
        if parts and parts[0]:
            return parts[0]
    if "/" in arn:
        return arn.rsplit("/", 1)[-1]

    principal_id = str(identity.get("principalId") or "")
    if principal_id:
        return principal_id.split(":", 1)[0]
    return ""


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


class TraceCollector:
    """Agents observed through an Ostiari gateway."""

    source = "gateway-traces"

    def __init__(self, org: str = DEFAULT_ORG) -> None:
        self._org = org
        self.last_error = ""

    def collect(self) -> list[Sighting]:
        from control_plane.routers.traces import recent_traces_for

        agg: dict[str, dict[str, Any]] = {}
        for trace in recent_traces_for(self._org):
            agent_id = trace.get("agent_id")
            if not agent_id or agent_id == "unknown":
                continue
            item = agg.setdefault(
                agent_id,
                {"count": 0, "gateways": set(), "last_seen": ""},
            )
            item["count"] += 1
            gateway_id = trace.get("gateway_id") or trace.get("sidecar_id")
            if gateway_id:
                item["gateways"].add(gateway_id)
            if trace.get("timestamp"):
                item["last_seen"] = str(trace["timestamp"])

        return [
            Sighting(
                agent_id=agent_id,
                source=self.source,
                evidence=f"{item['count']} governed call(s) observed",
                gateways=sorted(item["gateways"]),
                call_count=item["count"],
                last_seen=item["last_seen"],
                confidence=1.0,
                governed=True,
            )
            for agent_id, item in agg.items()
        ]


_MOCK_CLOUD_SIGHTINGS = [
    (
        "cloudtrail",
        "research-agent",
        "bedrock:InvokeModel by IAM role research-agent-role (corroborates traces)",
        0.95,
    ),
    (
        "cloudtrail",
        "batch-summarizer",
        "bedrock:InvokeModel by IAM role batch-jobs-role - never seen at any gateway",
        0.9,
    ),
    (
        "cloudtrail",
        "notebook-explorer",
        "bedrock:InvokeModel from SageMaker notebook exec role - likely a dev agent",
        0.6,
    ),
    (
        "secrets",
        "nightly-report-bot",
        "GetSecretValue on prod/openai-key by role nightly-cron - off-gateway agent",
        0.85,
    ),
    (
        "billing",
        "unknown-openai-spend",
        "new OpenAI line item (~$430/mo) with no registered agent - investigate",
        0.4,
    ),
    (
        "resources",
        "bedrock-flow-agent",
        "Bedrock Agent 'order-triage' exists in account; not in Ostiari registry",
        0.8,
    ),
]


class CloudSignalCollector:
    """Seeded cloud sightings used only by the full demo."""

    source = "cloud-signals(mock)"
    last_error = ""

    def collect(self) -> list[Sighting]:
        return [
            Sighting(
                agent_id=agent_id,
                source=f"{source}(mock)",
                evidence=evidence,
                confidence=confidence,
            )
            for source, agent_id, evidence, confidence in _MOCK_CLOUD_SIGHTINGS
        ]


class _AwsCollector:
    source = "aws"

    def __init__(
        self,
        org: str,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._org = org
        self._session_factory = session_factory
        self.last_error = ""

    def _session(self) -> Any:
        if self._session_factory:
            return self._session_factory()
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "AWS discovery requires the control-plane 'aws' extra"
            ) from exc

        profile = os.environ.get("OSTIARI_DISCOVERY_AWS_PROFILE", "").strip()
        return boto3.Session(profile_name=profile or None)

    def _regions(self, session: Any) -> list[str]:
        regions = _csv("OSTIARI_DISCOVERY_AWS_REGIONS")
        if not regions:
            fallback = (
                getattr(session, "region_name", None)
                or os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION")
            )
            if fallback:
                regions = [fallback]
        if not regions:
            raise RuntimeError(
                "AWS discovery requires OSTIARI_DISCOVERY_AWS_REGIONS "
                "or a default AWS region"
            )
        invalid = [region for region in regions if not _REGION.fullmatch(region)]
        if invalid:
            raise RuntimeError(f"Invalid AWS discovery region: {invalid[0]}")
        return list(dict.fromkeys(regions))

    def _load(self) -> list[Sighting]:
        raise NotImplementedError

    def _cache_key(self) -> tuple[Any, ...]:
        return (
            self.source,
            self._org,
            tuple(_csv("OSTIARI_DISCOVERY_AWS_REGIONS")),
            id(self._session_factory) if self._session_factory else None,
        )

    def collect(self) -> list[Sighting]:
        self.last_error = ""
        now = time.monotonic()
        key: tuple[Any, ...] | None = None
        try:
            key = self._cache_key()
            with _CACHE_LOCK:
                failed = _FAILURE_CACHE.get(key)
                if failed and failed[0] > now:
                    self.last_error = failed[1]
                    return []
            result = _cached(key, self._load)
            with _CACHE_LOCK:
                _FAILURE_CACHE.pop(key, None)
            return result
        except Exception as exc:  # noqa: BLE001 - one source must not break discovery
            self.last_error = str(exc)
            failure_ttl = min(
                _bounded_int("OSTIARI_DISCOVERY_CACHE_SECONDS", 60, 5, 3600),
                30,
            )
            if key is not None:
                with _CACHE_LOCK:
                    _FAILURE_CACHE[key] = (
                        time.monotonic() + failure_ttl,
                        self.last_error,
                    )
            log.warning("%s discovery failed for org %s: %s", self.source, self._org, exc)
            return []


class AwsCloudTrailLakeCollector(_AwsCollector):
    """Model calls and LLM-secret reads queried from CloudTrail Lake."""

    source = "aws-cloudtrail-lake"

    def _stores(self) -> dict[str, str]:
        stores: dict[str, str] = {}
        for item in _csv("OSTIARI_DISCOVERY_CLOUDTRAIL_DATA_STORES"):
            if "=" not in item:
                raise RuntimeError(
                    "CloudTrail data stores must use REGION=EVENT_DATA_STORE_ID"
                )
            region, store_id = (part.strip() for part in item.split("=", 1))
            if not _REGION.fullmatch(region) or not _LAKE_TABLE.fullmatch(store_id):
                raise RuntimeError(f"Invalid CloudTrail data store mapping: {item}")
            stores[region] = store_id
        return stores

    def _cache_key(self) -> tuple[Any, ...]:
        return (
            *super()._cache_key(),
            tuple(sorted(self._stores().items())),
            os.environ.get("OSTIARI_DISCOVERY_LOOKBACK_HOURS", ""),
            os.environ.get("OSTIARI_DISCOVERY_MAX_EVENTS", ""),
            os.environ.get("OSTIARI_DISCOVERY_SECRET_PATTERNS", ""),
        )

    @staticmethod
    def _row(columns: list[dict[str, Any]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for column in columns:
            if isinstance(column, dict):
                row.update(column)
        return row

    def _query(
        self,
        client: Any,
        store_id: str,
        start: datetime,
        max_events: int,
    ) -> list[dict[str, Any]]:
        event_names = ", ".join(
            f"'{name}'" for name in (*_MODEL_EVENTS, "GetSecretValue")
        )
        model_names = ", ".join(f"'{name}'" for name in _MODEL_EVENTS)
        start_iso = start.strftime("%Y-%m-%d %H:%M:%S")
        statement = (
            "SELECT eventTime, eventName, eventSource, userIdentity, "
            f"requestParameters, resources, awsRegion FROM {store_id} "
            f"WHERE eventTime >= '{start_iso}' AND eventName IN ({event_names}) "
            "AND ("
            f"(eventSource = 'bedrock.amazonaws.com' AND eventName IN ({model_names})) "
            "OR (eventSource = 'secretsmanager.amazonaws.com' "
            "AND eventName = 'GetSecretValue')) "
            f"ORDER BY eventTime DESC LIMIT {max_events}"
        )
        query_id = client.start_query(QueryStatement=statement).get("QueryId")
        if not query_id:
            raise RuntimeError("CloudTrail Lake did not return a query id")

        timeout = _bounded_int(
            "OSTIARI_DISCOVERY_QUERY_TIMEOUT_SECONDS", 15, 1, 120
        )
        deadline = time.monotonic() + timeout
        while True:
            response = client.get_query_results(
                EventDataStore=store_id,
                QueryId=query_id,
                MaxQueryResults=min(max_events, 1000),
            )
            status = response.get("QueryStatus", "")
            if status == "FINISHED":
                break
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                detail = response.get("ErrorMessage") or status
                raise RuntimeError(f"CloudTrail Lake query {status.lower()}: {detail}")
            if time.monotonic() >= deadline:
                with suppress(Exception):
                    client.cancel_query(QueryId=query_id)
                raise RuntimeError("CloudTrail Lake query timed out")
            time.sleep(0.25)

        rows = [
            self._row(columns)
            for columns in response.get("QueryResultRows", [])
        ]
        token = response.get("NextToken")
        while token and len(rows) < max_events:
            response = client.get_query_results(
                EventDataStore=store_id,
                QueryId=query_id,
                NextToken=token,
                MaxQueryResults=min(max_events - len(rows), 1000),
            )
            rows.extend(
                self._row(columns)
                for columns in response.get("QueryResultRows", [])
            )
            token = response.get("NextToken")
        return rows[:max_events]

    @staticmethod
    def _secret_matches(event: dict[str, Any], patterns: list[str]) -> tuple[bool, str]:
        params = _json_object(event.get("requestParameters"))
        secret_id = str(params.get("secretId") or "")
        if not secret_id:
            resources = _json_list(event.get("resources"))
            for resource in resources:
                if isinstance(resource, dict):
                    secret_id = str(
                        resource.get("ARN")
                        or resource.get("resourceName")
                        or resource.get("ResourceName")
                        or ""
                    )
                    if secret_id:
                        break
        lowered = secret_id.lower()
        return any(pattern in lowered for pattern in patterns), secret_id

    def _load(self) -> list[Sighting]:
        stores = self._stores()
        if not stores:
            return []
        session = self._session()
        max_events = _bounded_int("OSTIARI_DISCOVERY_MAX_EVENTS", 1000, 1, 10000)
        lookback = _bounded_int(
            "OSTIARI_DISCOVERY_LOOKBACK_HOURS", 24, 1, 24 * 90
        )
        start = datetime.now(timezone.utc) - timedelta(hours=lookback)
        patterns = [
            value.lower()
            for value in (
                _csv("OSTIARI_DISCOVERY_SECRET_PATTERNS")
                or ["openai", "anthropic", "bedrock", "llm", "api-key"]
            )
        ]

        aggregated: dict[tuple[str, str], dict[str, Any]] = {}
        for region, store_id in stores.items():
            client = session.client("cloudtrail", region_name=region)
            for event in self._query(client, store_id, start, max_events):
                principal = _principal(event)
                if not principal:
                    continue
                event_name = str(event.get("eventName") or "")
                kind = ""
                detail = ""
                confidence = 0.0
                if event_name in _MODEL_EVENTS:
                    params = _json_object(event.get("requestParameters"))
                    detail = str(
                        params.get("modelId")
                        or params.get("modelIdentifier")
                        or "Bedrock model"
                    )
                    kind = "aws-cloudtrail-bedrock"
                    confidence = 0.95
                elif event_name == "GetSecretValue":
                    matches, secret_id = self._secret_matches(event, patterns)
                    if not matches:
                        continue
                    kind = "aws-cloudtrail-secrets"
                    detail = secret_id or "matched LLM credential"
                    confidence = 0.85
                else:
                    continue

                key = (kind, principal)
                item = aggregated.setdefault(
                    key,
                    {
                        "count": 0,
                        "latest": "",
                        "detail": detail,
                        "confidence": confidence,
                        "region": region,
                    },
                )
                item["count"] += 1
                timestamp = _iso(event.get("eventTime"))
                if timestamp >= item["latest"]:
                    item["latest"] = timestamp
                    item["detail"] = detail
                    item["region"] = str(event.get("awsRegion") or region)

        sightings: list[Sighting] = []
        for (source, principal), item in aggregated.items():
            if source == "aws-cloudtrail-bedrock":
                evidence = (
                    f"{item['count']} Bedrock model call(s) by IAM principal "
                    f"{principal}; latest model {item['detail']} in {item['region']}"
                )
                call_count = item["count"]
            else:
                evidence = (
                    f"{item['count']} LLM credential read(s) by IAM principal "
                    f"{principal}; latest match {item['detail']} in {item['region']}"
                )
                call_count = 0
            sightings.append(
                Sighting(
                    agent_id=principal,
                    source=source,
                    evidence=evidence,
                    last_seen=item["latest"],
                    call_count=call_count,
                    confidence=item["confidence"],
                )
            )
        return sightings


class AwsBedrockAgentCollector(_AwsCollector):
    """Bedrock Agent resources visible in configured AWS regions."""

    source = "aws-bedrock-agents"

    def _load(self) -> list[Sighting]:
        session = self._session()
        sightings: list[Sighting] = []
        for region in self._regions(session):
            client = session.client("bedrock-agent", region_name=region)
            token = None
            while True:
                request: dict[str, Any] = {"maxResults": 1000}
                if token:
                    request["nextToken"] = token
                response = client.list_agents(**request)
                for summary in response.get("agentSummaries", []):
                    agent_id = str(
                        summary.get("agentName") or summary.get("agentId") or ""
                    )
                    if not agent_id:
                        continue
                    sightings.append(
                        Sighting(
                            agent_id=agent_id,
                            source=self.source,
                            evidence=(
                                f"Bedrock Agent {summary.get('agentId', agent_id)} "
                                f"is {summary.get('agentStatus', 'present')} in {region}"
                            ),
                            last_seen=_iso(summary.get("updatedAt")),
                            confidence=1.0,
                        )
                    )
                token = response.get("nextToken")
                if not token:
                    break
        return sightings


class AwsTaggedResourceCollector(_AwsCollector):
    """AWS resources carrying an explicit Ostiari agent identity tag."""

    source = "aws-tagged-resources"

    def _cache_key(self) -> tuple[Any, ...]:
        return (
            *super()._cache_key(),
            os.environ.get("OSTIARI_DISCOVERY_AGENT_TAG_KEY", ""),
            os.environ.get("OSTIARI_DISCOVERY_RESOURCE_TYPES", ""),
        )

    def _load(self) -> list[Sighting]:
        session = self._session()
        tag_key = (
            os.environ.get("OSTIARI_DISCOVERY_AGENT_TAG_KEY", "").strip()
            or "ostiari:agent-id"
        )
        resource_types = _csv("OSTIARI_DISCOVERY_RESOURCE_TYPES")
        sightings: list[Sighting] = []
        for region in self._regions(session):
            client = session.client("resourcegroupstaggingapi", region_name=region)
            token = ""
            while True:
                request: dict[str, Any] = {
                    "TagFilters": [{"Key": tag_key}],
                    "ResourcesPerPage": 100,
                }
                if resource_types:
                    request["ResourceTypeFilters"] = resource_types
                if token:
                    request["PaginationToken"] = token
                response = client.get_resources(**request)
                for mapping in response.get("ResourceTagMappingList", []):
                    tags = {
                        str(tag.get("Key")): str(tag.get("Value") or "")
                        for tag in mapping.get("Tags", [])
                    }
                    agent_id = tags.get(tag_key, "").strip()
                    if not agent_id:
                        continue
                    arn = str(mapping.get("ResourceARN") or "")
                    sightings.append(
                        Sighting(
                            agent_id=agent_id,
                            source=self.source,
                            evidence=f"Resource tagged {tag_key} in {region}: {arn}",
                            confidence=1.0,
                        )
                    )
                token = str(response.get("PaginationToken") or "")
                if not token:
                    break
        return sightings


def _aws_enabled_for(org: str) -> bool:
    if not _truthy("OSTIARI_DISCOVERY_AWS"):
        return False
    configured_org = (
        os.environ.get("OSTIARI_DISCOVERY_AWS_ORG", "").strip() or DEFAULT_ORG
    )
    return org == configured_org


def default_collectors(org: str = DEFAULT_ORG) -> list[Any]:
    """Return only sources enabled for this tenant and deployment."""
    collectors: list[Any] = [TraceCollector(org)]
    if _truthy("OSTIARI_DISCOVERY_MOCK"):
        collectors.append(CloudSignalCollector())
    if _aws_enabled_for(org):
        if _csv("OSTIARI_DISCOVERY_CLOUDTRAIL_DATA_STORES"):
            collectors.append(AwsCloudTrailLakeCollector(org))
        collectors.extend(
            [
                AwsBedrockAgentCollector(org),
                AwsTaggedResourceCollector(org),
            ]
        )
    return collectors
