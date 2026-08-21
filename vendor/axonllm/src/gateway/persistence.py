"""DynamoDB persistence layer for LLM Router state.

Controlled by LLM_ROUTER_DYNAMODB_ENABLED env var (default: "false").
Uses a single DynamoDB table with composite keys (PK/SK pattern).
"""

import asyncio
import json
import logging
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.gateway.models import (
    APIKey,
    BudgetReservationResult,
    FeedbackRecord,
    GuardrailRule,
    PolicyNode,
    Principal,
    Project,
    RateLimitResult,
    ScimGroup,
    ScimUser,
    SpendCounterState,
    UsageRecord,
)
from src.gateway.routing_config import (
    RoutingConfigSnapshot,
)
from src.gateway.routing_config_contract import (
    ROUTING_CONFIG_SIGNATURE_SCHEMA,
    ROUTING_CONFIG_SIGNING_MODES,
    routing_config_signing_key_region,
    validate_routing_config_signing_key_arn,
)
from src.gateway.routing_config_signing import (
    KmsRoutingConfigAuthenticator,
    RoutingConfigRollbackError,
    RoutingConfigSignatureError,
)

logger = logging.getLogger(__name__)

_BUDGET_ALERT_THRESHOLDS = (
    Decimal("0.8"),
    Decimal("0.9"),
    Decimal("1.0"),
)
_TENANT_DATASOURCE_DOCUMENT_FIELDS = frozenset(
    {
        "name",
        "role_arn",
        "region",
        "catalog",
        "database",
        "workgroup",
        "enabled",
        "created_at",
        "updated_at",
    }
)
_MODEL_REGISTRY_MAX_DOCUMENT_BYTES = 350 * 1024
_DYNAMODB_MAX_ITEM_BYTES = 400 * 1024


def _validate_project_item_size(item: dict) -> None:
    """Reject project documents that cannot fit in one DynamoDB item."""
    try:
        encoded = json.dumps(
            item,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("project contains values that cannot be persisted") from exc
    # JSON adds structural bytes and escapes stored strings, so this is a
    # conservative upper bound for this flat scalar DynamoDB item.
    if len(encoded) > _DYNAMODB_MAX_ITEM_BYTES:
        raise ValueError("project exceeds the DynamoDB item size limit")


def validate_project_storage_size(project: Project) -> None:
    """Validate the complete serialized project before a storage write."""
    DynamoPersistence.serialize_project(project)


class CanonicalMembershipNotFoundError(RuntimeError):
    """The requested canonical project, user, or principal does not exist."""


class CanonicalMembershipConflictError(RuntimeError):
    """A concurrent authority update rejected a membership transaction."""


class PersistenceConflictError(RuntimeError):
    """A conditional full-document write lost a concurrent update race."""


class PersistenceQuotaExceededError(RuntimeError):
    """A transactional tenant resource quota rejected a create."""


def tenant_project_partition_key(tenant_id: str) -> str:
    """DynamoDB partition key for one tenant's project namespace."""
    return f"TENANT#{tenant_id}"


def tenant_project_sort_key(project_id: str) -> str:
    """DynamoDB sort key for a project inside a tenant namespace."""
    return f"PROJECT#{project_id}"


def _require_tenant_id(tenant_id: str) -> str:
    """Return a canonical tenant id or reject an ambiguous namespace."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    return tenant_id


def _require_revision(value: object, *, name: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(
            f"{name} must be a non-negative integer"
        ) from None
    if value != normalized or normalized < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return normalized


class DynamoPersistence:
    """DynamoDB persistence layer for LLM Router state."""

    def __init__(
        self,
        table_name: str | None = None,
        region: str = "us-east-1",
        routing_config_signing_mode: str | None = None,
        routing_config_signing_key_arn: str | None = None,
        routing_config_authenticator: (
            KmsRoutingConfigAuthenticator | None
        ) = None,
    ):
        # Table name comes from AXON_DYNAMODB_TABLE so the app and the CDK stack
        # agree on a single source of truth. The CDK provisions a table by this
        # exact name (see infra/stack.py) and passes it in via the env var; the
        # "axonllm-state" fallback matches the stack default for local/manual runs.
        self._table_name = (
            table_name
            or os.environ.get("AXON_DYNAMODB_TABLE", "axonllm-state")
        )
        self._region = region
        self._routing_config_signing_mode = (
            routing_config_signing_mode
            if routing_config_signing_mode is not None
            else os.environ.get(
                "AXON_ROUTING_CONFIG_SIGNING_MODE",
                "disabled",
            )
        ).strip().lower()
        if (
            self._routing_config_signing_mode
            not in ROUTING_CONFIG_SIGNING_MODES
        ):
            raise ValueError(
                "routing configuration signing mode is invalid"
            )
        self._routing_config_signing_key_arn = (
            routing_config_signing_key_arn
            if routing_config_signing_key_arn is not None
            else os.environ.get(
                "AXON_ROUTING_CONFIG_SIGNING_KEY_ARN",
                "",
            )
        ).strip()
        if self._routing_config_signing_key_arn:
            validate_routing_config_signing_key_arn(
                self._routing_config_signing_key_arn
            )
            if (
                routing_config_signing_key_region(
                    self._routing_config_signing_key_arn
                )
                != region
            ):
                raise ValueError(
                    "routing configuration signing key region does not "
                    "match the persistence region"
                )
        if self._routing_config_signing_mode == "disabled":
            self._routing_config_authenticator = None
        else:
            if not self._routing_config_signing_key_arn:
                raise RuntimeError(
                    "routing configuration signing requires a KMS key ARN"
                )
            self._routing_config_authenticator = (
                routing_config_authenticator
                or KmsRoutingConfigAuthenticator(
                    self._routing_config_signing_key_arn,
                    region=region,
                )
            )
        self._authenticated_routing_snapshot: (
            RoutingConfigSnapshot | None
        ) = None
        self._enabled = os.environ.get(
            "LLM_ROUTER_DYNAMODB_ENABLED", "false"
        ).lower() == "true"
        self._table = None
        self._dynamodb = None
        self._init_lock = threading.Lock()
        # Set to a short reason string when a write is dropped, so a health probe
        # can surface silent persistence failures instead of losing data quietly.
        self.last_write_error: str | None = None
        # Whether the last usage-record scan raised. Lets
        # ``load_usage_records_or_none`` distinguish an outage from an empty table
        # without changing ``load_usage_records``' return-[] contract.
        self._last_scan_failed = False
        # Same idea for the Cedar policy scan, where mistaking an outage for an
        # empty set would drop every enforced policy rather than lose a count.
        self._last_policy_scan_failed = False
        # And for the two config scans a live refresh re-reads. Same stakes as
        # the policy one: adopting an empty result would un-enforce every budget
        # and model restriction the fleet is running, so the refresh needs to
        # tell "the table is empty" from "the scan failed".
        self._last_project_scan_failed = False
        self._last_user_config_scan_failed = False

    @property
    def enabled(self) -> bool:
        """Whether DynamoDB persistence is active."""
        return self._enabled

    @property
    def routing_config_signing_mode(self) -> str:
        return self._routing_config_signing_mode

    @property
    def authenticated_routing_snapshot(
        self,
    ) -> RoutingConfigSnapshot | None:
        return self._authenticated_routing_snapshot

    def _record_write_failure(self, what: str, ident: str) -> None:
        """Log a dropped write at ERROR and remember it for the health probe.

        A silently-swallowed write means billing/usage/config data is lost with
        no signal — the failure mode that made persistence dead-on-arrival. We
        still don't raise (a provider call shouldn't 500 because Dynamo hiccuped),
        but we log loudly and expose it via health_status() so ops can detect it.
        """
        self.last_write_error = f"{what} {ident}"
        logger.error("Failed to persist %s %s to DynamoDB", what, ident, exc_info=True)

    async def health_status(self) -> dict:
        """Report persistence reachability for a health probe.

        Returns {"enabled": bool, "reachable": bool, "table": str,
        "last_write_error": str | None}. When enabled, performs a cheap
        describe_table so a misconfigured/missing table or IAM denial surfaces
        instead of silently dropping every write.
        """
        status = {
            "enabled": self._enabled,
            "table": self._table_name,
            "last_write_error": self.last_write_error,
            "reachable": None,
        }
        if not self._enabled:
            return status

        def _describe() -> bool:
            import boto3

            if self._dynamodb is None:
                self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
            self._dynamodb.meta.client.describe_table(TableName=self._table_name)
            return True

        try:
            status["reachable"] = await asyncio.to_thread(_describe)
        except Exception as exc:
            status["reachable"] = False
            status["error"] = f"{type(exc).__name__}: {exc}"
            logger.error(
                "DynamoDB table %s not reachable: %s", self._table_name, exc, exc_info=True
            )
        return status

    def _get_table(self):
        """Lazily create the boto3 DynamoDB Table resource (thread-safe)."""
        if self._table is None:
            with self._init_lock:
                if self._table is None:
                    import boto3

                    self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
                    self._table = self._dynamodb.Table(self._table_name)
        return self._table

    # --- Table management ---

    async def create_table_if_not_exists(self) -> None:
        """Create the DynamoDB table if it doesn't already exist."""
        if not self._enabled:
            return

        def _create():
            import boto3

            if self._dynamodb is None:
                self._dynamodb = boto3.resource(
                    "dynamodb", region_name=self._region
                )

            client = self._dynamodb.meta.client
            try:
                client.describe_table(TableName=self._table_name)
            except client.exceptions.ResourceNotFoundException:
                self._dynamodb.create_table(
                    TableName=self._table_name,
                    KeySchema=[
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                    AttributeDefinitions=[
                        {"AttributeName": "PK", "AttributeType": "S"},
                        {"AttributeName": "SK", "AttributeType": "S"},
                    ],
                    BillingMode="PAY_PER_REQUEST",
                )
                # Wait until the table exists
                client.get_waiter("table_exists").wait(
                    TableName=self._table_name
                )
                logger.info("Created DynamoDB table %s", self._table_name)
            # Reset cached table reference so it picks up the new table
            self._table = None

        try:
            await asyncio.to_thread(_create)
        except Exception:
            logger.warning(
                "Failed to create DynamoDB table %s", self._table_name, exc_info=True
            )

    # --- UsageRecord serialization ---

    @staticmethod
    def serialize_usage_record(record: UsageRecord) -> dict:
        """Serialize a UsageRecord to a DynamoDB item dict."""
        ts_iso = record.timestamp.isoformat()
        if record.tenant_id is None:
            partition_key = f"USAGE#{record.request_id}"
        else:
            partition_key = (
                f"TENANT#{record.tenant_id}#USAGE#{record.request_id}"
            )
        item = {
            "PK": partition_key,
            "SK": f"USAGE#{ts_iso}",
            "entity_type": "usage_record",
            "request_id": record.request_id,
            "project_id": record.project_id,
            "user_id": record.user_id,
            "provider": record.provider,
            "model": record.model,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
            "cost": record.cost,
            "timestamp": ts_iso,
            "cached_tokens": record.cached_tokens,
            "cache_creation_tokens": record.cache_creation_tokens,
            "image_tokens": record.image_tokens,
            "reasoning_tokens": record.reasoning_tokens,
            "latency_ms": record.latency_ms,
            "status": record.status,
            "routing_strategy": record.routing_strategy,
            "task_type": record.task_type,
            "provider_request_id": record.provider_request_id,
        }
        if record.tenant_id is not None:
            item["tenant_id"] = record.tenant_id
            item["GSI1PK"] = (
                f"TENANT#{record.tenant_id}#USAGE#"
                f"{record.timestamp:%Y-%m}"
            )
            item["GSI1SK"] = f"{ts_iso}#{record.request_id}"
        return item

    @staticmethod
    def deserialize_usage_record(item: dict) -> UsageRecord:
        """Deserialize a DynamoDB item dict to a UsageRecord."""
        return UsageRecord(
            request_id=item["request_id"],
            project_id=item["project_id"],
            user_id=item["user_id"],
            provider=item["provider"],
            model=item["model"],
            prompt_tokens=int(item["prompt_tokens"]),
            completion_tokens=int(item["completion_tokens"]),
            total_tokens=int(item["total_tokens"]),
            cost=float(item["cost"]),
            timestamp=datetime.fromisoformat(item["timestamp"]),
            tenant_id=item.get("tenant_id"),
            cached_tokens=int(item.get("cached_tokens", 0)),
            cache_creation_tokens=int(item.get("cache_creation_tokens", 0)),
            image_tokens=int(item.get("image_tokens", 0)),
            reasoning_tokens=int(item.get("reasoning_tokens", 0)),
            # float() is not redundant: _convert_decimals_to_native narrows a
            # whole-valued Decimal to int, so a latency that happens to land on
            # 1234.0 comes back as an int and the field's declared type silently
            # stops holding.
            latency_ms=float(item.get("latency_ms", 0.0)),
            # "" for a row written before this field existed, NOT "success" —
            # the dataclass default. Defaulting an unknown row to "success"
            # would manufacture an error rate: every pre-migration record would
            # assert it succeeded, and the one thing a reader wants from this
            # field is which requests failed.
            status=str(item.get("status", "")),
            routing_strategy=str(item.get("routing_strategy", "")),
            # Absent on every row written before this field existed. Defaulting to
            # "" rather than "general" is the whole point: an unclassified record
            # must not be counted as a classification result.
            task_type=str(item.get("task_type", "")),
            provider_request_id=str(item.get("provider_request_id", "")),
        )

    # --- Project serialization ---

    @staticmethod
    def serialize_project(project: Project) -> dict:
        """Serialize a Project to a DynamoDB item dict."""
        guardrail_rules = [
            {
                "name": rule.name,
                "rule_type": rule.rule_type,
                "pattern": rule.pattern,
                "action": rule.action,
                "applies_to": rule.applies_to,
            }
            for rule in project.guardrail_rules
        ]
        if project.tenant_id is None:
            partition_key = f"PROJECT#{project.project_id}"
            sort_key = "PROJECT"
        else:
            partition_key = tenant_project_partition_key(project.tenant_id)
            sort_key = tenant_project_sort_key(project.project_id)

        item = {
            "PK": partition_key,
            "SK": sort_key,
            "entity_type": "project",
            "project_id": project.project_id,
            "name": project.name,
            "budget_limit": project.budget_limit,
            "alert_threshold": project.alert_threshold,
            "allowed_models": json.dumps(project.allowed_models),
            "guardrail_rules": json.dumps(guardrail_rules),
            "cache_enabled": project.cache_enabled,
            "cache_ttl_seconds": project.cache_ttl_seconds,
            "semantic_cache_enabled": project.semantic_cache_enabled,
            "semantic_cache_threshold": project.semantic_cache_threshold,
            "log_level": project.log_level,
            "log_destination": project.log_destination,
            "prompt_caching_enabled": project.prompt_caching_enabled,
            "ltm_enabled": project.ltm_enabled,
            "retention_period_hours": project.retention_period_hours,
            "rate_limit_rpm": project.rate_limit_rpm,
            "members": json.dumps(project.members),
            "revision": project.revision,
            "created_at": project.created_at.isoformat(),
        }
        if project.tenant_id is not None:
            item["tenant_id"] = project.tenant_id
        _validate_project_item_size(item)
        return item

    @staticmethod
    def deserialize_project(item: dict) -> Project:
        """Deserialize a DynamoDB item dict to a Project."""
        allowed_models_raw = item.get("allowed_models")
        if isinstance(allowed_models_raw, str):
            allowed_models = json.loads(allowed_models_raw)
        else:
            allowed_models = allowed_models_raw

        guardrail_rules_raw = item.get("guardrail_rules", "[]")
        if isinstance(guardrail_rules_raw, str):
            guardrail_dicts = json.loads(guardrail_rules_raw)
        else:
            guardrail_dicts = guardrail_rules_raw
        guardrail_rules = [
            GuardrailRule(
                name=g["name"],
                rule_type=g["rule_type"],
                pattern=g.get("pattern"),
                action=g["action"],
                applies_to=g["applies_to"],
            )
            for g in guardrail_dicts
        ]

        members_raw = item.get("members", "[]")
        if isinstance(members_raw, str):
            members = json.loads(members_raw)
        else:
            members = members_raw

        return Project(
            project_id=item["project_id"],
            name=item["name"],
            tenant_id=item.get("tenant_id"),
            budget_limit=float(item["budget_limit"]) if item.get("budget_limit") is not None else None,
            alert_threshold=float(item["alert_threshold"]) if item.get("alert_threshold") is not None else None,
            allowed_models=allowed_models,
            guardrail_rules=guardrail_rules,
            cache_enabled=bool(item.get("cache_enabled", False)),
            cache_ttl_seconds=int(item.get("cache_ttl_seconds", 300)),
            semantic_cache_enabled=bool(item.get("semantic_cache_enabled", False)),
            # float(), not the raw value: DynamoDB returns numbers as Decimal,
            # and a Decimal threshold compares fine but would not round-trip
            # through JSON on the admin surface. None stays None — see the note
            # on the field, 0.0 would mean "match everything".
            semantic_cache_threshold=(
                float(item["semantic_cache_threshold"])
                if item.get("semantic_cache_threshold") is not None
                else None
            ),
            log_level=item.get("log_level", "INFO"),
            log_destination=item.get("log_destination"),
            prompt_caching_enabled=bool(
                item.get("prompt_caching_enabled", False)
            ),
            ltm_enabled=bool(item.get("ltm_enabled", False)),
            retention_period_hours=int(item.get("retention_period_hours", 24)),
            rate_limit_rpm=int(item["rate_limit_rpm"]) if item.get("rate_limit_rpm") is not None else None,
            members=members,
            revision=int(item.get("revision", 0)),
            created_at=datetime.fromisoformat(item["created_at"]) if "created_at" in item else datetime.now(timezone.utc),
        )

    # --- UserConfig serialization ---

    @staticmethod
    def serialize_user_config(user_id: str, config: dict) -> dict:
        """Serialize a user configuration to a DynamoDB item dict."""
        allowed_models = config.get("allowed_models")
        revision = _require_revision(config.get("revision", 0))
        return {
            "PK": f"USER#{user_id}",
            "SK": "CONFIG",
            "entity_type": "user_config",
            "user_id": user_id,
            "allowed_models": json.dumps(allowed_models) if allowed_models is not None else None,
            "budget_limit": config.get("budget_limit"),
            "alert_threshold": config.get("alert_threshold"),
            "revision": revision,
        }

    @staticmethod
    def deserialize_user_config(item: dict) -> tuple[str, dict]:
        """Deserialize a DynamoDB item dict to a (user_id, config) tuple."""
        user_id = item["user_id"]

        allowed_models_raw = item.get("allowed_models")
        if isinstance(allowed_models_raw, str):
            allowed_models = json.loads(allowed_models_raw)
        else:
            allowed_models = allowed_models_raw

        config = {
            "allowed_models": allowed_models,
            "budget_limit": float(item["budget_limit"]) if item.get("budget_limit") is not None else None,
            "alert_threshold": float(item["alert_threshold"]) if item.get("alert_threshold") is not None else None,
            "revision": int(item.get("revision", 0)),
        }
        return user_id, config

    @staticmethod
    def serialize_tenant_user_config(
        tenant_id: str,
        user_id: str,
        config: dict,
    ) -> dict:
        """Serialize user policy inside one tenant namespace."""
        tenant_id = _require_tenant_id(tenant_id)
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be a non-empty string")
        item = DynamoPersistence.serialize_user_config(user_id, config)
        item.update(
            {
                "PK": f"TENANT#{tenant_id}",
                "SK": f"USER_CONFIG#{user_id}",
                "entity_type": "tenant_user_config",
                "tenant_id": tenant_id,
            }
        )
        return item

    # --- Helpers ---

    @staticmethod
    def _convert_floats_to_decimal(item: dict) -> dict:
        """Convert float values to Decimal for DynamoDB compatibility."""
        converted = {}
        for key, value in item.items():
            if isinstance(value, float):
                converted[key] = Decimal(str(value))
            else:
                converted[key] = value
        return converted

    @staticmethod
    def _convert_decimals_to_native(item: dict) -> dict:
        """Convert Decimal values from DynamoDB back to int/float."""
        converted = {}
        for key, value in item.items():
            if isinstance(value, Decimal):
                if value == int(value):
                    converted[key] = int(value)
                else:
                    converted[key] = float(value)
            else:
                converted[key] = value
        return converted

    # --- Async DynamoDB operations ---

    async def save_usage_record(self, record: UsageRecord) -> None:
        """Serialize and write a UsageRecord to DynamoDB."""
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_usage_record(record)
            item = self._convert_floats_to_decimal(item)
            table.put_item(Item=item)

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("usage record", record.request_id)

    async def load_usage_records_or_none(self) -> list[UsageRecord] | None:
        """Like ``load_usage_records``, but None on failure instead of ``[]``.

        ``load_usage_records`` swallows its exceptions and returns an empty list,
        which is right for startup hydration — a gateway should boot with no
        history rather than refuse to start. It is wrong for a caller that
        rate-limits itself on the result: an outage looks identical to an empty
        store, so the caller records a successful refresh and serves
        single-instance numbers for a full window after the store recovers.

        Kept as a separate method rather than changing the original's contract,
        which several callers depend on.
        """
        if not self._enabled:
            return None
        records = await self.load_usage_records()
        return None if self._last_scan_failed else records

    async def load_usage_records(self) -> list[UsageRecord]:
        """Scan DynamoDB for all usage records and deserialize them."""
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("usage_record")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("usage_record"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            records = []
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                records.append(self.deserialize_usage_record(item))
            self._last_scan_failed = False
            return records
        except Exception:
            logger.warning("Failed to load usage records from DynamoDB", exc_info=True)
            # Recorded rather than raised, so this method's contract (boot with no
            # history rather than refuse to start) is unchanged, while
            # ``load_usage_records_or_none`` can still tell an outage from an
            # empty table.
            self._last_scan_failed = True
            return []

    async def load_audit_records(self, project_id: str | None = None) -> list[dict]:
        """Load persisted audit records (raw dicts), ordered by SK (timestamp).

        Audit rows use PK ``AUDIT#<project_id>`` / SK
        ``AUDIT#<iso>#<record_id>``. Returns them chronologically so the hash
        chain can be reloaded/verified against the durable store.
        """
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            flt = Attr("PK").begins_with("AUDIT#")
            if project_id:
                flt = Attr("PK").eq(f"AUDIT#{project_id}")
            items = []
            response = table.scan(FilterExpression=flt)
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=flt,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw = await asyncio.to_thread(_scan)
            raw = [self._convert_decimals_to_native(i) for i in raw]
            raw.sort(key=lambda i: i.get("SK", ""))
            return raw
        except Exception:
            logger.warning("Failed to load audit records from DynamoDB", exc_info=True)
            return []

    async def get_latest_audit_hash(self) -> str | None:
        """Return the most recent persisted audit record_hash (the chain head).

        Used to reload chain continuity on startup so the hash chain survives
        restarts. None when no audit rows exist.
        """
        rows = await self.load_audit_records()
        return rows[-1].get("record_hash") if rows else None

    @staticmethod
    def _tenant_audit_partition_key(tenant_id: str) -> str:
        return f"TENANT#{_require_tenant_id(tenant_id)}"

    @staticmethod
    def _tenant_audit_head_key(tenant_id: str) -> dict[str, str]:
        return {
            "PK": DynamoPersistence._tenant_audit_partition_key(tenant_id),
            "SK": "AUDIT#HEAD",
        }

    @staticmethod
    def _tenant_audit_record_item(
        tenant_id: str,
        record: dict,
    ) -> dict:
        tenant_id = _require_tenant_id(tenant_id)
        if record.get("tenant_id") != tenant_id:
            raise ValueError("audit record tenant_id does not match tenant_id")
        required = (
            "record_id",
            "timestamp",
            "prev_hash",
            "record_hash",
        )
        if any(not isinstance(record.get(name), str) or not record[name] for name in required):
            raise ValueError("audit record is missing required fields")
        return {
            **record,
            "PK": DynamoPersistence._tenant_audit_partition_key(tenant_id),
            "SK": (
                f"AUDIT#RECORD#{record['timestamp']}#"
                f"{record['record_id']}"
            ),
            "entity_type": "tenant_audit_record",
        }

    async def append_tenant_audit_record(
        self,
        tenant_id: str,
        record: dict,
        expected_prev_hash: str,
    ) -> bool:
        """Atomically append one record and compare-and-swap its tenant head.

        Returns ``False`` only when another writer advanced the head first.
        Every other persistence failure raises so an audit outage cannot be
        mistaken for a successful append.
        """
        tenant_id = _require_tenant_id(tenant_id)
        if not isinstance(expected_prev_hash, str) or not expected_prev_hash:
            raise ValueError("expected_prev_hash must be a non-empty string")
        item = self._tenant_audit_record_item(tenant_id, record)
        if item["prev_hash"] != expected_prev_hash:
            raise ValueError("audit record prev_hash does not match expected head")
        if not self._enabled:
            raise RuntimeError("tenant audit persistence is disabled")

        def _append() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError("atomic tenant audit persistence requires transactions")
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(item),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND "
                                "attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(
                                self._tenant_audit_head_key(tenant_id)
                            ),
                            "UpdateExpression": (
                                "SET #hash = :new_hash, "
                                "tenant_id = :tenant_id, "
                                "entity_type = :entity_type, "
                                "updated_at = :updated_at"
                            ),
                            "ConditionExpression": (
                                "#hash = :expected OR "
                                "(attribute_not_exists(#hash) "
                                "AND :expected = :genesis)"
                            ),
                            "ExpressionAttributeNames": {
                                "#hash": "record_hash",
                            },
                            "ExpressionAttributeValues": self._serialize_dynamo_map(
                                {
                                    ":new_hash": item["record_hash"],
                                    ":expected": expected_prev_hash,
                                    ":genesis": "genesis",
                                    ":tenant_id": tenant_id,
                                    ":entity_type": "tenant_audit_head",
                                    ":updated_at": item["timestamp"],
                                }
                            ),
                        }
                    },
                ],
                ClientRequestToken=self._api_key_transaction_token(
                    "audit-append"
                ),
            )

        try:
            await asyncio.to_thread(_append)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 1):
                return False
            self._record_write_failure("tenant audit record", item["record_id"])
            raise RuntimeError("tenant audit append failed") from exc
        return True

    async def get_latest_tenant_audit_hash(
        self,
        tenant_id: str,
    ) -> str | None:
        """Read one tenant's authoritative audit-chain head."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("tenant audit persistence is disabled")

        def _get() -> dict | None:
            response = self._get_table().get_item(
                Key=self._tenant_audit_head_key(tenant_id),
                ConsistentRead=True,
            )
            return response.get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error(
                "Failed to read tenant audit head for %s",
                tenant_id,
                exc_info=True,
            )
            raise RuntimeError("tenant audit head read failed") from exc
        if not item:
            return None
        head = item.get("record_hash")
        if not isinstance(head, str) or not head:
            raise RuntimeError("tenant audit head is malformed")
        return head

    async def load_tenant_audit_records(
        self,
        tenant_id: str,
        project_id: str | None = None,
    ) -> list[dict]:
        """Load one tenant's audit records in append order."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("tenant audit persistence is disabled")

        def _query() -> list[dict]:
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            condition = (
                Key("PK").eq(self._tenant_audit_partition_key(tenant_id))
                & Key("SK").begins_with("AUDIT#RECORD#")
            )
            items: list[dict] = []
            response = table.query(
                KeyConditionExpression=condition,
                ConsistentRead=True,
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.query(
                    KeyConditionExpression=condition,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                    ConsistentRead=True,
                )
                items.extend(response.get("Items", []))
            return items

        try:
            rows = await asyncio.to_thread(_query)
        except Exception as exc:
            logger.error(
                "Failed to load tenant audit records for %s",
                tenant_id,
                exc_info=True,
            )
            raise RuntimeError("tenant audit record load failed") from exc

        normalized = [
            self._convert_decimals_to_native(dict(row))
            for row in rows
        ]
        if project_id is not None:
            normalized = [
                row
                for row in normalized
                if row.get("project_id") == project_id
            ]
        normalized.sort(key=lambda row: row.get("SK", ""))
        return normalized

    # --- Durable model registry ---

    @staticmethod
    def serialize_model_registry(
        config: dict,
        *,
        revision: int,
    ) -> dict:
        """Serialize one authoritative, revisioned model registry document."""
        revision = _require_revision(
            revision,
            name="model registry revision",
        )
        if not isinstance(config, dict):
            raise ValueError("model registry config must be a mapping")
        snapshot = RoutingConfigSnapshot.from_config(
            config,
            revision=revision,
        )
        document = snapshot.document
        if len(document.encode("utf-8")) > _MODEL_REGISTRY_MAX_DOCUMENT_BYTES:
            raise ValueError("model registry exceeds the durable size limit")

        return {
            "PK": "MODEL_REGISTRY",
            "SK": "CONFIG",
            "entity_type": "model_registry",
            "schema_version": 1,
            "revision": revision,
            "document": document,
            "document_sha256": snapshot.sha256,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def serialize_signed_model_registry(
        snapshot: RoutingConfigSnapshot,
    ) -> dict:
        """Serialize one signed schema-v2 routing snapshot."""
        if not snapshot.is_signed:
            raise ValueError(
                "signed model registry serialization requires a signature"
            )
        if (
            len(snapshot.document.encode("utf-8"))
            > _MODEL_REGISTRY_MAX_DOCUMENT_BYTES
        ):
            raise ValueError(
                "model registry exceeds the durable size limit"
            )
        return {
            "PK": "MODEL_REGISTRY",
            "SK": "CONFIG",
            "entity_type": "model_registry",
            "schema_version": 2,
            "revision": snapshot.revision,
            "document": snapshot.document,
            "document_sha256": snapshot.sha256,
            "signature_schema": ROUTING_CONFIG_SIGNATURE_SCHEMA,
            "signing_key_arn": snapshot.signing_key_arn,
            "signing_algorithm": snapshot.signing_algorithm,
            "signature": snapshot.signature_b64,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def save_model_registry(
        self,
        config: dict,
        *,
        expected_revision: int,
    ) -> RoutingConfigSnapshot:
        """CAS and return one complete authenticated routing snapshot."""
        if not self._enabled:
            raise RuntimeError("model registry persistence is disabled")
        if self._routing_config_signing_mode == "verify":
            raise RuntimeError(
                "routing configuration persistence is verification-only"
            )
        expected = _require_revision(
            expected_revision,
            name="expected model registry revision",
        )
        next_revision = expected + 1
        snapshot = RoutingConfigSnapshot.from_config(
            config,
            revision=next_revision,
        )
        if (
            len(snapshot.document.encode("utf-8"))
            > _MODEL_REGISTRY_MAX_DOCUMENT_BYTES
        ):
            raise ValueError(
                "model registry exceeds the durable size limit"
            )
        if self._routing_config_signing_mode == "sign-verify":
            authenticator = self._routing_config_authenticator
            if authenticator is None:
                raise RuntimeError(
                    "routing configuration signing is not configured"
                )
            snapshot = await authenticator.sign(snapshot)
            item = self.serialize_signed_model_registry(snapshot)
        else:
            item = self.serialize_model_registry(
                config,
                revision=next_revision,
            )

        def _put() -> None:
            kwargs: dict = {"Item": item}
            if expected == 0:
                kwargs["ConditionExpression"] = (
                    "attribute_not_exists(PK) AND "
                    "attribute_not_exists(SK)"
                )
            else:
                kwargs.update(
                    {
                        "ConditionExpression": (
                            "entity_type = :entity_type AND "
                            "#revision = :expected AND "
                            "#schema_version = :expected_schema"
                        ),
                        "ExpressionAttributeNames": {
                            "#revision": "revision",
                            "#schema_version": "schema_version",
                        },
                        "ExpressionAttributeValues": {
                            ":entity_type": "model_registry",
                            ":expected": expected,
                            ":expected_schema": (
                                2
                                if self._routing_config_signing_mode
                                == "sign-verify"
                                else 1
                            ),
                        },
                    }
                )
            self._get_table().put_item(**kwargs)

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    "model registry changed concurrently"
                ) from exc
            self._record_write_failure("model registry", "CONFIG")
            raise RuntimeError("model registry write failed") from exc
        if snapshot.is_signed:
            self._authenticated_routing_snapshot = snapshot
        return snapshot

    @staticmethod
    def _deserialize_model_registry_item(
        item: dict,
    ) -> tuple[RoutingConfigSnapshot, int]:
        if item.get("entity_type") != "model_registry":
            raise RuntimeError("model registry row is malformed")
        schema_version = item.get("schema_version")
        if schema_version not in (1, 2):
            raise RuntimeError("model registry row is malformed")
        try:
            revision = _require_revision(
                item.get("revision"),
                name="model registry revision",
            )
        except ValueError as exc:
            raise RuntimeError("model registry row is malformed") from exc
        if revision < 1:
            raise RuntimeError("model registry row is malformed")
        document = item.get("document")
        digest = item.get("document_sha256")
        try:
            if schema_version == 1:
                snapshot = RoutingConfigSnapshot.from_document(
                    document,
                    revision=revision,
                    sha256=digest,
                )
            else:
                if (
                    item.get("signature_schema")
                    != ROUTING_CONFIG_SIGNATURE_SCHEMA
                ):
                    raise ValueError(
                        "routing configuration signature schema is invalid"
                    )
                snapshot = RoutingConfigSnapshot.from_document(
                    document,
                    revision=revision,
                    sha256=digest,
                    signing_key_arn=item.get("signing_key_arn"),
                    signing_algorithm=item.get("signing_algorithm"),
                    signature_b64=item.get("signature"),
                )
        except (TypeError, ValueError) as exc:
            message = str(exc)
            if "checksum" in message:
                raise RuntimeError(
                    "model registry document checksum mismatch"
                ) from exc
            raise RuntimeError("model registry row is malformed") from exc
        return snapshot, schema_version

    def _enforce_routing_revision(
        self,
        snapshot: RoutingConfigSnapshot,
        *,
        after_revision: int | None,
    ) -> None:
        if (
            after_revision is not None
            and snapshot.revision < after_revision
        ):
            raise RoutingConfigRollbackError(
                "routing configuration revision moved backward"
            )
        authenticated = self._authenticated_routing_snapshot
        if authenticated is None:
            return
        if snapshot.revision < authenticated.revision:
            raise RoutingConfigRollbackError(
                "routing configuration revision moved backward"
            )
        if (
            snapshot.revision == authenticated.revision
            and snapshot != authenticated
        ):
            raise RoutingConfigRollbackError(
                "routing configuration revision was rewritten"
            )

    async def _migrate_unsigned_model_registry(
        self,
        snapshot: RoutingConfigSnapshot,
    ) -> RoutingConfigSnapshot:
        authenticator = self._routing_config_authenticator
        if authenticator is None:
            raise RoutingConfigSignatureError(
                "routing configuration verification is not configured"
            )
        signed = await authenticator.sign(snapshot)
        item = self.serialize_signed_model_registry(signed)

        def _put() -> None:
            self._get_table().put_item(
                Item=item,
                ConditionExpression=(
                    "entity_type = :entity_type AND "
                    "#revision = :expected AND "
                    "schema_version = :legacy_schema AND "
                    "document_sha256 = :document_sha256"
                ),
                ExpressionAttributeNames={
                    "#revision": "revision",
                },
                ExpressionAttributeValues={
                    ":entity_type": "model_registry",
                    ":expected": snapshot.revision,
                    ":legacy_schema": 1,
                    ":document_sha256": snapshot.sha256,
                },
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    "model registry changed during signature migration"
                ) from exc
            self._record_write_failure(
                "model registry signature migration",
                "CONFIG",
            )
            raise RuntimeError(
                "model registry signature migration failed"
            ) from exc
        self._authenticated_routing_snapshot = signed
        return signed

    async def load_model_registry_snapshot(
        self,
        *,
        after_revision: int | None = None,
        _migration_retry: bool = True,
    ) -> RoutingConfigSnapshot | None:
        """Strongly load and authenticate the durable routing snapshot."""
        if not self._enabled:
            raise RuntimeError("model registry persistence is disabled")
        if after_revision is not None:
            after_revision = _require_revision(
                after_revision,
                name="current model registry revision",
            )

        def _get() -> dict | None:
            return self._get_table().get_item(
                Key={"PK": "MODEL_REGISTRY", "SK": "CONFIG"},
                ConsistentRead=True,
            ).get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error(
                "Failed to load the model registry from DynamoDB",
                exc_info=True,
            )
            raise RuntimeError("model registry read failed") from exc
        if item is None:
            return None
        snapshot, schema_version = self._deserialize_model_registry_item(
            item
        )
        self._enforce_routing_revision(
            snapshot,
            after_revision=after_revision,
        )
        if self._routing_config_signing_mode == "disabled":
            return snapshot
        if schema_version == 1:
            if self._routing_config_signing_mode != "sign-verify":
                raise RoutingConfigSignatureError(
                    "routing configuration snapshot is unsigned"
                )
            try:
                return await self._migrate_unsigned_model_registry(
                    snapshot
                )
            except PersistenceConflictError:
                if not _migration_retry:
                    raise
                return await self.load_model_registry_snapshot(
                    after_revision=after_revision,
                    _migration_retry=False,
                )

        authenticated = self._authenticated_routing_snapshot
        if authenticated == snapshot:
            return snapshot
        authenticator = self._routing_config_authenticator
        if authenticator is None:
            raise RoutingConfigSignatureError(
                "routing configuration verification is not configured"
            )
        await authenticator.verify(snapshot)
        self._authenticated_routing_snapshot = snapshot
        return snapshot

    def _config_version_operation(self) -> dict:
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": self._serialize_dynamo_map(
                    {"PK": "CONFIG#VERSION", "SK": "TOTAL"}
                ),
                "UpdateExpression": "ADD #version :one",
                "ExpressionAttributeNames": {"#version": "version"},
                "ExpressionAttributeValues": self._serialize_dynamo_map(
                    {":one": 1}
                ),
            }
        }

    async def save_project(
        self,
        project: Project,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """Conditionally replace a project and return its committed revision."""
        if not self._enabled:
            raise RuntimeError("project persistence is disabled")
        expected = _require_revision(
            project.revision
            if expected_revision is None
            else expected_revision,
            name="expected project revision",
        )
        next_revision = expected + 1
        staged = replace(project, revision=next_revision)
        item = self._convert_floats_to_decimal(
            self.serialize_project(staged)
        )
        names = {"#revision": "revision"}
        values = {
            ":entity_type": "project",
            ":project_id": project.project_id,
            ":expected": expected,
        }
        identity = (
            "attribute_exists(PK) AND attribute_exists(SK) AND "
            "entity_type = :entity_type AND project_id = :project_id"
        )
        revision_condition = (
            "(attribute_not_exists(#revision) OR #revision = :expected)"
            if expected == 0
            else "#revision = :expected"
        )
        condition = f"{identity} AND {revision_condition}"
        if project.tenant_id is not None:
            condition += " AND tenant_id = :tenant_id"
            values[":tenant_id"] = project.tenant_id

        def _put() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic project persistence requires transactions"
                )
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(item),
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(values)
                            ),
                        }
                    },
                    self._config_version_operation(),
                ],
                ClientRequestToken=self._api_key_transaction_token(
                    "project-save"
                ),
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    f"Project '{project.project_id}' changed concurrently"
                ) from exc
            self._record_write_failure("project", project.project_id)
            raise RuntimeError("project persistence failed") from exc
        return next_revision

    async def create_project(self, project: Project) -> int:
        """Conditionally create a revisioned project."""
        if not self._enabled:
            raise RuntimeError("project persistence is disabled")
        staged = replace(project, revision=1)
        item = self._convert_floats_to_decimal(
            self.serialize_project(staged)
        )
        condition = "attribute_not_exists(PK) AND attribute_not_exists(SK)"

        def _put() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic project persistence requires transactions"
                )
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(item),
                            "ConditionExpression": condition,
                        }
                    },
                    self._config_version_operation(),
                ],
                ClientRequestToken=self._api_key_transaction_token(
                    "project-create"
                ),
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise ValueError(
                    f"Project '{project.project_id}' already exists"
                ) from exc
            self._record_write_failure("project create", project.project_id)
            raise RuntimeError("project persistence failed") from exc
        return staged.revision

    async def get_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> Project | None:
        """Read a single project by id.

        A point read rather than a filtered scan: the caller is resolving one
        `{id}` path parameter, and `load_projects()` scans the whole table to
        answer it. Used by `AdminRouter` to resolve a project another instance
        created, which its startup-hydrated dict cannot know about.

        Returns None both for "no such project" and for a read failure — the
        caller renders either as `404`. A transient DynamoDB error therefore
        reads as a missing project, which is the same behaviour the rest of the
        admin API already has for a dropped read, and is why the exception is
        logged rather than swallowed silently.
        """
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            if tenant_id is None:
                key = {"PK": f"PROJECT#{project_id}", "SK": "PROJECT"}
            else:
                key = {
                    "PK": tenant_project_partition_key(tenant_id),
                    "SK": tenant_project_sort_key(project_id),
                }
            resp = table.get_item(Key=key, ConsistentRead=True)
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                project = self.deserialize_project(
                    self._convert_decimals_to_native(item)
                )
                if tenant_id is not None and project.tenant_id != tenant_id:
                    logger.error(
                        "Tenant project key returned mismatched owner project=%s "
                        "expected_tenant=%s actual_tenant=%s",
                        project_id,
                        tenant_id,
                        project.tenant_id,
                    )
                    return None
                return project
        except Exception:
            logger.warning(
                "Failed to load project %s for tenant %s from DynamoDB",
                project_id,
                tenant_id,
                exc_info=True,
            )
        return None

    async def load_projects(self) -> dict[str, Project]:
        """Load legacy globally keyed projects for the legacy control plane.

        Canonical tenant-owned projects cannot be represented by this
        ``dict[project_id, Project]`` contract: two tenants may intentionally use
        the same project id. Those rows are resolved only through
        ``get_project(project_id, tenant_id)`` and ``DynamoProjectRepository``.
        Mixing them into this map would let scan order decide which tenant's
        project survives.
        """
        if not self._enabled:
            return {}

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("project"),
                ConsistentRead=True,
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("project"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                    ConsistentRead=True,
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            projects = {}
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                project = self.deserialize_project(item)
                if project.tenant_id is not None:
                    continue
                projects[project.project_id] = project
            self._last_project_scan_failed = False
            return projects
        except Exception:
            logger.warning("Failed to load projects from DynamoDB", exc_info=True)
            self._last_project_scan_failed = True
            return {}

    async def list_tenant_projects(self, tenant_id: str) -> list[Project]:
        """Strongly read every project owned by one tenant partition."""
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if not self._enabled:
            return []

        def _query() -> list[dict]:
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            kwargs = {
                "KeyConditionExpression": (
                    Key("PK").eq(tenant_project_partition_key(tenant_id))
                    & Key("SK").begins_with("PROJECT#")
                ),
                "ConsistentRead": True,
            }
            items: list[dict] = []
            while True:
                response = table.query(**kwargs)
                items.extend(response.get("Items", []))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    return items
                kwargs["ExclusiveStartKey"] = last_key

        try:
            raw_items = await asyncio.to_thread(_query)
            projects = [
                self.deserialize_project(
                    self._convert_decimals_to_native(item)
                )
                for item in raw_items
            ]
            if any(project.tenant_id != tenant_id for project in projects):
                raise ValueError(
                    "tenant project query returned a mismatched owner"
                )
            return projects
        except Exception as exc:
            logger.warning(
                "Failed to list projects for tenant %s",
                tenant_id,
                exc_info=True,
            )
            raise RuntimeError("tenant project listing failed") from exc

    # --- Tenant Athena datasources ---

    @staticmethod
    def _tenant_datasource_sort_key(
        project_id: str,
        datasource_id: str,
    ) -> str:
        for name, value in (
            ("project_id", project_id),
            ("datasource_id", datasource_id),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 128
                or "#" in value
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(
                    f"{name} must be a delimiter-safe non-empty string"
                )
        return f"DATASOURCE#{project_id}#{datasource_id}"

    @classmethod
    def serialize_tenant_datasource(
        cls,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        document: dict,
        *,
        revision: int,
    ) -> dict:
        """Serialize one tenant datasource without storing credentials."""
        tenant_id = _require_tenant_id(tenant_id)
        if not isinstance(document, dict):
            raise ValueError("datasource document must be an object")
        unknown = set(document).difference(
            _TENANT_DATASOURCE_DOCUMENT_FIELDS
        )
        missing = _TENANT_DATASOURCE_DOCUMENT_FIELDS.difference(
            document
        )
        if unknown or missing:
            raise ValueError(
                "datasource document fields do not match the "
                "credential-free schema"
            )
        revision = _require_revision(
            revision,
            name="datasource revision",
        )
        return {
            "PK": tenant_project_partition_key(tenant_id),
            "SK": cls._tenant_datasource_sort_key(
                project_id,
                datasource_id,
            ),
            "entity_type": "athena_datasource",
            "tenant_id": tenant_id,
            "project_id": project_id,
            "datasource_id": datasource_id,
            "revision": revision,
            "document": json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def deserialize_tenant_datasource(item: dict) -> dict:
        raw = item.get("document")
        if not isinstance(raw, str):
            raise ValueError("datasource document is missing")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("datasource document must be an object")
        document.update(
            {
                "tenant_id": item["tenant_id"],
                "project_id": item["project_id"],
                "datasource_id": item["datasource_id"],
                "revision": int(item["revision"]),
            }
        )
        return document

    @staticmethod
    def _tenant_datasource_quota_key(
        tenant_id: str,
    ) -> dict[str, str]:
        return {
            "PK": tenant_project_partition_key(tenant_id),
            "SK": "QUOTA#ATHENA_DATASOURCES",
        }

    def _ensure_tenant_datasource_quota(
        self,
        table,
        tenant_id: str,
        *,
        max_datasources: int,
    ) -> None:
        """Initialize a legacy tenant's quota counter exactly once."""
        quota_key = self._tenant_datasource_quota_key(tenant_id)
        response = table.get_item(Key=quota_key, ConsistentRead=True)
        item = response.get("Item")
        if item is not None:
            if (
                item.get("entity_type") != "athena_datasource_quota"
                or item.get("tenant_id") != tenant_id
                or _require_revision(
                    item.get("datasource_count"),
                    name="datasource quota count",
                )
                < 0
            ):
                raise RuntimeError("datasource quota state is invalid")
            return

        from boto3.dynamodb.conditions import Key

        kwargs = {
            "KeyConditionExpression": (
                Key("PK").eq(tenant_project_partition_key(tenant_id))
                & Key("SK").begins_with("DATASOURCE#")
            ),
            "ConsistentRead": True,
            "Select": "COUNT",
        }
        count = 0
        while True:
            page = table.query(**kwargs)
            count += int(page.get("Count", 0))
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        try:
            table.put_item(
                Item={
                    **quota_key,
                    "entity_type": "athena_datasource_quota",
                    "tenant_id": tenant_id,
                    "datasource_count": count,
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) AND "
                    "attribute_not_exists(SK)"
                ),
            )
        except Exception as exc:
            if not self._api_key_condition_failed(exc, 0):
                raise
            winner = table.get_item(
                Key=quota_key,
                ConsistentRead=True,
            ).get("Item")
            if (
                not isinstance(winner, dict)
                or winner.get("entity_type")
                != "athena_datasource_quota"
                or winner.get("tenant_id") != tenant_id
            ):
                raise RuntimeError(
                    "datasource quota initialization raced invalid state"
                ) from exc

    async def save_tenant_datasource(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        document: dict,
        *,
        expected_revision: int,
        max_datasources: int = 500,
    ) -> int:
        """Conditionally create or replace one tenant datasource."""
        tenant_id = _require_tenant_id(tenant_id)
        expected_revision = _require_revision(
            expected_revision,
            name="expected datasource revision",
        )
        if (
            isinstance(max_datasources, bool)
            or not isinstance(max_datasources, int)
            or not 1 <= max_datasources <= 10_000
        ):
            raise ValueError(
                "max_datasources must be between 1 and 10000"
            )
        if not self._enabled:
            raise RuntimeError("datasource persistence is disabled")
        next_revision = expected_revision + 1

        def _put() -> None:
            item = self.serialize_tenant_datasource(
                tenant_id,
                project_id,
                datasource_id,
                document,
                revision=next_revision,
            )
            kwargs = {
                "Item": item,
                "ExpressionAttributeNames": {
                    "#revision": "revision",
                    "#entity_type": "entity_type",
                },
            }
            if expected_revision == 0:
                table = self._get_table()
                self._ensure_tenant_datasource_quota(
                    table,
                    tenant_id,
                    max_datasources=max_datasources,
                )
                client = getattr(
                    getattr(table, "meta", None),
                    "client",
                    None,
                )
                if client is None:
                    raise RuntimeError(
                        "atomic datasource quota requires transactions"
                    )
                client.transact_write_items(
                    TransactItems=[
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": self._serialize_dynamo_map(item),
                                "ConditionExpression": (
                                    "attribute_not_exists(PK) AND "
                                    "attribute_not_exists(SK)"
                                ),
                            }
                        },
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": self._serialize_dynamo_map(
                                    self._tenant_datasource_quota_key(
                                        tenant_id
                                    )
                                ),
                                "UpdateExpression": (
                                    "ADD datasource_count :one"
                                ),
                                "ConditionExpression": (
                                    "entity_type = :entity_type AND "
                                    "tenant_id = :tenant_id AND "
                                    "datasource_count < :limit"
                                ),
                                "ExpressionAttributeValues": (
                                    self._serialize_dynamo_map(
                                        {
                                            ":one": 1,
                                            ":entity_type": (
                                                "athena_datasource_quota"
                                            ),
                                            ":tenant_id": tenant_id,
                                            ":limit": max_datasources,
                                        }
                                    )
                                ),
                            }
                        },
                    ],
                    ClientRequestToken=self._api_key_transaction_token(
                        "datasource-create"
                    ),
                )
                return
            else:
                kwargs.update(
                    {
                        "ConditionExpression": (
                            "attribute_exists(PK) AND "
                            "attribute_exists(SK) AND "
                            "#entity_type = :entity_type AND "
                            "#revision = :expected"
                        ),
                        "ExpressionAttributeValues": {
                            ":entity_type": "athena_datasource",
                            ":expected": expected_revision,
                        },
                    }
                )
            self._get_table().put_item(**kwargs)

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if (
                expected_revision == 0
                and self._api_key_condition_failed(exc, 1)
            ):
                raise PersistenceQuotaExceededError(
                    "tenant datasource quota exceeded"
                ) from exc
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    "datasource changed concurrently"
                ) from exc
            self._record_write_failure(
                "Athena datasource",
                f"{tenant_id}/{project_id}/{datasource_id}",
            )
            raise RuntimeError("datasource write failed") from exc
        return next_revision

    async def get_tenant_datasource(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
    ) -> dict | None:
        """Strongly read one tenant/project datasource."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("datasource persistence is disabled")

        def _get() -> dict | None:
            response = self._get_table().get_item(
                Key={
                    "PK": tenant_project_partition_key(tenant_id),
                    "SK": self._tenant_datasource_sort_key(
                        project_id,
                        datasource_id,
                    ),
                },
                ConsistentRead=True,
            )
            return response.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                return None
            converted = self._convert_decimals_to_native(item)
            if (
                converted.get("entity_type") != "athena_datasource"
                or converted.get("tenant_id") != tenant_id
                or converted.get("project_id") != project_id
                or converted.get("datasource_id") != datasource_id
            ):
                raise ValueError(
                    "datasource row identity does not match its key"
                )
            return self.deserialize_tenant_datasource(converted)
        except Exception as exc:
            if isinstance(exc, ValueError):
                logger.error(
                    "Malformed datasource row tenant=%s project=%s id=%s",
                    tenant_id,
                    project_id,
                    datasource_id,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "Failed to load datasource tenant=%s project=%s id=%s",
                    tenant_id,
                    project_id,
                    datasource_id,
                    exc_info=True,
                )
            raise RuntimeError("datasource read failed") from exc

    async def list_tenant_datasources(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        limit: int = 50,
        exclusive_start_key: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Strongly read one bounded datasource page."""
        tenant_id = _require_tenant_id(tenant_id)
        if project_id is not None and (
            not isinstance(project_id, str) or not project_id.strip()
        ):
            raise ValueError("project_id must be None or non-empty")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("datasource page limit must be between 1 and 100")
        prefix = (
            f"DATASOURCE#{project_id}#"
            if project_id is not None
            else "DATASOURCE#"
        )
        if exclusive_start_key is not None and (
            not isinstance(exclusive_start_key, str)
            or not exclusive_start_key.startswith(prefix)
        ):
            raise ValueError(
                "datasource start key does not match the requested scope"
            )
        if not self._enabled:
            raise RuntimeError("datasource persistence is disabled")

        def _query() -> tuple[list[dict], str | None]:
            from boto3.dynamodb.conditions import Key

            kwargs = {
                "KeyConditionExpression": (
                    Key("PK").eq(tenant_project_partition_key(tenant_id))
                    & Key("SK").begins_with(prefix)
                ),
                "ConsistentRead": True,
                "Limit": limit,
            }
            if exclusive_start_key is not None:
                kwargs["ExclusiveStartKey"] = {
                    "PK": tenant_project_partition_key(tenant_id),
                    "SK": exclusive_start_key,
                }
            response = self._get_table().query(**kwargs)
            last_key = response.get("LastEvaluatedKey")
            next_key = (
                last_key.get("SK")
                if isinstance(last_key, dict)
                else None
            )
            if next_key is not None and (
                not isinstance(next_key, str)
                or not next_key.startswith(prefix)
            ):
                raise ValueError(
                    "datasource query returned an invalid continuation key"
                )
            return response.get("Items", []), next_key

        try:
            items, next_key = await asyncio.to_thread(_query)
            documents = []
            for item in items:
                converted = self._convert_decimals_to_native(item)
                if (
                    converted.get("entity_type")
                    != "athena_datasource"
                    or converted.get("tenant_id") != tenant_id
                    or (
                        project_id is not None
                        and converted.get("project_id") != project_id
                    )
                ):
                    raise ValueError(
                        "datasource query returned a mismatched owner"
                    )
                documents.append(
                    self.deserialize_tenant_datasource(converted)
                )
            return documents, next_key
        except Exception as exc:
            logger.warning(
                "Failed to list datasources tenant=%s project=%s",
                tenant_id,
                project_id,
                exc_info=True,
            )
            raise RuntimeError("datasource listing failed") from exc

    async def delete_tenant_datasource(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        *,
        expected_revision: int,
        max_datasources: int = 500,
    ) -> None:
        """Conditionally remove one datasource after an explicit CAS check."""
        tenant_id = _require_tenant_id(tenant_id)
        expected_revision = _require_revision(
            expected_revision,
            name="expected datasource revision",
        )
        if expected_revision == 0:
            raise ValueError(
                "expected datasource revision must be positive"
            )
        if (
            isinstance(max_datasources, bool)
            or not isinstance(max_datasources, int)
            or not 1 <= max_datasources <= 10_000
        ):
            raise ValueError(
                "max_datasources must be between 1 and 10000"
            )
        if not self._enabled:
            raise RuntimeError("datasource persistence is disabled")

        def _delete() -> None:
            table = self._get_table()
            self._ensure_tenant_datasource_quota(
                table,
                tenant_id,
                max_datasources=max_datasources,
            )
            client = getattr(
                getattr(table, "meta", None),
                "client",
                None,
            )
            if client is None:
                raise RuntimeError(
                    "atomic datasource quota requires transactions"
                )
            client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(
                                {
                                    "PK": (
                                        tenant_project_partition_key(
                                            tenant_id
                                        )
                                    ),
                                    "SK": (
                                        self._tenant_datasource_sort_key(
                                            project_id,
                                            datasource_id,
                                        )
                                    ),
                                }
                            ),
                            "ConditionExpression": (
                                "attribute_exists(PK) AND "
                                "attribute_exists(SK) AND "
                                "#entity_type = :entity_type AND "
                                "#revision = :expected"
                            ),
                            "ExpressionAttributeNames": {
                                "#entity_type": "entity_type",
                                "#revision": "revision",
                            },
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(
                                    {
                                        ":entity_type": (
                                            "athena_datasource"
                                        ),
                                        ":expected": expected_revision,
                                    }
                                )
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(
                                self._tenant_datasource_quota_key(
                                    tenant_id
                                )
                            ),
                            "UpdateExpression": (
                                "ADD datasource_count :minus_one"
                            ),
                            "ConditionExpression": (
                                "entity_type = :entity_type AND "
                                "tenant_id = :tenant_id AND "
                                "datasource_count > :zero"
                            ),
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(
                                    {
                                        ":minus_one": -1,
                                        ":zero": 0,
                                        ":entity_type": (
                                            "athena_datasource_quota"
                                        ),
                                        ":tenant_id": tenant_id,
                                    }
                                )
                            ),
                        }
                    },
                ],
                ClientRequestToken=self._api_key_transaction_token(
                    "datasource-delete"
                ),
            )

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:
            if any(
                self._api_key_condition_failed(exc, index)
                for index in (0, 1)
            ):
                raise PersistenceConflictError(
                    "datasource changed concurrently or no longer exists"
                ) from exc
            self._record_write_failure(
                "Athena datasource delete",
                f"{tenant_id}/{project_id}/{datasource_id}",
            )
            raise RuntimeError("datasource delete failed") from exc

    async def _save_user_config_revision(
        self,
        user_id: str,
        config: dict,
        *,
        tenant_id: str | None,
        expected_revision: int | None,
    ) -> int:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be a non-empty string")
        if tenant_id is not None:
            tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("user config persistence is disabled")
        expected = _require_revision(
            config.get("revision", 0)
            if expected_revision is None
            else expected_revision,
            name="expected user config revision",
        )
        next_revision = expected + 1
        staged = {**config, "revision": next_revision}
        item = (
            self.serialize_user_config(user_id, staged)
            if tenant_id is None
            else self.serialize_tenant_user_config(
                tenant_id,
                user_id,
                staged,
            )
        )
        item = self._convert_floats_to_decimal(item)
        names = {"#revision": "revision"}
        values = {
            ":entity_type": (
                "user_config"
                if tenant_id is None
                else "tenant_user_config"
            ),
            ":user_id": user_id,
            ":expected": expected,
        }
        identity = (
            "entity_type = :entity_type AND user_id = :user_id"
        )
        if tenant_id is not None:
            identity += " AND tenant_id = :tenant_id"
            values[":tenant_id"] = tenant_id
        if expected == 0:
            condition = (
                "(attribute_not_exists(PK) AND attribute_not_exists(SK)) "
                f"OR ({identity} AND "
                "(attribute_not_exists(#revision) "
                "OR #revision = :expected))"
            )
        else:
            condition = f"{identity} AND #revision = :expected"

        def _put() -> None:
            table = self._get_table()
            if tenant_id is not None:
                table.put_item(
                    Item=item,
                    ConditionExpression=condition,
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
                return
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic user config persistence requires transactions"
                )
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(item),
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(values)
                            ),
                        }
                    },
                    self._config_version_operation(),
                ],
                ClientRequestToken=self._api_key_transaction_token(
                    "user-config"
                ),
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    f"User config for '{user_id}' changed concurrently"
                ) from exc
            resource = (
                user_id
                if tenant_id is None
                else f"{tenant_id}/{user_id}"
            )
            self._record_write_failure("user config", resource)
            raise RuntimeError("user config write failed") from exc
        return next_revision

    async def save_user_config(
        self,
        user_id: str,
        config: dict,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """Conditionally create or replace a legacy user configuration."""
        return await self._save_user_config_revision(
            user_id,
            config,
            tenant_id=None,
            expected_revision=expected_revision,
        )

    async def save_tenant_user_config(
        self,
        tenant_id: str,
        user_id: str,
        config: dict,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """Conditionally create or replace a tenant user configuration."""
        return await self._save_user_config_revision(
            user_id,
            config,
            tenant_id=tenant_id,
            expected_revision=expected_revision,
        )

    async def get_tenant_user_config(
        self,
        tenant_id: str,
        user_id: str,
    ) -> dict | None:
        """Strongly read one tenant user config, raising on store outages."""
        tenant_id = _require_tenant_id(tenant_id)
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be a non-empty string")
        if not self._enabled:
            raise RuntimeError("tenant user config persistence is disabled")

        def _get() -> dict | None:
            response = self._get_table().get_item(
                Key={
                    "PK": f"TENANT#{tenant_id}",
                    "SK": f"USER_CONFIG#{user_id}",
                },
                ConsistentRead=True,
            )
            return response.get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error(
                "Failed to load tenant user config tenant=%s user=%s",
                tenant_id,
                user_id,
                exc_info=True,
            )
            raise RuntimeError("tenant user config read failed") from exc
        if not item:
            return None
        if (
            item.get("tenant_id") != tenant_id
            or item.get("user_id") != user_id
            or item.get("entity_type") != "tenant_user_config"
        ):
            raise RuntimeError("tenant user config row is malformed")
        _, config = self.deserialize_user_config(
            self._convert_decimals_to_native(item)
        )
        return config

    async def bump_config_version(self) -> int | None:
        """Atomically increment the shared config version, returning the new one.

        One counter covers both projects and user configs rather than one each.
        The refresh re-reads both scans together, so a second counter would only
        let it skip one of two scans it is already making — and two counters can
        disagree about ordering, which one cannot.
        """
        if not self._enabled:
            return None

        def _add():
            resp = self._get_table().update_item(
                Key={"PK": "CONFIG#VERSION", "SK": "TOTAL"},
                UpdateExpression="ADD #v :one",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":one": Decimal("1")},
                ReturnValues="UPDATED_NEW",
            )
            return resp.get("Attributes", {}).get("version")

        try:
            version = await asyncio.to_thread(_add)
            return int(version) if version is not None else None
        except Exception:
            self._record_write_failure("config version", "TOTAL")
            return None

    async def get_config_version(self) -> int | None:
        """Read the shared config version, or None if it could not be read.

        Absent returns 0 for the same reason ``get_policy_version`` does: no
        config has ever been written through the API is a known state, and every
        gateway starts in it. Conflating it with unreadable would mean a
        seed-only deployment never records a successful check and re-reads this
        counter on every request forever.
        """
        if not self._enabled:
            return None

        def _get():
            resp = self._get_table().get_item(
                Key={"PK": "CONFIG#VERSION", "SK": "TOTAL"},
                ConsistentRead=True,
            )
            item = resp.get("Item")
            return item.get("version", 0) if item else 0

        try:
            return int(await asyncio.to_thread(_get))
        except Exception:
            logger.warning("Failed to read the shared config version", exc_info=True)
            return None

    async def load_projects_or_none(self) -> dict[str, Project] | None:
        """Like ``load_projects``, but None on failure instead of ``{}``.

        ``load_projects`` returns ``{}`` on failure so a Dynamo outage cannot
        block startup. On a live refresh that trade inverts: adopting ``{}``
        would drop every project the fleet knows about, and an unresolved project
        means no budget gate and no allowed-models list.
        """
        if not self._enabled:
            return None
        # Reset here rather than relying on the loader to clear it, so this cannot
        # read a flag left set by an earlier caller. That makes the loader's own
        # success-path clear redundant; it is kept because the flag is public
        # enough that a future reader of it should not have to know which of the
        # two resets it depends on.
        self._last_project_scan_failed = False
        projects = await self.load_projects()
        return None if self._last_project_scan_failed else projects

    async def load_user_configs_or_none(self) -> dict[str, dict] | None:
        """Like ``load_user_configs``, but None on failure instead of ``{}``.

        Same reasoning as ``load_projects_or_none``: an adopted empty result
        clears every per-user budget limit and model restriction in the fleet.
        """
        if not self._enabled:
            return None
        self._last_user_config_scan_failed = False
        configs = await self.load_user_configs()
        return None if self._last_user_config_scan_failed else configs

    async def load_user_configs(self) -> dict[str, dict]:
        """Scan DynamoDB for all user configs and deserialize them."""
        if not self._enabled:
            return {}

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("user_config"),
                ConsistentRead=True,
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("user_config"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                    ConsistentRead=True,
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            configs = {}
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                user_id, config = self.deserialize_user_config(item)
                configs[user_id] = config
            self._last_user_config_scan_failed = False
            return configs
        except Exception:
            logger.warning("Failed to load user configs from DynamoDB", exc_info=True)
            self._last_user_config_scan_failed = True
            return {}

    # --- FeedbackRecord serialization ---

    @staticmethod
    def serialize_feedback_record(record: FeedbackRecord) -> dict:
        ts_iso = record.timestamp.isoformat()
        return {
            "PK": f"FEEDBACK#{record.request_id}",
            "SK": f"FEEDBACK#{ts_iso}",
            "entity_type": "feedback_record",
            "request_id": record.request_id,
            "timestamp": ts_iso,
            "task_type": record.task_type,
            "confidence": record.confidence,
            "selected_model": record.selected_model,
            "benchmark_score": record.benchmark_score,
        }

    @staticmethod
    def deserialize_feedback_record(item: dict) -> FeedbackRecord:
        return FeedbackRecord(
            request_id=item["request_id"],
            timestamp=datetime.fromisoformat(item["timestamp"]),
            task_type=item["task_type"],
            confidence=float(item["confidence"]),
            selected_model=item["selected_model"],
            benchmark_score=float(item["benchmark_score"]),
        )

    async def save_feedback_record(self, record: FeedbackRecord) -> None:
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_feedback_record(record)
            item = self._convert_floats_to_decimal(item)
            table.put_item(Item=item)

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("feedback record", record.request_id)

    async def load_feedback_records(self) -> list[FeedbackRecord]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("feedback_record")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("feedback_record"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            records = []
            for item in raw_items:
                item = self._convert_decimals_to_native(item)
                records.append(self.deserialize_feedback_record(item))
            return records
        except Exception:
            logger.warning("Failed to load feedback records from DynamoDB", exc_info=True)
            return []

    # --- APIKey persistence ---

    @staticmethod
    def _api_key_primary_key(
        key_id: str,
        tenant_id: str | None,
    ) -> dict[str, str]:
        if tenant_id is None:
            return {"PK": f"APIKEY#{key_id}", "SK": "APIKEY"}
        return {
            "PK": f"TENANT#{tenant_id}#APIKEY#{key_id}",
            "SK": "METADATA",
        }

    @staticmethod
    def _api_key_project_partition(
        project_id: str,
        tenant_id: str | None,
    ) -> str:
        if tenant_id is None:
            return f"PROJECT#{project_id}"
        return f"TENANT#{tenant_id}#PROJECT#{project_id}"

    @staticmethod
    def _api_key_epoch_key(tenant_id: str | None) -> dict[str, str]:
        if tenant_id is None:
            return {"PK": "REVOCATION", "SK": "EPOCH"}
        return {"PK": f"TENANT#{tenant_id}", "SK": "AUTHZ#EPOCH"}

    @staticmethod
    def _serialize_dynamo_map(values: dict) -> dict:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        return {name: serializer.serialize(value) for name, value in values.items()}

    @staticmethod
    def _api_key_transaction_token(_action: str) -> str:
        import uuid

        # Unique per application attempt, but stable across botocore's retries
        # of this one request. Reusing a key-derived token would make a second
        # concurrent call look like an idempotent success instead of a conflict.
        return str(uuid.uuid4())

    @staticmethod
    def _api_key_condition_failed(exc: Exception, item_index: int) -> bool:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error")
        if not isinstance(error, dict):
            return False
        if error.get("Code") == "ConditionalCheckFailedException":
            return True
        if error.get("Code") != "TransactionCanceledException":
            return False
        reasons = response.get("CancellationReasons")
        return (
            isinstance(reasons, list)
            and len(reasons) > item_index
            and isinstance(reasons[item_index], dict)
            and reasons[item_index].get("Code") == "ConditionalCheckFailed"
        )

    @staticmethod
    def serialize_api_key(key: APIKey) -> dict:
        item = {
            **DynamoPersistence._api_key_primary_key(
                key.key_id,
                key.tenant_id,
            ),
            "entity_type": "api_key",
            "key_id": key.key_id,
            "key_hash": key.key_hash,
            "project_id": key.project_id,
            "name": key.name,
            "scopes": json.dumps(key.scopes),
            "created_by": key.created_by,
            "created_at": key.created_at.isoformat(),
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "revoked": key.revoked,
            "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
            "revoked_by": key.revoked_by,
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        }
        if key.tenant_id is not None:
            item["tenant_id"] = key.tenant_id
        return item

    @staticmethod
    def deserialize_api_key(item: dict) -> APIKey:
        scopes_raw = item.get("scopes", "[]")
        scopes = json.loads(scopes_raw) if isinstance(scopes_raw, str) else scopes_raw
        return APIKey(
            key_id=item["key_id"],
            key_hash=item["key_hash"],
            project_id=item["project_id"],
            name=item["name"],
            scopes=scopes,
            created_by=item["created_by"],
            tenant_id=item.get("tenant_id"),
            created_at=datetime.fromisoformat(item["created_at"]),
            expires_at=datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None,
            revoked=bool(item.get("revoked", False)),
            revoked_at=datetime.fromisoformat(item["revoked_at"]) if item.get("revoked_at") else None,
            revoked_by=item.get("revoked_by"),
            last_used_at=datetime.fromisoformat(item["last_used_at"]) if item.get("last_used_at") else None,
        )

    @classmethod
    def _api_key_issue_rows(cls, key: APIKey) -> list[dict]:
        item = cls.serialize_api_key(key)
        primary_key = cls._api_key_primary_key(key.key_id, key.tenant_id)
        hash_lookup = {
            "PK": f"APIKEY_HASH#{key.key_hash}",
            "SK": "LOOKUP",
            "entity_type": "api_key_hash_lookup",
            "key_id": key.key_id,
            "key_pk": primary_key["PK"],
            "key_sk": primary_key["SK"],
        }
        project_edge = {
            "PK": cls._api_key_project_partition(
                key.project_id,
                key.tenant_id,
            ),
            "SK": f"APIKEY#{key.key_id}",
            "entity_type": "project_api_key",
            "key_id": key.key_id,
            "project_id": key.project_id,
            "key_pk": primary_key["PK"],
            "key_sk": primary_key["SK"],
        }
        if key.tenant_id is not None:
            hash_lookup["tenant_id"] = key.tenant_id
            project_edge["tenant_id"] = key.tenant_id
        return [item, hash_lookup, project_edge]

    @staticmethod
    def _validate_api_key_principal(key: APIKey, principal: Principal) -> None:
        from src.gateway.auth.principal import API_KEY_ISSUER
        from src.gateway.models import AuthMethod, MembershipStatus, TenantRole

        if key.tenant_id is None:
            raise ValueError("canonical API-key principal requires tenant_id")
        if (
            principal.tenant_id != key.tenant_id
            or principal.subject != key.key_id
            or principal.issuer != API_KEY_ISSUER
            or principal.auth_method is not AuthMethod.API_KEY
            or principal.membership_status is not MembershipStatus.ACTIVE
            or principal.roles != frozenset({TenantRole.SERVICE})
            or principal.project_ids != frozenset({key.project_id})
            or principal.scopes != frozenset(key.scopes)
            or principal.credential_id != key.key_id
            or principal.authorization_version != 1
        ):
            raise ValueError("API-key principal does not match credential authority")

    @staticmethod
    def _serialize_principal(principal: Principal) -> dict:
        from src.gateway.auth.dynamo_principal_repository import (
            DynamoPrincipalRepository,
        )

        return DynamoPrincipalRepository.serialize(principal)

    @staticmethod
    def _api_key_principal_key(key: APIKey) -> dict[str, str]:
        from src.gateway.auth.dynamo_principal_repository import (
            identity_partition_key,
            membership_sort_key,
        )
        from src.gateway.auth.principal import API_KEY_ISSUER

        if key.tenant_id is None:
            raise ValueError("canonical API-key principal requires tenant_id")
        return {
            "PK": identity_partition_key(API_KEY_ISSUER, key.key_id),
            "SK": membership_sort_key(key.tenant_id),
        }

    def _api_key_put_operations(self, rows: list[dict]) -> list[dict]:
        condition = "attribute_not_exists(PK) AND attribute_not_exists(SK)"
        return [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": self._serialize_dynamo_map(row),
                    "ConditionExpression": condition,
                }
            }
            for row in rows
        ]

    def _api_key_revoke_operation(self, key: APIKey) -> dict:
        if (
            not key.revoked
            or key.revoked_at is None
            or not isinstance(key.revoked_by, str)
            or not key.revoked_by.strip()
        ):
            raise ValueError(
                "revocation requires revoked=True, revoked_at, and revoked_by"
            )
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": self._serialize_dynamo_map(
                    self._api_key_primary_key(key.key_id, key.tenant_id)
                ),
                "UpdateExpression": (
                    "SET revoked = :true, revoked_at = :revoked_at, "
                    "revoked_by = :revoked_by"
                ),
                "ConditionExpression": (
                    "attribute_exists(PK) AND attribute_exists(SK) "
                    "AND key_hash = :key_hash "
                    "AND (attribute_not_exists(revoked) OR revoked = :false)"
                ),
                "ExpressionAttributeValues": self._serialize_dynamo_map(
                    {
                        ":true": True,
                        ":false": False,
                        ":revoked_at": key.revoked_at.isoformat(),
                        ":revoked_by": key.revoked_by,
                        ":key_hash": key.key_hash,
                    }
                ),
            }
        }

    def _api_key_principal_deprovision_operation(self, key: APIKey) -> dict:
        from src.gateway.auth.principal import API_KEY_ISSUER
        from src.gateway.models import AuthMethod, MembershipStatus

        if key.tenant_id is None:
            raise ValueError("canonical API-key principal requires tenant_id")
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": self._serialize_dynamo_map(
                    self._api_key_principal_key(key)
                ),
                "UpdateExpression": (
                    "SET membership_status = :deprovisioned, "
                    "authorization_version = authorization_version + :one"
                ),
                "ConditionExpression": (
                    "attribute_exists(PK) AND attribute_exists(SK) "
                    "AND entity_type = :entity_type "
                    "AND tenant_id = :tenant_id "
                    "AND subject = :key_id "
                    "AND issuer = :issuer "
                    "AND auth_method = :auth_method "
                    "AND credential_id = :key_id "
                    "AND membership_status = :active"
                ),
                "ExpressionAttributeValues": self._serialize_dynamo_map(
                    {
                        ":deprovisioned": MembershipStatus.DEPROVISIONED.value,
                        ":one": 1,
                        ":entity_type": "tenant_principal",
                        ":tenant_id": key.tenant_id,
                        ":key_id": key.key_id,
                        ":issuer": API_KEY_ISSUER,
                        ":auth_method": AuthMethod.API_KEY.value,
                        ":active": MembershipStatus.ACTIVE.value,
                    }
                ),
            }
        }

    def _api_key_epoch_operation(self, tenant_id: str | None) -> dict:
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": self._serialize_dynamo_map(
                    self._api_key_epoch_key(tenant_id)
                ),
                "UpdateExpression": "ADD #epoch :one",
                "ExpressionAttributeNames": {"#epoch": "epoch"},
                "ExpressionAttributeValues": self._serialize_dynamo_map(
                    {":one": 1}
                ),
            }
        }

    async def save_api_key(self, key: APIKey) -> None:
        await self._save_api_key(key)

    async def save_api_key_with_principal(
        self,
        key: APIKey,
        principal: Principal,
    ) -> None:
        """Atomically create a tenant key and its canonical service principal."""
        self._validate_api_key_principal(key, principal)
        await self._save_api_key(key, principal)

    async def _save_api_key(
        self,
        key: APIKey,
        principal: Principal | None = None,
    ) -> None:
        if not self._enabled:
            return

        def _put() -> None:
            table = self._get_table()
            rows = self._api_key_issue_rows(key)
            if principal is not None:
                rows.append(self._serialize_principal(principal))
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                # Compatibility for existing in-process table fakes. Every
                # boto3 DynamoDB Table has meta.client and therefore cannot take
                # this non-transactional branch.
                for row in rows:
                    table.put_item(Item=row)
                return
            client.transact_write_items(
                TransactItems=self._api_key_put_operations(rows),
                ClientRequestToken=self._api_key_transaction_token("issue"),
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise RuntimeError("API key already exists") from exc
            if self._api_key_condition_failed(exc, 1):
                raise RuntimeError("API key hash already exists") from exc
            if self._api_key_condition_failed(exc, 2):
                raise RuntimeError("API key project edge already exists") from exc
            if principal is not None and self._api_key_condition_failed(exc, 3):
                raise RuntimeError("API key principal already exists") from exc
            self._record_write_failure("API key", key.key_id)
            raise RuntimeError("API key transaction failed") from exc

    async def get_api_key_by_hash(self, key_hash: str) -> APIKey | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            lookup_response = table.get_item(
                Key={"PK": f"APIKEY_HASH#{key_hash}", "SK": "LOOKUP"},
                ConsistentRead=True,
            )
            lookup = lookup_response.get("Item")
            if not lookup:
                return None
            key_id = lookup.get("key_id")
            tenant_id = lookup.get("tenant_id")
            if not isinstance(key_id, str) or not key_id:
                raise ValueError("API key hash lookup has no key_id")
            if tenant_id is not None and (
                not isinstance(tenant_id, str) or not tenant_id
            ):
                raise ValueError("API key hash lookup has an invalid tenant_id")
            key = self._api_key_primary_key(key_id, tenant_id)
            if (
                ("key_pk" in lookup and lookup["key_pk"] != key["PK"])
                or ("key_sk" in lookup and lookup["key_sk"] != key["SK"])
            ):
                raise ValueError("API key hash lookup points outside its namespace")
            key_response = table.get_item(Key=key, ConsistentRead=True)
            item = key_response.get("Item")
            if item is not None and (
                item.get("PK") != key["PK"]
                or item.get("SK") != key["SK"]
                or item.get("key_id") != key_id
                or item.get("tenant_id") != tenant_id
            ):
                raise ValueError(
                    "API key hash lookup returned a mismatched key row"
                )
            return item

        try:
            item = await asyncio.to_thread(_get)
            if item:
                key = self.deserialize_api_key(
                    self._convert_decimals_to_native(item)
                )
                if key.key_hash != key_hash:
                    raise ValueError("API key hash lookup returned a mismatched key")
                return key
        except Exception:
            logger.warning("Failed to lookup API key by hash", exc_info=True)
        return None

    async def get_api_key(
        self,
        key_id: str,
        tenant_id: str | None = None,
    ) -> APIKey | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(
                Key=self._api_key_primary_key(key_id, tenant_id),
                ConsistentRead=True,
            )
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                key = self.deserialize_api_key(
                    self._convert_decimals_to_native(item)
                )
                if key.key_id != key_id or key.tenant_id != tenant_id:
                    raise ValueError("API key row does not match its storage key")
                return key
        except Exception:
            logger.warning(
                "Failed to get API key %s for tenant %s",
                key_id,
                tenant_id,
                exc_info=True,
            )
            if tenant_id is not None:
                raise RuntimeError("Tenant API key lookup failed")
        return None

    async def list_api_keys_for_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> list[APIKey]:
        if not self._enabled:
            return []

        def _query():
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            resp = table.query(
                KeyConditionExpression=Key("PK").eq(
                    self._api_key_project_partition(project_id, tenant_id)
                )
                & Key("SK").begins_with("APIKEY#"),
                ConsistentRead=True,
            )
            keys = []
            for edge in resp.get("Items", []):
                key_id = edge.get("key_id")
                if not isinstance(key_id, str) or not key_id:
                    raise ValueError("project API key edge has no key_id")
                key = self._api_key_primary_key(key_id, tenant_id)
                if (
                    ("key_pk" in edge and edge["key_pk"] != key["PK"])
                    or ("key_sk" in edge and edge["key_sk"] != key["SK"])
                ):
                    raise ValueError(
                        "project API key edge points outside its namespace"
                    )
                key_resp = table.get_item(Key=key, ConsistentRead=True)
                item = key_resp.get("Item")
                if item:
                    keys.append(item)
            return keys

        try:
            items = await asyncio.to_thread(_query)
            keys = [
                self.deserialize_api_key(self._convert_decimals_to_native(item))
                for item in items
            ]
            if any(
                key.project_id != project_id or key.tenant_id != tenant_id
                for key in keys
            ):
                raise ValueError("project API key edge returned a mismatched key")
            return keys
        except Exception:
            logger.warning(
                "Failed to list API keys for project %s in tenant %s",
                project_id,
                tenant_id,
                exc_info=True,
            )
            if tenant_id is not None:
                raise RuntimeError("Tenant API key listing failed")
            return []

    async def update_api_key(self, key: APIKey) -> None:
        if not key.revoked:
            raise ValueError("only revocation updates are supported")
        if not await self.revoke_api_key(key):
            raise RuntimeError("API key is missing or already revoked")

    async def revoke_api_key(self, key: APIKey) -> bool:
        """Atomically revoke a key and advance its cache-invalidation epoch."""
        return await self._revoke_api_key(key, include_principal=False)

    async def revoke_api_key_with_principal(self, key: APIKey) -> bool:
        """Atomically revoke a tenant key and deprovision its principal."""
        if key.tenant_id is None:
            raise ValueError("canonical API-key principal requires tenant_id")
        return await self._revoke_api_key(key, include_principal=True)

    async def _revoke_api_key(
        self,
        key: APIKey,
        *,
        include_principal: bool,
    ) -> bool:
        if not self._enabled:
            return False
        if (
            not key.revoked
            or key.revoked_at is None
            or not isinstance(key.revoked_by, str)
            or not key.revoked_by.strip()
        ):
            raise ValueError(
                "revocation requires revoked=True, revoked_at, and revoked_by"
            )

        def _revoke() -> None:
            table = self._get_table()
            epoch_key = self._api_key_epoch_key(key.tenant_id)
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                if include_principal:
                    raise RuntimeError(
                        "canonical API-key revocation requires transactions"
                    )
                # See save_api_key: this preserves old table-only test doubles;
                # a real DynamoDB Table always takes the transaction below.
                table.put_item(Item=self.serialize_api_key(key))
                update_item = getattr(table, "update_item", None)
                if update_item is not None:
                    update_item(
                        Key=epoch_key,
                        UpdateExpression="ADD #epoch :one",
                        ExpressionAttributeNames={"#epoch": "epoch"},
                        ExpressionAttributeValues={":one": 1},
                    )
                return
            operations = [self._api_key_revoke_operation(key)]
            if include_principal:
                operations.append(
                    self._api_key_principal_deprovision_operation(key)
                )
            operations.append(self._api_key_epoch_operation(key.tenant_id))
            client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=self._api_key_transaction_token("revoke"),
            )

        try:
            await asyncio.to_thread(_revoke)
            return True
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                return False
            if include_principal and self._api_key_condition_failed(exc, 1):
                raise RuntimeError(
                    "API key principal is missing or inactive"
                ) from exc
            self._record_write_failure("API key revocation", key.key_id)
            raise RuntimeError("API key revocation transaction failed") from exc

    async def rotate_api_key_with_principal(
        self,
        revoked_key: APIKey,
        replacement: APIKey,
        replacement_principal: Principal,
    ) -> bool:
        """Atomically revoke one tenant key and create its replacement."""
        if revoked_key.tenant_id is None:
            raise ValueError("canonical API-key rotation requires tenant_id")
        if (
            not revoked_key.revoked
            or revoked_key.revoked_at is None
            or not isinstance(revoked_key.revoked_by, str)
            or not revoked_key.revoked_by.strip()
        ):
            raise ValueError(
                "rotation requires a revoked source key with actor attribution"
            )
        if replacement.revoked or replacement.revoked_at is not None:
            raise ValueError("replacement API key must be active")
        if (
            replacement.tenant_id != revoked_key.tenant_id
            or replacement.project_id != revoked_key.project_id
            or replacement.name != revoked_key.name
            or replacement.scopes != revoked_key.scopes
        ):
            raise ValueError("replacement API key must preserve credential grants")
        if (
            replacement.key_id == revoked_key.key_id
            or replacement.key_hash == revoked_key.key_hash
        ):
            raise ValueError("replacement API key must use new credentials")
        self._validate_api_key_principal(
            replacement,
            replacement_principal,
        )
        if not self._enabled:
            return False

        def _rotate() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "canonical API-key rotation requires transactions"
                )
            replacement_rows = self._api_key_issue_rows(replacement)
            replacement_rows.append(
                self._serialize_principal(replacement_principal)
            )
            client.transact_write_items(
                TransactItems=[
                    self._api_key_revoke_operation(revoked_key),
                    self._api_key_principal_deprovision_operation(revoked_key),
                    self._api_key_epoch_operation(revoked_key.tenant_id),
                    *self._api_key_put_operations(replacement_rows),
                ],
                ClientRequestToken=self._api_key_transaction_token("rotate"),
            )

        try:
            await asyncio.to_thread(_rotate)
            return True
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                return False
            if self._api_key_condition_failed(exc, 1):
                raise RuntimeError(
                    "API key principal is missing or inactive"
                ) from exc
            collision_messages = {
                3: "replacement API key already exists",
                4: "replacement API key hash already exists",
                5: "replacement API key project edge already exists",
                6: "replacement API key principal already exists",
            }
            for index, message in collision_messages.items():
                if self._api_key_condition_failed(exc, index):
                    raise RuntimeError(message) from exc
            self._record_write_failure("API key rotation", revoked_key.key_id)
            raise RuntimeError("API key rotation transaction failed") from exc

    # --- Cross-instance revocation signal ---
    #
    # A revocation has to reach instances that are holding the key in their own
    # validation cache, and there is no message bus here to push it to them. So
    # instead of broadcasting, one counter in the table is bumped on every
    # revocation and each instance polls it cheaply: a changed value means "some
    # key you may be caching was revoked", and the instance drops its cache.
    #
    # Deliberately one counter per tenant rather than a per-key tombstone. It
    # costs one small point read per active tenant and poll interval no matter
    # how many keys exist. Legacy unqualified keys retain their global counter.

    async def bump_revocation_epoch(self, tenant_id: str | None = None) -> None:
        """Signal that a key was revoked. Called on the revoking instance."""
        if not self._enabled:
            return

        def _bump():
            table = self._get_table()
            table.update_item(
                Key=self._api_key_epoch_key(tenant_id),
                UpdateExpression="ADD #epoch :one",
                ExpressionAttributeNames={"#epoch": "epoch"},
                ExpressionAttributeValues={":one": 1},
            )

        try:
            await asyncio.to_thread(_bump)
        except Exception:
            # Logged, not raised: the revocation itself already persisted, and
            # failing the request would tell the operator the revocation did not
            # happen when it did. Other instances fall back to CACHE_TTL_SECONDS.
            self._record_write_failure("revocation_epoch", "EPOCH")

    async def get_revocation_epoch(
        self,
        tenant_id: str | None = None,
    ) -> int | None:
        """Current revocation counter, or None if it could not be read.

        None is distinct from 0: 0 means "no revocation has ever happened", while
        None means the read failed and the caller should keep whatever it already
        believed rather than treat the epoch as reset.
        """
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(
                Key=self._api_key_epoch_key(tenant_id),
                ConsistentRead=True,
            )
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                return 0
            return int(item.get("epoch", 0))
        except Exception:
            logger.warning("Failed to read revocation epoch", exc_info=True)
            return None

    # --- Fleet-wide fixed-window rate limiting ---

    @staticmethod
    def _rate_limit_counter_key(
        namespace: str,
        tenant_id: str | None,
        resource_type: str,
        resource_id: str,
        window_start: int,
    ) -> dict[str, str]:
        import hashlib

        scope = json.dumps(
            [
                namespace,
                tenant_id,
                resource_type,
                resource_id,
            ],
            separators=(",", ":"),
        )
        digest = hashlib.sha256(scope.encode()).hexdigest()
        return {
            "PK": f"RATE_LIMIT#{digest}",
            "SK": f"WINDOW#{window_start}",
        }

    async def consume_rate_limit_window(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        user_id: str | None,
        project_id: str,
        user_limit: int | None,
        project_limit: int,
        window_seconds: int,
        now: datetime,
    ) -> RateLimitResult | None:
        """Atomically consume user/project capacity in one fleet-wide window."""
        if (
            not namespace.strip()
            or not project_id.strip()
            or project_limit < 1
            or window_seconds < 1
            or (user_limit is not None and user_limit < 1)
            or (user_limit is not None and not user_id)
            or now.tzinfo is None
        ):
            raise ValueError("invalid shared rate-limit request")
        if tenant_id is not None and not tenant_id.strip():
            raise ValueError("tenant_id must be None or non-empty")
        if not self._enabled:
            return None

        window_start = (
            int(now.timestamp()) // window_seconds * window_seconds
        )
        reset_at = datetime.fromtimestamp(
            window_start + window_seconds,
            tz=timezone.utc,
        )
        expires_at = int(
            (reset_at + timedelta(seconds=window_seconds)).timestamp()
        )
        counters: list[tuple[str, str, int]] = []
        if user_limit is not None and user_id is not None:
            counters.append(("user", user_id, user_limit))
        counters.append(("project", project_id, project_limit))

        def _consume() -> tuple[bool, list[int]] | None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                return None
            operations = []
            for resource_type, resource_id, limit in counters:
                key = self._rate_limit_counter_key(
                    namespace,
                    tenant_id,
                    resource_type,
                    resource_id,
                    window_start,
                )
                operations.append(
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(key),
                            "UpdateExpression": (
                                "SET expires_at = :expires_at, "
                                "tenant_id = :tenant_id, "
                                "resource_type = :resource_type, "
                                "resource_id = :resource_id "
                                "ADD #count :one"
                            ),
                            "ConditionExpression": (
                                "attribute_not_exists(#count) "
                                "OR #count < :limit"
                            ),
                            "ExpressionAttributeNames": {
                                "#count": "request_count"
                            },
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(
                                    {
                                        ":expires_at": expires_at,
                                        ":tenant_id": (
                                            tenant_id
                                            if tenant_id is not None
                                            else "__legacy__"
                                        ),
                                        ":resource_type": resource_type,
                                        ":resource_id": resource_id,
                                        ":one": 1,
                                        ":limit": limit,
                                    }
                                )
                            ),
                        }
                    }
                )
            allowed = True
            try:
                client.transact_write_items(
                    TransactItems=operations,
                    ClientRequestToken=self._api_key_transaction_token(
                        "rate-limit"
                    ),
                )
            except Exception as exc:
                if not any(
                    self._api_key_condition_failed(exc, index)
                    for index in range(len(operations))
                ):
                    raise
                allowed = False

            counts: list[int] = []
            for resource_type, resource_id, _limit in counters:
                key = self._rate_limit_counter_key(
                    namespace,
                    tenant_id,
                    resource_type,
                    resource_id,
                    window_start,
                )
                response = table.get_item(Key=key, ConsistentRead=True)
                item = response.get("Item")
                counts.append(
                    int(item.get("request_count", 0)) if item else 0
                )
            return allowed, counts

        try:
            outcome = await asyncio.to_thread(_consume)
        except Exception:
            logger.error(
                "Shared rate-limit transaction failed",
                exc_info=True,
            )
            return None
        if outcome is None:
            return None
        allowed, counts = outcome
        remaining_by_counter = [
            max(0, limit - count)
            for (_, _, limit), count in zip(counters, counts)
        ]
        restrictive_index = min(
            range(len(counters)),
            key=lambda index: remaining_by_counter[index],
        )
        limit = counters[restrictive_index][2]
        remaining = remaining_by_counter[restrictive_index]
        retry_after = None
        if not allowed:
            retry_after = max(
                1,
                int((reset_at - now).total_seconds() + 0.999999),
            )
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
            retry_after_seconds=retry_after,
        )

    # --- Fleet-wide Athena query admission and lifecycle ---

    @staticmethod
    def _query_lifecycle_key(
        tenant_id: str,
        project_id: str,
        request_id: str,
    ) -> dict[str, str]:
        import hashlib

        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return {
            "PK": tenant_project_partition_key(tenant_id),
            "SK": f"QUERY#{project_id}#{digest}",
        }

    @staticmethod
    def _query_admission_scope_key(
        tenant_id: str,
        scope: str,
        ident: str,
    ) -> str:
        import hashlib

        material = json.dumps(
            [tenant_id, scope, ident],
            separators=(",", ":"),
        )
        return (
            "QUERY_ADMISSION#"
            + hashlib.sha256(material.encode("utf-8")).hexdigest()
        )

    @classmethod
    def _query_slot_key(
        cls,
        tenant_id: str,
        scope: str,
        ident: str,
        slot: int,
    ) -> dict[str, str]:
        return {
            "PK": cls._query_admission_scope_key(
                tenant_id,
                scope,
                ident,
            ),
            "SK": f"SLOT#{slot:04d}",
        }

    @classmethod
    def _query_scan_counter_key(
        cls,
        tenant_id: str,
        scope: str,
        ident: str,
        window_start: int,
    ) -> dict[str, str]:
        return {
            "PK": cls._query_admission_scope_key(
                tenant_id,
                scope,
                ident,
            ),
            "SK": f"WINDOW#{window_start}",
        }

    @staticmethod
    def _encode_query_reconciliation_cursor(
        key: dict[str, object] | None,
    ) -> str | None:
        if key is None:
            return None
        import base64

        if (
            set(key) != {"PK", "SK"}
            or any(
                not isinstance(key.get(name), str)
                or not key[name]
                or len(key[name]) > 2048
                for name in ("PK", "SK")
            )
        ):
            raise ValueError(
                "query reconciliation continuation key is invalid"
            )
        payload = json.dumps(
            {"v": 1, "key": {"PK": key["PK"], "SK": key["SK"]}},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_query_reconciliation_cursor(
        cursor: str | None,
    ) -> dict[str, str] | None:
        if cursor is None:
            return None
        import base64

        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 4096
            or any(character.isspace() for character in cursor)
        ):
            raise ValueError("query reconciliation cursor is invalid")
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(
                base64.b64decode(
                    cursor + padding,
                    altchars=b"-_",
                    validate=True,
                )
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                "query reconciliation cursor is invalid"
            ) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "key"}
            or payload.get("v") != 1
            or not isinstance(payload.get("key"), dict)
        ):
            raise ValueError("query reconciliation cursor is invalid")
        key = payload["key"]
        if (
            set(key) != {"PK", "SK"}
            or any(
                not isinstance(key.get(name), str)
                or not key[name]
                or len(key[name]) > 2048
                for name in ("PK", "SK")
            )
        ):
            raise ValueError("query reconciliation cursor is invalid")
        return {"PK": key["PK"], "SK": key["SK"]}

    @staticmethod
    def _query_reconciliation_integer(
        value: object,
        *,
        name: str,
        minimum: int = 0,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError(f"{name} must be an integer")
        normalized = int(value)
        if value != normalized or normalized < minimum:
            raise ValueError(f"{name} must be an integer")
        return normalized

    @staticmethod
    def _query_terminal_audit_document(terminal_audit: object) -> dict:
        from src.gateway.query.reconciliation import QueryTerminalAudit

        if not isinstance(terminal_audit, QueryTerminalAudit):
            raise ValueError("query terminal audit is invalid")
        return {
            "status": terminal_audit.status,
            "failure_code": terminal_audit.failure_code,
            "execution_id": terminal_audit.execution_id,
            "athena_state": terminal_audit.athena_state,
            "observed_scan_bytes": terminal_audit.observed_scan_bytes,
            "accounted_scan_bytes": terminal_audit.accounted_scan_bytes,
            "engine_execution_ms": terminal_audit.engine_execution_ms,
            "cancellation_requested": (
                terminal_audit.cancellation_requested
            ),
            "scan_accounting": terminal_audit.scan_accounting,
            "row_count": terminal_audit.row_count,
            "truncated": terminal_audit.truncated,
            "result_bytes": terminal_audit.result_bytes,
        }

    @classmethod
    def _query_reconciliation_claim_from_item(
        cls,
        item: object,
        *,
        claim_token: str,
    ):
        from src.gateway.query.admission import QueryAdmissionLease
        from src.gateway.query.reconciliation import (
            QueryLifecycleClaim,
            QueryTerminalAudit,
        )

        if not isinstance(item, dict):
            raise ValueError("query lifecycle item is invalid")

        def _required_string(name: str) -> str:
            value = item.get(name)
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 2048
            ):
                raise ValueError(f"query lifecycle {name} is invalid")
            return value

        tenant_id = _required_string("tenant_id")
        project_id = _required_string("project_id")
        request_id = _required_string("request_id")
        if item.get("entity_type") != "query_lifecycle":
            raise ValueError("query lifecycle entity type is invalid")
        expected_key = cls._query_lifecycle_key(
            tenant_id,
            project_id,
            request_id,
        )
        if any(item.get(name) != value for name, value in expected_key.items()):
            raise ValueError("query lifecycle ownership key is invalid")

        lease = QueryAdmissionLease(
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=_required_string("principal_id"),
            request_id=request_id,
            datasource_id=_required_string("datasource_id"),
            query_sha256=_required_string("query_sha256"),
            lease_token=_required_string("lease_token"),
            window_start=cls._query_reconciliation_integer(
                item.get("window_start"),
                name="window_start",
            ),
            lease_expires_at=cls._query_reconciliation_integer(
                item.get("lease_expires_at"),
                name="lease_expires_at",
                minimum=1,
            ),
            project_slot=cls._query_reconciliation_integer(
                item.get("project_slot"),
                name="project_slot",
            ),
            principal_slot=cls._query_reconciliation_integer(
                item.get("principal_slot"),
                name="principal_slot",
            ),
            reserved_scan_bytes=cls._query_reconciliation_integer(
                item.get("reserved_scan_bytes"),
                name="reserved_scan_bytes",
                minimum=1,
            ),
        )
        status = item.get("status")
        execution_id = item.get("execution_id")
        if execution_id is not None and (
            not isinstance(execution_id, str) or not execution_id
        ):
            raise ValueError("query lifecycle execution ID is invalid")

        terminal_audit = None
        if status in {"succeeded", "failed", "cancelled"}:
            if item.get("audit_pending") is not True:
                raise ValueError(
                    "terminal query lifecycle audit is not pending"
                )
            document = item.get("terminal_audit")
            if not isinstance(document, dict):
                raise ValueError("query terminal audit document is invalid")
            expected_fields = {
                "status",
                "failure_code",
                "execution_id",
                "athena_state",
                "observed_scan_bytes",
                "accounted_scan_bytes",
                "engine_execution_ms",
                "cancellation_requested",
                "scan_accounting",
                "row_count",
                "truncated",
                "result_bytes",
            }
            if set(document) != expected_fields:
                raise ValueError("query terminal audit document is invalid")

            def _optional_integer(name: str) -> int | None:
                value = document.get(name)
                if value is None:
                    return None
                return cls._query_reconciliation_integer(
                    value,
                    name=name,
                )

            terminal_audit = QueryTerminalAudit(
                status=document.get("status"),
                failure_code=document.get("failure_code"),
                execution_id=document.get("execution_id"),
                athena_state=document.get("athena_state"),
                observed_scan_bytes=_optional_integer(
                    "observed_scan_bytes"
                ),
                accounted_scan_bytes=(
                    cls._query_reconciliation_integer(
                        document.get("accounted_scan_bytes"),
                        name="accounted_scan_bytes",
                    )
                ),
                engine_execution_ms=_optional_integer(
                    "engine_execution_ms"
                ),
                cancellation_requested=document.get(
                    "cancellation_requested"
                ),
                scan_accounting=document.get("scan_accounting"),
                row_count=_optional_integer("row_count"),
                truncated=document.get("truncated"),
                result_bytes=_optional_integer("result_bytes"),
            )
        return QueryLifecycleClaim(
            lease=lease,
            claim_token=claim_token,
            status=status,
            execution_id=execution_id,
            terminal_audit=terminal_audit,
        )

    @classmethod
    def _query_reconciliation_terminal_matches(
        cls,
        item: object,
        *,
        claim: object,
        terminal_audit: object,
    ) -> bool:
        from src.gateway.query.reconciliation import QueryLifecycleClaim

        if not isinstance(item, dict) or not isinstance(
            claim,
            QueryLifecycleClaim,
        ):
            return False
        try:
            stored = item.get("terminal_audit")
            if not isinstance(stored, dict):
                return False
            normalized = cls._query_terminal_audit_document(
                terminal_audit
            )
            return (
                item.get("lease_token") == claim.lease.lease_token
                and (
                    item.get("reconciliation_token")
                    == claim.claim_token
                    or item.get("audit_acknowledged_claim_token")
                    == claim.claim_token
                )
                and item.get("status") == normalized["status"]
                and cls._query_reconciliation_integer(
                    item.get("actual_scan_bytes"),
                    name="actual_scan_bytes",
                )
                == normalized["accounted_scan_bytes"]
                and stored == normalized
            )
        except (TypeError, ValueError):
            return False

    async def reserve_query_capacity(
        self,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
        datasource_id: str,
        query_sha256: str,
        reserved_scan_bytes: int,
        project_concurrency: int,
        principal_concurrency: int,
        project_scan_limit: int,
        principal_scan_limit: int,
        window_seconds: int,
        lease_seconds: int,
        now: datetime,
    ) -> dict | None:
        """Atomically reserve query slots, scan budgets, and lifecycle state."""
        tenant_id = _require_tenant_id(tenant_id)
        strings = {
            "project_id": project_id,
            "principal_id": principal_id,
            "request_id": request_id,
            "datasource_id": datasource_id,
            "query_sha256": query_sha256,
        }
        if any(
            not isinstance(value, str) or not value
            for value in strings.values()
        ):
            raise ValueError("query admission identities must be non-empty")
        integers = {
            "reserved_scan_bytes": reserved_scan_bytes,
            "project_concurrency": project_concurrency,
            "principal_concurrency": principal_concurrency,
            "project_scan_limit": project_scan_limit,
            "principal_scan_limit": principal_scan_limit,
            "window_seconds": window_seconds,
            "lease_seconds": lease_seconds,
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            for value in integers.values()
        ):
            raise ValueError("query admission limits must be positive integers")
        if (
            reserved_scan_bytes > project_scan_limit
            or reserved_scan_bytes > principal_scan_limit
            or principal_concurrency > project_concurrency
            or now.tzinfo is None
        ):
            raise ValueError("query admission limits are inconsistent")
        if not self._enabled:
            return None

        now_epoch = int(now.timestamp())
        window_start = (
            now_epoch // window_seconds * window_seconds
        )
        lease_expires_at = now_epoch + lease_seconds
        counter_expires_at = (
            window_start + (2 * window_seconds) + lease_seconds
        )
        lifecycle_expires_at = now_epoch + (90 * 24 * 60 * 60)
        import uuid

        lease_token = uuid.uuid4().hex
        lifecycle_key = self._query_lifecycle_key(
            tenant_id,
            project_id,
            request_id,
        )
        lifecycle_item = {
            **lifecycle_key,
            "entity_type": "query_lifecycle",
            "tenant_id": tenant_id,
            "project_id": project_id,
            "principal_id": principal_id,
            "request_id": request_id,
            "datasource_id": datasource_id,
            "query_sha256": query_sha256,
            "status": "accepted",
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at,
            "reserved_scan_bytes": reserved_scan_bytes,
            "window_start": window_start,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": lifecycle_expires_at,
        }
        project_remaining = project_scan_limit - reserved_scan_bytes
        principal_remaining = principal_scan_limit - reserved_scan_bytes
        seed = int(lease_token[:16], 16)
        project_slots = [
            (seed + offset) % project_concurrency
            for offset in range(project_concurrency)
        ]
        principal_slots = [
            (seed + offset) % principal_concurrency
            for offset in range(principal_concurrency)
        ]

        def _slot_operation(
            scope: str,
            ident: str,
            slot: int,
        ) -> dict:
            item = {
                **self._query_slot_key(
                    tenant_id,
                    scope,
                    ident,
                    slot,
                ),
                "entity_type": "query_admission_slot",
                "tenant_id": tenant_id,
                "scope": scope,
                "scope_id": ident,
                "slot": slot,
                "request_id": request_id,
                "lease_token": lease_token,
                "lease_expires_at": lease_expires_at,
                "expires_at": lease_expires_at,
            }
            return {
                "Put": {
                    "TableName": self._table_name,
                    "Item": self._serialize_dynamo_map(item),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) OR "
                        "lease_expires_at < :now"
                    ),
                    "ExpressionAttributeValues": (
                        self._serialize_dynamo_map({":now": now_epoch})
                    ),
                }
            }

        def _counter_operation(
            scope: str,
            ident: str,
            remaining: int,
        ) -> dict:
            return {
                "Update": {
                    "TableName": self._table_name,
                    "Key": self._serialize_dynamo_map(
                        self._query_scan_counter_key(
                            tenant_id,
                            scope,
                            ident,
                            window_start,
                        )
                    ),
                    "UpdateExpression": (
                        "SET entity_type = :entity_type, "
                        "tenant_id = :tenant_id, "
                        "#scope = :scope, "
                        "scope_id = :scope_id, "
                        "window_start = :window_start, "
                        "expires_at = :expires_at "
                        "ADD reserved_scan_bytes :reserved"
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(reserved_scan_bytes) OR "
                        "reserved_scan_bytes <= :remaining"
                    ),
                    "ExpressionAttributeNames": {"#scope": "scope"},
                    "ExpressionAttributeValues": (
                        self._serialize_dynamo_map(
                            {
                                ":entity_type": "query_scan_counter",
                                ":tenant_id": tenant_id,
                                ":scope": scope,
                                ":scope_id": ident,
                                ":window_start": window_start,
                                ":expires_at": counter_expires_at,
                                ":reserved": reserved_scan_bytes,
                                ":remaining": remaining,
                            }
                        )
                    ),
                }
            }

        def _reserve() -> dict:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic query admission requires transactions"
                )
            last_slot_failure = "project_concurrency"
            for project_slot in project_slots:
                for principal_slot in principal_slots:
                    operations = [
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": self._serialize_dynamo_map(
                                    {
                                        **lifecycle_item,
                                        "project_slot": project_slot,
                                        "principal_slot": principal_slot,
                                    }
                                ),
                                "ConditionExpression": (
                                    "attribute_not_exists(PK) AND "
                                    "attribute_not_exists(SK)"
                                ),
                            }
                        },
                        _slot_operation(
                            "project",
                            project_id,
                            project_slot,
                        ),
                        _slot_operation(
                            "principal",
                            principal_id,
                            principal_slot,
                        ),
                        _counter_operation(
                            "project",
                            project_id,
                            project_remaining,
                        ),
                        _counter_operation(
                            "principal",
                            principal_id,
                            principal_remaining,
                        ),
                    ]
                    try:
                        client.transact_write_items(
                            TransactItems=operations,
                            ClientRequestToken=(
                                self._api_key_transaction_token(
                                    "query-admission"
                                )
                            ),
                        )
                        return {
                            "allowed": True,
                            "lease_token": lease_token,
                            "window_start": window_start,
                            "lease_expires_at": lease_expires_at,
                            "project_slot": project_slot,
                            "principal_slot": principal_slot,
                        }
                    except Exception as exc:
                        if self._api_key_condition_failed(exc, 0):
                            return {
                                "allowed": False,
                                "reason": "duplicate_request",
                            }
                        if self._api_key_condition_failed(exc, 3):
                            return {
                                "allowed": False,
                                "reason": "project_scan_budget",
                                "retry_after_seconds": max(
                                    1,
                                    window_start
                                    + window_seconds
                                    - now_epoch,
                                ),
                            }
                        if self._api_key_condition_failed(exc, 4):
                            return {
                                "allowed": False,
                                "reason": "principal_scan_budget",
                                "retry_after_seconds": max(
                                    1,
                                    window_start
                                    + window_seconds
                                    - now_epoch,
                                ),
                            }
                        project_busy = self._api_key_condition_failed(
                            exc,
                            1,
                        )
                        principal_busy = self._api_key_condition_failed(
                            exc,
                            2,
                        )
                        if not project_busy and not principal_busy:
                            raise
                        last_slot_failure = (
                            "project_concurrency"
                            if project_busy
                            else "principal_concurrency"
                        )
            return {
                "allowed": False,
                "reason": last_slot_failure,
                "retry_after_seconds": lease_seconds,
            }

        try:
            return await asyncio.to_thread(_reserve)
        except Exception:
            logger.exception("Query admission transaction failed")
            return None

    async def mark_query_started(
        self,
        *,
        tenant_id: str,
        project_id: str,
        request_id: str,
        lease_token: str,
        execution_id: str,
        now: datetime,
    ) -> bool:
        """Persist the Athena execution ID before polling for completion."""
        tenant_id = _require_tenant_id(tenant_id)
        if any(
            not isinstance(value, str) or not value
            for value in (
                project_id,
                request_id,
                lease_token,
                execution_id,
            )
        ) or now.tzinfo is None:
            raise ValueError("query lifecycle update is invalid")
        if not self._enabled:
            return False

        def _update() -> None:
            self._get_table().update_item(
                Key=self._query_lifecycle_key(
                    tenant_id,
                    project_id,
                    request_id,
                ),
                UpdateExpression=(
                    "SET #status = :running, "
                    "execution_id = :execution_id, "
                    "updated_at = :updated_at"
                ),
                ConditionExpression=(
                    "lease_token = :lease_token AND "
                    "(#status = :accepted OR "
                    "(#status = :running AND "
                    "execution_id = :execution_id))"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":running": "running",
                    ":accepted": "accepted",
                    ":execution_id": execution_id,
                    ":lease_token": lease_token,
                    ":updated_at": now.isoformat(),
                },
            )

        try:
            await asyncio.to_thread(_update)
            return True
        except Exception:
            logger.exception("Query lifecycle start update failed")
            return False

    async def finalize_query_capacity(
        self,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        request_id: str,
        lease_token: str,
        window_start: int,
        project_slot: int,
        principal_slot: int,
        reserved_scan_bytes: int,
        actual_scan_bytes: int,
        status: str,
        execution_id: str | None,
        failure_code: str | None,
        terminal_audit: object,
        audit_claim_token: str,
        now: datetime,
        audit_claim_seconds: int = 60,
    ) -> bool:
        """Finalize lifecycle and reconcile worst-case scan reservations."""
        from src.gateway.query.reconciliation import QueryTerminalAudit

        tenant_id = _require_tenant_id(tenant_id)
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("query terminal status is invalid")
        if (
            not all(
                isinstance(value, str) and value
                for value in (
                    project_id,
                    principal_id,
                    request_id,
                    lease_token,
                )
            )
            or isinstance(reserved_scan_bytes, bool)
            or not isinstance(reserved_scan_bytes, int)
            or reserved_scan_bytes < 1
            or isinstance(actual_scan_bytes, bool)
            or not isinstance(actual_scan_bytes, int)
            or not 0 <= actual_scan_bytes <= reserved_scan_bytes
            or isinstance(window_start, bool)
            or not isinstance(window_start, int)
            or window_start < 0
            or isinstance(project_slot, bool)
            or not isinstance(project_slot, int)
            or project_slot < 0
            or isinstance(principal_slot, bool)
            or not isinstance(principal_slot, int)
            or principal_slot < 0
            or now.tzinfo is None
        ):
            raise ValueError("query finalization is invalid")
        if execution_id is not None and (
            not isinstance(execution_id, str) or not execution_id
        ):
            raise ValueError("execution_id must be None or non-empty")
        if failure_code is not None and (
            not isinstance(failure_code, str) or not failure_code
        ):
            raise ValueError("failure_code must be None or non-empty")
        if (
            not isinstance(terminal_audit, QueryTerminalAudit)
            or terminal_audit.status != status
            or terminal_audit.execution_id != execution_id
            or terminal_audit.failure_code != failure_code
            or terminal_audit.accounted_scan_bytes != actual_scan_bytes
            or not isinstance(audit_claim_token, str)
            or not audit_claim_token
            or audit_claim_token != audit_claim_token.strip()
            or len(audit_claim_token) > 256
            or isinstance(audit_claim_seconds, bool)
            or not isinstance(audit_claim_seconds, int)
            or not 15 <= audit_claim_seconds <= 900
        ):
            raise ValueError("query terminal audit claim is invalid")
        if not self._enabled:
            return False
        refund = reserved_scan_bytes - actual_scan_bytes
        lifecycle_key = self._query_lifecycle_key(
            tenant_id,
            project_id,
            request_id,
        )
        terminal_document = (
            self._query_terminal_audit_document(terminal_audit)
        )

        def _finalize() -> bool:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic query finalization requires transactions"
                )
            update_expression = (
                "SET #status = :terminal_status, "
                "actual_scan_bytes = :actual_scan_bytes, "
                "terminal_at = :terminal_at, "
                "updated_at = :updated_at"
            )
            values: dict[str, object] = {
                ":terminal_status": status,
                ":actual_scan_bytes": actual_scan_bytes,
                ":terminal_at": now.isoformat(),
                ":updated_at": now.isoformat(),
                ":lease_token": lease_token,
                ":accepted": "accepted",
                ":running": "running",
            }
            if execution_id is not None:
                update_expression += ", execution_id = :execution_id"
                values[":execution_id"] = execution_id
            if failure_code is not None:
                update_expression += ", failure_code = :failure_code"
                values[":failure_code"] = failure_code
            update_expression += (
                ", audit_pending = :audit_pending, "
                "terminal_audit = :terminal_audit, "
                "reconciliation_token = :audit_claim_token, "
                "reconciliation_owner = :reconciliation_owner, "
                "reconciliation_expires_at = :claim_expires_at, "
                "reconciliation_claimed_at = :claimed_at"
            )
            values.update(
                {
                    ":audit_pending": True,
                    ":terminal_audit": terminal_document,
                    ":audit_claim_token": audit_claim_token,
                    ":reconciliation_owner": "query-service",
                    ":claim_expires_at": (
                        int(now.timestamp()) + audit_claim_seconds
                    ),
                    ":claimed_at": now.isoformat(),
                }
            )
            operations: list[dict] = [
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": self._serialize_dynamo_map(
                            lifecycle_key
                        ),
                        "UpdateExpression": update_expression,
                        "ConditionExpression": (
                            "lease_token = :lease_token AND "
                            "(#status = :accepted OR #status = :running)"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status"
                        },
                        "ExpressionAttributeValues": (
                            self._serialize_dynamo_map(values)
                        ),
                    }
                }
            ]
            if refund:
                for scope, ident in (
                    ("project", project_id),
                    ("principal", principal_id),
                ):
                    operations.append(
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": self._serialize_dynamo_map(
                                    self._query_scan_counter_key(
                                        tenant_id,
                                        scope,
                                        ident,
                                        window_start,
                                    )
                                ),
                                "UpdateExpression": (
                                    "ADD reserved_scan_bytes :refund"
                                ),
                                "ConditionExpression": (
                                    "reserved_scan_bytes >= :absolute_refund"
                                ),
                                "ExpressionAttributeValues": (
                                    self._serialize_dynamo_map(
                                        {
                                            ":refund": -refund,
                                            ":absolute_refund": refund,
                                        }
                                    )
                                ),
                            }
                        }
                    )
            for scope, ident, slot in (
                ("project", project_id, project_slot),
                ("principal", principal_id, principal_slot),
            ):
                slot_key = self._query_slot_key(
                    tenant_id,
                    scope,
                    ident,
                    slot,
                )
                current = table.get_item(
                    Key=slot_key,
                    ConsistentRead=True,
                ).get("Item")
                if not isinstance(current, dict) or (
                    current.get("lease_token") != lease_token
                    or current.get("request_id") != request_id
                ):
                    continue
                operations.append(
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(slot_key),
                            "ConditionExpression": (
                                "lease_token = :lease_token AND "
                                "request_id = :request_id"
                            ),
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(
                                    {
                                        ":lease_token": lease_token,
                                        ":request_id": request_id,
                                    }
                                )
                            ),
                        }
                    }
                )
            try:
                client.transact_write_items(
                    TransactItems=operations,
                    ClientRequestToken=self._api_key_transaction_token(
                        "query-finalize"
                    ),
                )
            except Exception as exc:
                response = table.get_item(
                    Key=lifecycle_key,
                    ConsistentRead=True,
                )
                item = response.get("Item")
                terminal_matches = isinstance(item, dict) and (
                    item.get("lease_token") == lease_token
                    and item.get("status") == status
                    and int(item.get("actual_scan_bytes", -1))
                    == actual_scan_bytes
                    and item.get("execution_id") == execution_id
                    and item.get("failure_code") == failure_code
                )
                if terminal_matches:
                    terminal_matches = (
                        item.get("terminal_audit") == terminal_document
                        and (
                            item.get("reconciliation_token")
                            == audit_claim_token
                            or item.get(
                                "audit_acknowledged_claim_token"
                            )
                            == audit_claim_token
                        )
                    )
                if terminal_matches:
                    return True
                if any(
                    self._api_key_condition_failed(exc, index)
                    for index in range(len(operations))
                ):
                    return False
                raise
            return True

        try:
            return await asyncio.to_thread(_finalize)
        except Exception:
            logger.exception("Query lifecycle finalization failed")
            return False

    async def claim_query_reconciliation_page(
        self,
        *,
        owner_token: str,
        now: datetime,
        claim_seconds: int,
        limit: int,
        cursor: str | None,
    ):
        """Claim one bounded scan page of expired or audit-pending queries."""
        from src.gateway.query.reconciliation import QueryLifecyclePage

        if (
            not isinstance(owner_token, str)
            or not owner_token
            or owner_token != owner_token.strip()
            or len(owner_token) > 256
            or now.tzinfo is None
            or isinstance(claim_seconds, bool)
            or not isinstance(claim_seconds, int)
            or not 15 <= claim_seconds <= 900
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("query reconciliation claim request is invalid")
        start_key = self._decode_query_reconciliation_cursor(cursor)
        if not self._enabled:
            return QueryLifecyclePage(claims=())

        now_epoch = int(now.timestamp())
        claim_expires_at = now_epoch + claim_seconds

        def _claim():
            import uuid

            table = self._get_table()
            scan_values = {
                ":entity_type": "query_lifecycle",
                ":accepted": "accepted",
                ":running": "running",
                ":succeeded": "succeeded",
                ":failed": "failed",
                ":cancelled": "cancelled",
                ":now": now_epoch,
                ":pending": True,
            }
            scan_request: dict[str, object] = {
                "FilterExpression": (
                    "entity_type = :entity_type AND "
                    "((lease_expires_at <= :now AND "
                    "(#status = :accepted OR #status = :running)) OR "
                    "(audit_pending = :pending AND "
                    "(#status = :succeeded OR #status = :failed OR "
                    "#status = :cancelled))) AND "
                    "(attribute_not_exists(reconciliation_expires_at) OR "
                    "reconciliation_expires_at <= :now)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": scan_values,
                # DynamoDB applies Limit before FilterExpression. Read a
                # bounded sparse-table batch while still claiming at most the
                # caller's limit.
                "Limit": min(1000, max(limit, limit * 20)),
            }
            if start_key is not None:
                scan_request["ExclusiveStartKey"] = start_key
            response = table.scan(**scan_request)
            items = response.get("Items", ())
            if not isinstance(items, (list, tuple)):
                raise RuntimeError(
                    "query reconciliation scan returned invalid items"
                )

            claims = []
            stopped_before_page_end = False
            last_examined_key: dict[str, str] | None = None
            for index, candidate in enumerate(items):
                if not isinstance(candidate, dict):
                    logger.error(
                        "Ignoring malformed query lifecycle during "
                        "reconciliation"
                    )
                    continue
                candidate_key = {
                    "PK": candidate.get("PK"),
                    "SK": candidate.get("SK"),
                }
                try:
                    self._encode_query_reconciliation_cursor(candidate_key)
                except ValueError:
                    logger.error(
                        "Ignoring query lifecycle with malformed key during "
                        "reconciliation"
                    )
                    continue
                last_examined_key = candidate_key
                claim_token = uuid.uuid4().hex
                try:
                    candidate_claim = (
                        self._query_reconciliation_claim_from_item(
                            candidate,
                            claim_token=claim_token,
                        )
                    )
                except (TypeError, ValueError):
                    logger.error(
                        "Ignoring malformed query lifecycle during "
                        "reconciliation",
                        exc_info=True,
                    )
                    continue

                values = {
                    ":entity_type": "query_lifecycle",
                    ":lease_token": candidate_claim.lease.lease_token,
                    ":status": candidate_claim.status,
                    ":now": now_epoch,
                    ":pending": True,
                    ":claim_token": claim_token,
                    ":owner_token": owner_token,
                    ":claim_expires_at": claim_expires_at,
                    ":claimed_at": now.isoformat(),
                    ":updated_at": now.isoformat(),
                }
                eligibility = (
                    "lease_expires_at <= :now"
                    if candidate_claim.status in {"accepted", "running"}
                    else "audit_pending = :pending"
                )
                try:
                    updated = table.update_item(
                        Key={
                            "PK": candidate["PK"],
                            "SK": candidate["SK"],
                        },
                        UpdateExpression=(
                            "SET reconciliation_token = :claim_token, "
                            "reconciliation_owner = :owner_token, "
                            "reconciliation_expires_at = "
                            ":claim_expires_at, "
                            "reconciliation_claimed_at = :claimed_at, "
                            "updated_at = :updated_at"
                        ),
                        ConditionExpression=(
                            "entity_type = :entity_type AND "
                            "lease_token = :lease_token AND "
                            "#status = :status AND "
                            f"{eligibility} AND "
                            "(attribute_not_exists("
                            "reconciliation_expires_at) OR "
                            "reconciliation_expires_at <= :now)"
                        ),
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues=values,
                        ReturnValues="ALL_NEW",
                    )
                except Exception as exc:
                    if self._api_key_condition_failed(exc, 0):
                        continue
                    raise
                attributes = updated.get("Attributes")
                claimed_item = (
                    attributes
                    if isinstance(attributes, dict)
                    else candidate
                )
                claims.append(
                    self._query_reconciliation_claim_from_item(
                        claimed_item,
                        claim_token=claim_token,
                    )
                )
                if len(claims) >= limit:
                    stopped_before_page_end = index < len(items) - 1
                    break

            continuation = (
                last_examined_key
                if stopped_before_page_end
                else response.get("LastEvaluatedKey")
            )
            if continuation is not None and not isinstance(
                continuation,
                dict,
            ):
                raise RuntimeError(
                    "query reconciliation scan returned an invalid cursor"
                )
            return QueryLifecyclePage(
                claims=tuple(claims),
                next_cursor=self._encode_query_reconciliation_cursor(
                    continuation
                ),
            )

        try:
            return await asyncio.to_thread(_claim)
        except (TypeError, ValueError):
            raise
        except Exception:
            logger.exception("Query reconciliation claim scan failed")
            raise

    async def finalize_query_reconciliation(
        self,
        *,
        claim: object,
        terminal_audit: object,
        now: datetime,
    ) -> bool:
        """Atomically terminalize one claimed query and retain its audit."""
        from src.gateway.query.reconciliation import (
            QueryLifecycleClaim,
            QueryTerminalAudit,
        )

        if (
            not isinstance(claim, QueryLifecycleClaim)
            or claim.status not in {"accepted", "running"}
            or not isinstance(terminal_audit, QueryTerminalAudit)
            or terminal_audit.execution_id != claim.execution_id
            or (
                terminal_audit.accounted_scan_bytes
                > claim.lease.reserved_scan_bytes
            )
            or now.tzinfo is None
        ):
            raise ValueError(
                "query reconciliation finalization is invalid"
            )
        if not self._enabled:
            return False

        lease = claim.lease
        terminal_document = self._query_terminal_audit_document(
            terminal_audit
        )
        lifecycle_key = self._query_lifecycle_key(
            lease.tenant_id,
            lease.project_id,
            lease.request_id,
        )
        refund = (
            lease.reserved_scan_bytes
            - terminal_audit.accounted_scan_bytes
        )

        def _finalize() -> bool:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic query reconciliation requires transactions"
                )

            values: dict[str, object] = {
                ":terminal_status": terminal_audit.status,
                ":actual_scan_bytes": (
                    terminal_audit.accounted_scan_bytes
                ),
                ":terminal_at": now.isoformat(),
                ":updated_at": now.isoformat(),
                ":audit_pending": True,
                ":terminal_audit": terminal_document,
                ":lease_token": lease.lease_token,
                ":claim_token": claim.claim_token,
                ":claim_status": claim.status,
            }
            update_expression = (
                "SET #status = :terminal_status, "
                "actual_scan_bytes = :actual_scan_bytes, "
                "terminal_at = :terminal_at, "
                "updated_at = :updated_at, "
                "audit_pending = :audit_pending, "
                "terminal_audit = :terminal_audit"
            )
            removals: list[str] = []
            if terminal_audit.execution_id is None:
                removals.append("execution_id")
            else:
                update_expression += ", execution_id = :execution_id"
                values[":execution_id"] = terminal_audit.execution_id
            if terminal_audit.failure_code is None:
                removals.append("failure_code")
            else:
                update_expression += ", failure_code = :failure_code"
                values[":failure_code"] = terminal_audit.failure_code
            if removals:
                update_expression += " REMOVE " + ", ".join(removals)

            operations: list[dict] = [
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": self._serialize_dynamo_map(
                            lifecycle_key
                        ),
                        "UpdateExpression": update_expression,
                        "ConditionExpression": (
                            "lease_token = :lease_token AND "
                            "reconciliation_token = :claim_token AND "
                            "#status = :claim_status"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status"
                        },
                        "ExpressionAttributeValues": (
                            self._serialize_dynamo_map(values)
                        ),
                    }
                }
            ]

            if refund:
                for scope, ident in (
                    ("project", lease.project_id),
                    ("principal", lease.principal_id),
                ):
                    counter_key = self._query_scan_counter_key(
                        lease.tenant_id,
                        scope,
                        ident,
                        lease.window_start,
                    )
                    counter = table.get_item(
                        Key=counter_key,
                        ConsistentRead=True,
                    ).get("Item")
                    if counter is None:
                        continue
                    if not isinstance(counter, dict):
                        raise RuntimeError(
                            "query scan counter is malformed"
                        )
                    operations.append(
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": self._serialize_dynamo_map(
                                    counter_key
                                ),
                                "UpdateExpression": (
                                    "ADD reserved_scan_bytes :refund"
                                ),
                                "ConditionExpression": (
                                    "entity_type = :entity_type AND "
                                    "tenant_id = :tenant_id AND "
                                    "#scope = :scope AND "
                                    "scope_id = :scope_id AND "
                                    "reserved_scan_bytes >= "
                                    ":absolute_refund"
                                ),
                                "ExpressionAttributeNames": {
                                    "#scope": "scope"
                                },
                                "ExpressionAttributeValues": (
                                    self._serialize_dynamo_map(
                                        {
                                            ":entity_type": (
                                                "query_scan_counter"
                                            ),
                                            ":tenant_id": lease.tenant_id,
                                            ":scope": scope,
                                            ":scope_id": ident,
                                            ":refund": -refund,
                                            ":absolute_refund": refund,
                                        }
                                    )
                                ),
                            }
                        }
                    )

            for scope, ident, slot in (
                ("project", lease.project_id, lease.project_slot),
                (
                    "principal",
                    lease.principal_id,
                    lease.principal_slot,
                ),
            ):
                slot_key = self._query_slot_key(
                    lease.tenant_id,
                    scope,
                    ident,
                    slot,
                )
                current = table.get_item(
                    Key=slot_key,
                    ConsistentRead=True,
                ).get("Item")
                if not isinstance(current, dict) or (
                    current.get("lease_token") != lease.lease_token
                    or current.get("request_id") != lease.request_id
                ):
                    continue
                operations.append(
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(slot_key),
                            "ConditionExpression": (
                                "lease_token = :lease_token AND "
                                "request_id = :request_id"
                            ),
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(
                                    {
                                        ":lease_token": lease.lease_token,
                                        ":request_id": lease.request_id,
                                    }
                                )
                            ),
                        }
                    }
                )

            try:
                client.transact_write_items(
                    TransactItems=operations,
                    ClientRequestToken=self._api_key_transaction_token(
                        "query-reconcile-finalize"
                    ),
                )
                return True
            except Exception as exc:
                response = table.get_item(
                    Key=lifecycle_key,
                    ConsistentRead=True,
                )
                if self._query_reconciliation_terminal_matches(
                    response.get("Item"),
                    claim=claim,
                    terminal_audit=terminal_audit,
                ):
                    return True
                if any(
                    self._api_key_condition_failed(exc, index)
                    for index in range(len(operations))
                ):
                    return False
                raise

        try:
            return await asyncio.to_thread(_finalize)
        except Exception:
            logger.exception("Query reconciliation finalization failed")
            return False

    async def defer_query_reconciliation(
        self,
        *,
        claim: object,
        now: datetime,
    ) -> bool:
        """Release only the matching active reconciliation claim."""
        from src.gateway.query.reconciliation import QueryLifecycleClaim

        if (
            not isinstance(claim, QueryLifecycleClaim)
            or claim.status not in {"accepted", "running"}
            or now.tzinfo is None
        ):
            raise ValueError("query reconciliation defer request is invalid")
        if not self._enabled:
            return False
        lease = claim.lease
        lifecycle_key = self._query_lifecycle_key(
            lease.tenant_id,
            lease.project_id,
            lease.request_id,
        )

        def _defer() -> bool:
            table = self._get_table()
            try:
                table.update_item(
                    Key=lifecycle_key,
                    UpdateExpression=(
                        "SET reconciliation_deferred_token = :claim_token, "
                        "reconciliation_deferred_at = :deferred_at, "
                        "updated_at = :updated_at "
                        "REMOVE reconciliation_token, "
                        "reconciliation_owner, "
                        "reconciliation_expires_at, "
                        "reconciliation_claimed_at"
                    ),
                    ConditionExpression=(
                        "lease_token = :lease_token AND "
                        "reconciliation_token = :claim_token AND "
                        "#status = :claim_status"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":lease_token": lease.lease_token,
                        ":claim_token": claim.claim_token,
                        ":claim_status": claim.status,
                        ":deferred_at": now.isoformat(),
                        ":updated_at": now.isoformat(),
                    },
                )
                return True
            except Exception as exc:
                response = table.get_item(
                    Key=lifecycle_key,
                    ConsistentRead=True,
                )
                item = response.get("Item")
                if (
                    isinstance(item, dict)
                    and item.get("lease_token") == lease.lease_token
                    and item.get("status") == claim.status
                    and item.get("reconciliation_deferred_token")
                    == claim.claim_token
                ):
                    return True
                if self._api_key_condition_failed(exc, 0):
                    return False
                raise

        try:
            return await asyncio.to_thread(_defer)
        except Exception:
            logger.exception("Query reconciliation defer failed")
            return False

    async def ack_query_reconciliation_audit(
        self,
        *,
        claim: object,
        now: datetime,
    ) -> bool:
        """Acknowledge only the exact terminal audit held by this claim."""
        from src.gateway.query.reconciliation import QueryLifecycleClaim

        if (
            not isinstance(claim, QueryLifecycleClaim)
            or claim.status not in {"succeeded", "failed", "cancelled"}
            or claim.terminal_audit is None
            or now.tzinfo is None
        ):
            raise ValueError(
                "query reconciliation audit acknowledgement is invalid"
            )
        if not self._enabled:
            return False
        lease = claim.lease
        lifecycle_key = self._query_lifecycle_key(
            lease.tenant_id,
            lease.project_id,
            lease.request_id,
        )
        terminal_document = self._query_terminal_audit_document(
            claim.terminal_audit
        )

        def _acknowledge() -> bool:
            table = self._get_table()
            try:
                table.update_item(
                    Key=lifecycle_key,
                    UpdateExpression=(
                        "SET audit_acknowledged_at = :acknowledged_at, "
                        "audit_acknowledged_claim_token = :claim_token, "
                        "updated_at = :updated_at "
                        "REMOVE audit_pending, reconciliation_token, "
                        "reconciliation_owner, "
                        "reconciliation_expires_at, "
                        "reconciliation_claimed_at"
                    ),
                    ConditionExpression=(
                        "lease_token = :lease_token AND "
                        "reconciliation_token = :claim_token AND "
                        "#status = :terminal_status AND "
                        "audit_pending = :pending AND "
                        "terminal_audit = :terminal_audit"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":lease_token": lease.lease_token,
                        ":claim_token": claim.claim_token,
                        ":terminal_status": claim.status,
                        ":pending": True,
                        ":terminal_audit": terminal_document,
                        ":acknowledged_at": now.isoformat(),
                        ":updated_at": now.isoformat(),
                    },
                )
                return True
            except Exception as exc:
                response = table.get_item(
                    Key=lifecycle_key,
                    ConsistentRead=True,
                )
                item = response.get("Item")
                if (
                    isinstance(item, dict)
                    and item.get("lease_token") == lease.lease_token
                    and item.get("status") == claim.status
                    and item.get("terminal_audit") == terminal_document
                    and item.get("audit_pending") is not True
                    and item.get("audit_acknowledged_claim_token")
                    == claim.claim_token
                ):
                    return True
                if self._api_key_condition_failed(exc, 0):
                    return False
                raise

        try:
            return await asyncio.to_thread(_acknowledge)
        except Exception:
            logger.exception(
                "Query reconciliation audit acknowledgement failed"
            )
            return False

    # --- Fleet-wide spend counters ---
    #
    # Budget enforcement compares spend against a limit, so the spend it reads has
    # to be the whole fleet's. Every instance accumulating its own counter meant a
    # $100 limit admitted roughly $100 *per task* — ~$200 with the shipped
    # desired_count=2, ~$1000 once auto-scaling reached 10 — because no instance
    # ever saw more than its own share.
    #
    # DynamoDB's ADD is atomic and returns the post-update value, which is the
    # whole trick here: the instance that records spend learns the fleet total as
    # a side effect of a write it was already making. No extra read, no lock
    # across instances, and no lost update when two tasks bill at once.

    @staticmethod
    def _normalize_budget_reservations(
        reservations: list[tuple[str, str, float]],
    ) -> list[tuple[str, str, Decimal]]:
        """Validate and canonicalize the counters one request must reserve."""
        normalized: list[tuple[str, str, Decimal]] = []
        seen_scopes: set[str] = set()
        for scope, ident, limit in reservations:
            if (
                not isinstance(scope, str)
                or not scope.strip()
                or not isinstance(ident, str)
                or not ident.strip()
            ):
                raise ValueError(
                    "budget reservation scope and ident must be non-empty"
                )
            if scope in seen_scopes:
                raise ValueError(
                    "a budget reservation may contain one counter per scope"
                )
            limit_decimal = Decimal(str(limit))
            if not limit_decimal.is_finite() or limit_decimal < 0:
                raise ValueError(
                    "budget reservation limits must be finite and non-negative"
                )
            seen_scopes.add(scope)
            normalized.append((scope, ident, limit_decimal))
        if not normalized:
            raise ValueError("at least one budget reservation is required")
        return sorted(normalized, key=lambda value: (value[0], value[1]))

    @staticmethod
    def _budget_counter_key(scope: str, ident: str) -> dict[str, str]:
        return {
            "PK": f"SPEND#{scope}#{ident}",
            "SK": "TOTAL",
        }

    @classmethod
    def _budget_alert_key(
        cls,
        scope: str,
        ident: str,
        epoch: int,
        threshold: Decimal,
    ) -> dict[str, str]:
        return {
            **cls._budget_counter_key(scope, ident),
            "SK": (
                f"ALERT#{epoch}#"
                f"{int(threshold * Decimal('100'))}"
            ),
        }

    @classmethod
    def _budget_reservation_key(
        cls,
        request_id: str,
        reservations: list[tuple[str, str, Decimal]],
    ) -> dict[str, str]:
        import hashlib

        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > 256
        ):
            raise ValueError(
                "budget reservation request_id must be 1-256 characters"
            )
        primary_scope, primary_ident, _ = reservations[0]
        request_hash = hashlib.sha256(request_id.encode()).hexdigest()
        return {
            **cls._budget_counter_key(primary_scope, primary_ident),
            "SK": f"RESERVATION#{request_hash}",
        }

    @staticmethod
    def _budget_reservation_signature(
        reservations: list[tuple[str, str, Decimal]],
    ) -> str:
        return json.dumps(
            [
                [scope, ident, str(limit)]
                for scope, ident, limit in reservations
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _budget_amount(value: float, *, name: str) -> Decimal:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return amount

    @staticmethod
    def _budget_states_from_table(
        table,
        reservations: list[tuple[str, str, Decimal]],
    ) -> dict[str, SpendCounterState]:
        states: dict[str, SpendCounterState] = {}
        for scope, ident, _ in reservations:
            response = table.get_item(
                Key=DynamoPersistence._budget_counter_key(scope, ident),
                ConsistentRead=True,
            )
            item = response.get("Item")
            states[scope] = SpendCounterState(
                total=float(item.get("spend", 0)) if item else 0.0,
                epoch=int(item.get("epoch", 0)) if item else 0,
            )
        return states

    @classmethod
    def _budget_totals_from_table(
        cls,
        table,
        reservations: list[tuple[str, str, Decimal]],
    ) -> dict[str, float]:
        return {
            scope: state.total
            for scope, state in cls._budget_states_from_table(
                table,
                reservations,
            ).items()
        }

    @staticmethod
    def _budget_epochs_from_marker(
        marker: dict,
        reservations: list[tuple[str, str, Decimal]],
    ) -> dict[str, int]:
        raw = marker.get("reservation_epochs")
        if raw is None:
            return {scope: 0 for scope, _ident, _limit in reservations}
        try:
            decoded = json.loads(str(raw))
            epochs = {
                str(scope): int(epoch)
                for scope, epoch in decoded.items()
            }
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "budget reservation has invalid billing epochs"
            ) from exc
        expected = {scope for scope, _ident, _limit in reservations}
        if set(epochs) != expected or any(epoch < 0 for epoch in epochs.values()):
            raise RuntimeError(
                "budget reservation has invalid billing epochs"
            )
        return epochs

    async def reserve_budget(
        self,
        *,
        request_id: str,
        reservations: list[tuple[str, str, float]],
        amount: float,
        now: datetime | None = None,
        lease_seconds: int = 900,
    ) -> BudgetReservationResult | None:
        """Atomically reserve estimated spend against every applicable limit.

        The request marker and all counter increments commit in one DynamoDB
        transaction. Repeating the same request is idempotent; a changed amount
        or scope under the same request id is rejected as unavailable rather
        than charged twice.
        """
        normalized = self._normalize_budget_reservations(reservations)
        reserved = self._budget_amount(amount, name="reservation amount")
        if reserved <= 0:
            raise ValueError("reservation amount must be greater than zero")
        if lease_seconds < 60:
            raise ValueError("budget reservation lease must be at least 60s")
        if not self._enabled:
            return None
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("budget reservation time must be timezone-aware")

        marker_key = self._budget_reservation_key(
            request_id,
            normalized,
        )
        signature = self._budget_reservation_signature(normalized)
        lease_expires_at = int(
            (current_time + timedelta(seconds=lease_seconds)).timestamp()
        )
        created_at = current_time.isoformat()

        # An individual request larger than a limit cannot satisfy the counter
        # condition even when the counter is absent. Return a normal denial
        # without attempting an invalid negative max-before comparison.
        impossible_scope = next(
            (
                scope
                for scope, _ident, limit in normalized
                if reserved > limit
            ),
            None,
        )
        if impossible_scope is not None:
            try:
                states = await asyncio.to_thread(
                    self._budget_states_from_table,
                    self._get_table(),
                    normalized,
                )
            except Exception:
                states = {}
            return BudgetReservationResult(
                allowed=False,
                request_id=request_id,
                reserved_amount=float(reserved),
                totals={
                    scope: state.total
                    for scope, state in states.items()
                },
                epochs={
                    scope: state.epoch
                    for scope, state in states.items()
                },
                state="denied",
                denied_scope=impossible_scope,
            )

        def _reserve() -> BudgetReservationResult:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic budget reservations require transactions"
                )
            for _attempt in range(5):
                states = self._budget_states_from_table(table, normalized)
                epochs = {
                    scope: state.epoch
                    for scope, state in states.items()
                }
                epoch_signature = json.dumps(
                    epochs,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                marker = {
                    **marker_key,
                    "entity_type": "budget_reservation",
                    "request_id": request_id,
                    "reservation_signature": signature,
                    "reservation_epochs": epoch_signature,
                    "reserved_amount": reserved,
                    # A cleanup that cannot learn the actual provider charge
                    # keeps the admitted estimate. Finalization replaces this
                    # before reconciling the counters.
                    "settlement_amount": reserved,
                    "settlement_state": "estimated",
                    "state": "reserved",
                    "created_at": created_at,
                    "updated_at": created_at,
                    "lease_expires_at": lease_expires_at,
                }
                operations = [
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(marker),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) "
                                "AND attribute_not_exists(SK)"
                            ),
                        }
                    }
                ]
                for scope, ident, limit in normalized:
                    operations.append(
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": self._serialize_dynamo_map(
                                    self._budget_counter_key(scope, ident)
                                ),
                                "UpdateExpression": (
                                    "SET entity_type = :entity_type, "
                                    "budget_scope = :scope, "
                                    "updated_at = :updated_at, "
                                    "#epoch = if_not_exists(#epoch, :zero) "
                                    "ADD #spend :amount"
                                ),
                                "ConditionExpression": (
                                    "(attribute_not_exists(#epoch) "
                                    "OR #epoch = :expected_epoch) AND "
                                    "(attribute_not_exists(#spend) "
                                    "OR #spend <= :max_before)"
                                ),
                                "ExpressionAttributeNames": {
                                    "#epoch": "epoch",
                                    "#spend": "spend",
                                },
                                "ExpressionAttributeValues": (
                                    self._serialize_dynamo_map(
                                        {
                                            ":entity_type": "spend_counter",
                                            ":scope": scope,
                                            ":updated_at": created_at,
                                            ":zero": 0,
                                            ":expected_epoch": epochs[scope],
                                            ":amount": reserved,
                                            ":max_before": limit - reserved,
                                        }
                                    )
                                ),
                            }
                        }
                    )

                try:
                    client.transact_write_items(
                        TransactItems=operations,
                        ClientRequestToken=self._api_key_transaction_token(
                            "budget-reserve"
                        ),
                    )
                except Exception as exc:
                    existing = table.get_item(
                        Key=marker_key,
                        ConsistentRead=True,
                    ).get("Item")
                    if existing is not None:
                        if (
                            existing.get("request_id") != request_id
                            or existing.get("reservation_signature") != signature
                            or Decimal(str(existing.get("reserved_amount")))
                            != reserved
                        ):
                            raise RuntimeError(
                                "budget reservation idempotency conflict"
                            ) from exc
                        state = str(existing.get("state", ""))
                        if state not in {"reserved", "finalized"}:
                            raise RuntimeError(
                                "budget reservation has an invalid state"
                            ) from exc
                        current = self._budget_states_from_table(
                            table,
                            normalized,
                        )
                        return BudgetReservationResult(
                            allowed=True,
                            request_id=request_id,
                            reserved_amount=float(reserved),
                            totals={
                                scope: value.total
                                for scope, value in current.items()
                            },
                            epochs=self._budget_epochs_from_marker(
                                existing,
                                normalized,
                            ),
                            state=state,
                            idempotent=True,
                        )

                    denied_index = next(
                        (
                            index
                            for index in range(1, len(operations))
                            if self._api_key_condition_failed(exc, index)
                        ),
                        None,
                    )
                    if denied_index is None:
                        raise
                    current = self._budget_states_from_table(
                        table,
                        normalized,
                    )
                    if any(
                        current[scope].epoch != epoch
                        for scope, epoch in epochs.items()
                    ):
                        continue
                    denied_scope = normalized[denied_index - 1][0]
                    return BudgetReservationResult(
                        allowed=False,
                        request_id=request_id,
                        reserved_amount=float(reserved),
                        totals={
                            scope: value.total
                            for scope, value in current.items()
                        },
                        epochs={
                            scope: value.epoch
                            for scope, value in current.items()
                        },
                        state="denied",
                        denied_scope=denied_scope,
                    )

                current = self._budget_states_from_table(
                    table,
                    normalized,
                )
                return BudgetReservationResult(
                    allowed=True,
                    request_id=request_id,
                    reserved_amount=float(reserved),
                    totals={
                        scope: value.total
                        for scope, value in current.items()
                    },
                    epochs=epochs,
                )
            raise RuntimeError(
                "budget reservation raced repeated billing-cycle resets"
            )

        try:
            return await asyncio.to_thread(_reserve)
        except Exception:
            self._record_write_failure("budget reservation", request_id)
            logger.error(
                "Atomic budget reservation failed for %s",
                request_id,
                exc_info=True,
            )
            return None

    async def finalize_budget_reservation(
        self,
        *,
        request_id: str,
        reservations: list[tuple[str, str, float]],
        reserved_amount: float,
        actual_cost: float,
        now: datetime | None = None,
    ) -> BudgetReservationResult | None:
        """Idempotently replace a reservation with the request's actual cost."""
        normalized = self._normalize_budget_reservations(reservations)
        reserved = self._budget_amount(
            reserved_amount,
            name="reserved amount",
        )
        actual = self._budget_amount(actual_cost, name="actual cost")
        if reserved <= 0:
            raise ValueError("reserved amount must be greater than zero")
        if not self._enabled:
            return None
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("budget finalization time must be timezone-aware")

        marker_key = self._budget_reservation_key(
            request_id,
            normalized,
        )
        signature = self._budget_reservation_signature(normalized)
        updated_at = current_time.isoformat()
        expires_at = int(
            (current_time + timedelta(days=7)).timestamp()
        )

        def _finalize() -> BudgetReservationResult:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "atomic budget finalization requires transactions"
                )

            def _read_marker() -> dict | None:
                return table.get_item(
                    Key=marker_key,
                    ConsistentRead=True,
                ).get("Item")

            def _result(
                *,
                idempotent: bool,
                crossed_thresholds: tuple[float, ...] = (),
            ) -> BudgetReservationResult:
                states = self._budget_states_from_table(
                    table,
                    normalized,
                )
                return BudgetReservationResult(
                    allowed=True,
                    request_id=request_id,
                    reserved_amount=float(reserved),
                    totals={
                        scope: value.total
                        for scope, value in states.items()
                    },
                    epochs={
                        scope: value.epoch
                        for scope, value in states.items()
                    },
                    state="finalized",
                    idempotent=idempotent,
                    crossed_thresholds=crossed_thresholds,
                )

            marker = _read_marker()
            if marker is None:
                raise RuntimeError("budget reservation does not exist")
            if (
                marker.get("request_id") != request_id
                or marker.get("reservation_signature") != signature
                or Decimal(str(marker.get("reserved_amount"))) != reserved
            ):
                raise RuntimeError(
                    "budget reservation finalization conflict"
                )
            state = str(marker.get("state", ""))
            if state == "finalized":
                if Decimal(str(marker.get("actual_cost"))) != actual:
                    raise RuntimeError(
                        "budget reservation was finalized with another cost"
                    )
                return _result(
                    idempotent=True,
                )
            if state != "reserved":
                raise RuntimeError("budget reservation has an invalid state")

            reservation_epochs = self._budget_epochs_from_marker(
                marker,
                normalized,
            )
            # Persist the settlement intent before the all-or-nothing counter
            # reconciliation. If the latter is interrupted, lease cleanup uses
            # this amount instead of converting real provider spend to zero.
            try:
                table.update_item(
                    Key=marker_key,
                    UpdateExpression=(
                        "SET settlement_amount = :actual, "
                        "settlement_state = :reported, "
                        "updated_at = :updated_at"
                    ),
                    ConditionExpression=(
                        "#state = :reserved_state AND "
                        "reservation_signature = :signature AND "
                        "(attribute_not_exists(settlement_state) OR "
                        "settlement_state = :estimated OR "
                        "(settlement_state = :reported AND "
                        "settlement_amount = :actual))"
                    ),
                    ExpressionAttributeNames={"#state": "state"},
                    ExpressionAttributeValues={
                        ":actual": actual,
                        ":reported": "reported",
                        ":estimated": "estimated",
                        ":reserved_state": "reserved",
                        ":signature": signature,
                        ":updated_at": updated_at,
                    },
                )
            except Exception as exc:
                latest = _read_marker()
                if latest is not None and latest.get("state") == "finalized":
                    if Decimal(str(latest.get("actual_cost"))) == actual:
                        return _result(
                            idempotent=True,
                        )
                if (
                    latest is not None
                    and latest.get("state") == "reserved"
                    and latest.get("settlement_state") == "reported"
                    and Decimal(str(latest.get("settlement_amount"))) != actual
                ):
                    raise RuntimeError(
                        "budget reservation was reported with another cost"
                    ) from exc
                raise

            delta = actual - reserved
            for _attempt in range(3):
                states = self._budget_states_from_table(
                    table,
                    normalized,
                )
                if any(
                    states[scope].epoch < reservation_epoch
                    for scope, reservation_epoch in reservation_epochs.items()
                ):
                    raise RuntimeError(
                        "budget counter epoch moved backwards"
                    )
                values = self._serialize_dynamo_map(
                    {
                        ":reserved_state": "reserved",
                        ":finalized_state": "finalized",
                        ":signature": signature,
                        ":actual": actual,
                        ":reported": "reported",
                        ":updated_at": updated_at,
                        ":expires_at": expires_at,
                    }
                )
                operations = [
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(marker_key),
                            "UpdateExpression": (
                                "SET #state = :finalized_state, "
                                "actual_cost = :actual, "
                                "updated_at = :updated_at, "
                                "expires_at = :expires_at"
                            ),
                            "ConditionExpression": (
                                "#state = :reserved_state AND "
                                "reservation_signature = :signature AND "
                                "settlement_state = :reported AND "
                                "settlement_amount = :actual"
                            ),
                            "ExpressionAttributeNames": {
                                "#state": "state",
                            },
                            "ExpressionAttributeValues": values,
                        }
                    }
                ]
                if delta != 0:
                    for scope, ident, _limit in normalized:
                        expected_epoch = reservation_epochs[scope]
                        if states[scope].epoch != expected_epoch:
                            continue
                        operations.append(
                            {
                                "Update": {
                                    "TableName": self._table_name,
                                    "Key": self._serialize_dynamo_map(
                                        self._budget_counter_key(
                                            scope,
                                            ident,
                                        )
                                    ),
                                    "UpdateExpression": (
                                        "SET updated_at = :updated_at, "
                                        "#epoch = if_not_exists("
                                        "#epoch, :zero) ADD #spend :delta"
                                    ),
                                    "ConditionExpression": (
                                        "attribute_exists(#spend) AND "
                                        "#spend >= :reserved AND "
                                        "(#epoch = :expected_epoch OR "
                                        "(attribute_not_exists(#epoch) AND "
                                        ":expected_epoch = :zero))"
                                    ),
                                    "ExpressionAttributeNames": {
                                        "#epoch": "epoch",
                                        "#spend": "spend",
                                    },
                                    "ExpressionAttributeValues": (
                                        self._serialize_dynamo_map(
                                            {
                                                ":updated_at": updated_at,
                                                ":delta": delta,
                                                ":reserved": reserved,
                                                ":expected_epoch": (
                                                    expected_epoch
                                                ),
                                                ":zero": 0,
                                            }
                                        )
                                    ),
                                }
                            }
                        )
                alert_operation_indexes: set[int] = set()
                crossed_thresholds: list[float] = []
                quota_counter = next(
                    (
                        (ident, limit)
                        for scope, ident, limit in normalized
                        if scope == "quota"
                    ),
                    None,
                )
                if quota_counter is not None:
                    quota_ident, quota_limit = quota_counter
                    quota_epoch = reservation_epochs["quota"]
                    if (
                        quota_limit > 0
                        and states["quota"].epoch == quota_epoch
                    ):
                        finalized_total = (
                            Decimal(str(states["quota"].total)) + delta
                        )
                        for threshold in _BUDGET_ALERT_THRESHOLDS:
                            if finalized_total < quota_limit * threshold:
                                continue
                            alert_key = self._budget_alert_key(
                                "quota",
                                quota_ident,
                                quota_epoch,
                                threshold,
                            )
                            existing_alert = table.get_item(
                                Key=alert_key,
                                ConsistentRead=True,
                            ).get("Item")
                            if existing_alert is not None:
                                continue
                            alert_operation_indexes.add(len(operations))
                            crossed_thresholds.append(float(threshold))
                            operations.append(
                                {
                                    "Put": {
                                        "TableName": self._table_name,
                                        "Item": self._serialize_dynamo_map(
                                            {
                                                **alert_key,
                                                "entity_type": (
                                                    "budget_threshold_alert"
                                                ),
                                                "budget_scope": "quota",
                                                "budget_ident": quota_ident,
                                                "billing_epoch": quota_epoch,
                                                "threshold_pct": (
                                                    threshold * 100
                                                ),
                                                "created_at": updated_at,
                                            }
                                        ),
                                        "ConditionExpression": (
                                            "attribute_not_exists(PK) AND "
                                            "attribute_not_exists(SK)"
                                        ),
                                    }
                                }
                            )
                try:
                    client.transact_write_items(
                        TransactItems=operations,
                        ClientRequestToken=self._api_key_transaction_token(
                            "budget-finalize"
                        ),
                    )
                except Exception as exc:
                    latest = _read_marker()
                    if (
                        latest is not None
                        and latest.get("state") == "finalized"
                    ):
                        if Decimal(str(latest.get("actual_cost"))) != actual:
                            raise RuntimeError(
                                "budget reservation finalized concurrently "
                                "with another cost"
                            ) from exc
                        return _result(
                            idempotent=True,
                        )
                    if any(
                        self._api_key_condition_failed(exc, index)
                        for index in alert_operation_indexes
                    ):
                        continue
                    current = self._budget_states_from_table(
                        table,
                        normalized,
                    )
                    if any(
                        current[scope].epoch != states[scope].epoch
                        for scope in states
                    ):
                        continue
                    raise
                return _result(
                    idempotent=False,
                    crossed_thresholds=tuple(crossed_thresholds),
                )
            raise RuntimeError(
                "budget finalization raced repeated billing-cycle resets"
            )

        try:
            return await asyncio.to_thread(_finalize)
        except Exception:
            self._record_write_failure(
                "budget reservation finalization",
                request_id,
            )
            logger.error(
                "Atomic budget finalization failed for %s",
                request_id,
                exc_info=True,
            )
            return None

    async def release_budget_reservation(
        self,
        *,
        request_id: str,
        reservations: list[tuple[str, str, float]],
        reserved_amount: float,
    ) -> BudgetReservationResult | None:
        """Release a reservation for a request that made no billable call."""
        return await self.finalize_budget_reservation(
            request_id=request_id,
            reservations=reservations,
            reserved_amount=reserved_amount,
            actual_cost=0.0,
        )

    async def release_expired_budget_reservations(
        self,
        *,
        primary_scope: str,
        primary_ident: str,
        now: datetime | None = None,
    ) -> int | None:
        """Reconcile expired reservations in one counter partition.

        Reserved markers are deliberately not assigned DynamoDB TTL: deleting a
        marker without decrementing its counters would leak capacity forever.
        This bounded partition query finds expired leases and finalizes each at
        its last durable settlement amount. New reservations default that amount
        to the admitted estimate; an explicit release changes it to zero, while
        a completed provider call records its actual cost before reconciliation.
        Finalized markers then receive the normal seven-day TTL.
        """
        if (
            not isinstance(primary_scope, str)
            or not primary_scope.strip()
            or not isinstance(primary_ident, str)
            or not primary_ident.strip()
        ):
            raise ValueError(
                "budget cleanup scope and ident must be non-empty"
            )
        if not self._enabled:
            return None
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("budget cleanup time must be timezone-aware")
        now_epoch = int(current_time.timestamp())

        def _query() -> list[dict]:
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            condition = (
                Key("PK").eq(
                    self._budget_counter_key(
                        primary_scope,
                        primary_ident,
                    )["PK"]
                )
                & Key("SK").begins_with("RESERVATION#")
            )
            response = table.query(
                KeyConditionExpression=condition,
                ConsistentRead=True,
            )
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = table.query(
                    KeyConditionExpression=condition,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                    ConsistentRead=True,
                )
                items.extend(response.get("Items", []))
            return [
                self._convert_decimals_to_native(item)
                for item in items
                if item.get("state") == "reserved"
                and int(item.get("lease_expires_at", 0)) <= now_epoch
            ]

        try:
            expired = await asyncio.to_thread(_query)
        except Exception:
            logger.error(
                "Failed to scan expired budget reservations for %s/%s",
                primary_scope,
                primary_ident,
                exc_info=True,
            )
            return None

        released = 0
        for marker in expired:
            try:
                raw_reservations = json.loads(
                    marker["reservation_signature"]
                )
                reservations = [
                    (scope, ident, float(limit))
                    for scope, ident, limit in raw_reservations
                ]
                result = await self.finalize_budget_reservation(
                    request_id=marker["request_id"],
                    reservations=reservations,
                    reserved_amount=float(marker["reserved_amount"]),
                    actual_cost=float(
                        marker.get(
                            "settlement_amount",
                            marker["reserved_amount"],
                        )
                    ),
                    now=current_time,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.error(
                    "Malformed expired budget reservation %s",
                    marker.get("request_id", "?"),
                    exc_info=True,
                )
                continue
            if result is not None:
                released += 1
        return released

    @staticmethod
    def _validate_spend_identity(scope: str, ident: str) -> None:
        if (
            not isinstance(scope, str)
            or not scope.strip()
            or not isinstance(ident, str)
            or not ident.strip()
        ):
            raise ValueError("spend scope and ident must be non-empty")

    async def add_spend_state(
        self,
        scope: str,
        ident: str,
        cost: float,
    ) -> SpendCounterState | None:
        """Atomically add cost and return the billing epoch with its total."""
        self._validate_spend_identity(scope, ident)
        amount = self._budget_amount(cost, name="spend cost")
        if not self._enabled:
            return None

        def _add():
            table = self._get_table()
            resp = table.update_item(
                Key=self._budget_counter_key(scope, ident),
                UpdateExpression=(
                    "SET entity_type = :entity_type, "
                    "budget_scope = :scope, "
                    "#epoch = if_not_exists(#epoch, :zero) "
                    "ADD #spend :cost"
                ),
                ExpressionAttributeNames={
                    "#epoch": "epoch",
                    "#spend": "spend",
                },
                ExpressionAttributeValues={
                    ":entity_type": "spend_counter",
                    ":scope": scope,
                    ":zero": 0,
                    ":cost": amount,
                },
                ReturnValues="UPDATED_NEW",
            )
            attributes = resp.get("Attributes", {})
            if "spend" not in attributes or "epoch" not in attributes:
                return None
            return SpendCounterState(
                total=float(attributes["spend"]),
                epoch=int(attributes["epoch"]),
            )

        try:
            return await asyncio.to_thread(_add)
        except Exception:
            # Logged and surfaced through last_write_error rather than raised: a
            # provider call should not 500 because the counter write failed, and
            # the caller degrades to its local total.
            self._record_write_failure(f"spend_{scope}", ident)
            return None

    async def add_spend(self, scope: str, ident: str, cost: float) -> float | None:
        """Atomically add to a spend counter and return the new fleet-wide total.

        ``scope`` is ``"project"`` or ``"user"``; ``ident`` the id within it.

        Returns None if the counter could not be updated, which the caller must
        treat as "no fleet total available" and fall back to its local figure —
        not as zero, which would read as a reset budget and let every request
        through.
        """
        state = await self.add_spend_state(scope, ident, cost)
        return state.total if state is not None else None

    async def get_spend_state(
        self,
        scope: str,
        ident: str,
    ) -> SpendCounterState | None:
        """Read a fleet-wide total with its monotonic billing epoch.

        Used to seed an instance at startup and to answer admin reads. Not called
        per request — `add_spend` already returns the total the request path
        needs.

        None is distinct from 0.0: 0.0 means the counter exists and nothing has
        been spent, while None means the read failed and the caller should keep
        whatever total it already had.
        """
        self._validate_spend_identity(scope, ident)
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(
                Key=self._budget_counter_key(scope, ident),
                ConsistentRead=True,
            )
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                # Absent, not zero — the distinction this method's contract
                # promises. It used to return 0.0 here, which made "no counter
                # yet" look like "nothing spent" and defeated every caller's
                # None check. That is unsafe in the fail-open direction: a
                # project whose counter has not been created (demo seed bills
                # with share=False) would read as $0 and reopen a budget gate
                # the local total knows is closed.
                return None
            return SpendCounterState(
                total=float(item.get("spend", 0)),
                epoch=int(item.get("epoch", 0)),
            )
        except Exception:
            logger.warning("Failed to read %s spend for %s", scope, ident, exc_info=True)
            return None

    async def get_spend(self, scope: str, ident: str) -> float | None:
        """Read a fleet-wide spend total, preserving the legacy float API."""
        state = await self.get_spend_state(scope, ident)
        return state.total if state is not None else None

    async def reset_spend_counters(
        self,
        counters: list[tuple[str, str]],
        *,
        now: datetime | None = None,
    ) -> dict[str, SpendCounterState] | None:
        """Atomically zero counters and advance their billing epochs."""
        if not counters:
            raise ValueError("at least one spend counter is required")
        if len(counters) > 99:
            raise ValueError("at most 99 spend counters may be reset")
        scopes: set[str] = set()
        for scope, ident in counters:
            self._validate_spend_identity(scope, ident)
            if scope in scopes:
                raise ValueError("a spend reset may contain one counter per scope")
            scopes.add(scope)
        if not self._enabled:
            return None
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("spend reset time must be timezone-aware")
        updated_at = current_time.isoformat()

        def _reset() -> dict[str, SpendCounterState]:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError("atomic spend reset requires transactions")
            operations = []
            for scope, ident in counters:
                operations.append(
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(
                                self._budget_counter_key(scope, ident)
                            ),
                            "UpdateExpression": (
                                "SET entity_type = :entity_type, "
                                "budget_scope = :scope, "
                                "updated_at = :updated_at, "
                                "#spend = :zero ADD #epoch :one"
                            ),
                            "ExpressionAttributeNames": {
                                "#epoch": "epoch",
                                "#spend": "spend",
                            },
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(
                                    {
                                        ":entity_type": "spend_counter",
                                        ":scope": scope,
                                        ":updated_at": updated_at,
                                        ":zero": 0,
                                        ":one": 1,
                                    }
                                )
                            ),
                        }
                    }
                )
            client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=self._api_key_transaction_token(
                    "spend-reset"
                ),
            )
            normalized = [
                (scope, ident, Decimal(0))
                for scope, ident in counters
            ]
            return self._budget_states_from_table(table, normalized)

        try:
            return await asyncio.to_thread(_reset)
        except Exception:
            for scope, ident in counters:
                self._record_write_failure(f"spend_reset_{scope}", ident)
            return None

    async def reset_spend(self, scope: str, ident: str) -> bool:
        """Advance one counter to a new zeroed billing cycle."""
        states = await self.reset_spend_counters([(scope, ident)])
        return states is not None

    # --- PolicyNode persistence ---

    @staticmethod
    def serialize_policy_node(node: PolicyNode) -> dict:
        return {
            "PK": f"POLICY_NODE#{node.node_id}",
            "SK": "CONFIG",
            "entity_type": "policy_node",
            "node_id": node.node_id,
            "node_type": node.node_type,
            "parent_id": node.parent_id,
            "display_name": node.display_name,
            "limits": json.dumps(node.limits),
            "created_at": node.created_at.isoformat(),
        }

    @staticmethod
    def deserialize_policy_node(item: dict) -> PolicyNode:
        limits_raw = item.get("limits", "{}")
        limits = json.loads(limits_raw) if isinstance(limits_raw, str) else limits_raw
        return PolicyNode(
            node_id=item["node_id"],
            node_type=item["node_type"],
            parent_id=item.get("parent_id"),
            display_name=item.get("display_name", item["node_id"]),
            limits=limits,
            created_at=datetime.fromisoformat(item["created_at"]) if "created_at" in item else datetime.now(timezone.utc),
        )

    async def save_policy_node(self, node: PolicyNode) -> None:
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            item = self.serialize_policy_node(node)
            table.put_item(Item=item)
            if node.parent_id:
                table.put_item(Item={
                    "PK": f"POLICY_NODE#{node.parent_id}",
                    "SK": f"CHILD#{node.node_id}",
                    "node_id": node.node_id,
                })

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("policy node", node.node_id)

    async def get_policy_node(self, node_id: str) -> PolicyNode | None:
        if not self._enabled:
            return None

        def _get():
            table = self._get_table()
            resp = table.get_item(Key={"PK": f"POLICY_NODE#{node_id}", "SK": "CONFIG"})
            return resp.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item:
                return self.deserialize_policy_node(item)
        except Exception:
            logger.warning("Failed to get policy node %s", node_id, exc_info=True)
        return None

    async def load_all_policy_nodes(self) -> list[PolicyNode]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(
                FilterExpression=Attr("entity_type").eq("policy_node")
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("policy_node"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            return [self.deserialize_policy_node(item) for item in raw_items]
        except Exception:
            logger.warning("Failed to load policy nodes from DynamoDB", exc_info=True)
            return []

    @staticmethod
    def serialize_tenant_policy_node(
        tenant_id: str,
        node: PolicyNode,
    ) -> dict:
        """Serialize a hierarchy node inside one tenant partition."""
        tenant_id = _require_tenant_id(tenant_id)
        item = DynamoPersistence.serialize_policy_node(node)
        item.update(
            {
                "PK": f"TENANT#{tenant_id}",
                "SK": f"POLICY_NODE#{node.node_id}",
                "entity_type": "tenant_policy_node",
                "tenant_id": tenant_id,
            }
        )
        return item

    async def save_tenant_policy_node(
        self,
        tenant_id: str,
        node: PolicyNode,
        *,
        expected_revision: int | None = None,
        create_only: bool | None = None,
    ) -> int:
        """CAS one hierarchy node and advance the tenant snapshot revision."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("tenant policy persistence is disabled")
        if expected_revision is None or create_only is None:
            snapshot = await self.load_tenant_policy_nodes_snapshot(
                tenant_id
            )
            if expected_revision is None:
                expected_revision = snapshot[1]
            if create_only is None:
                create_only = all(
                    existing.node_id != node.node_id
                    for existing in snapshot[0]
                )
        expected = _require_revision(
            expected_revision,
            name="expected policy hierarchy revision",
        )
        next_revision = expected + 1

        def _put() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "tenant policy persistence requires transactions"
                )
            version_values = {
                ":expected": expected,
                ":next": next_revision,
                ":entity_type": "tenant_policy_hierarchy_version",
                ":tenant_id": tenant_id,
                ":updated_at": datetime.now(timezone.utc).isoformat(),
            }
            version_identity = (
                "entity_type = :entity_type AND "
                "tenant_id = :tenant_id"
            )
            version_condition = (
                "(attribute_not_exists(PK) AND "
                "attribute_not_exists(SK)) OR "
                f"({version_identity} AND "
                "(attribute_not_exists(#revision) "
                "OR #revision = :expected))"
                if expected == 0
                else (
                    f"{version_identity} AND "
                    "#revision = :expected"
                )
            )
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(
                                self.serialize_tenant_policy_node(
                                    tenant_id,
                                    node,
                                )
                            ),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND "
                                "attribute_not_exists(SK)"
                                if create_only
                                else (
                                    "attribute_exists(PK) AND "
                                    "attribute_exists(SK) AND "
                                    "entity_type = :node_type AND "
                                    "tenant_id = :tenant_id"
                                )
                            ),
                            **(
                                {}
                                if create_only
                                else {
                                    "ExpressionAttributeValues": (
                                        self._serialize_dynamo_map(
                                            {
                                                ":node_type": (
                                                    "tenant_policy_node"
                                                ),
                                                ":tenant_id": tenant_id,
                                            }
                                        )
                                    )
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(
                                {
                                    "PK": f"TENANT#{tenant_id}",
                                    "SK": "POLICY_HIERARCHY#VERSION",
                                }
                            ),
                            "UpdateExpression": (
                                "SET #revision = :next, "
                                "entity_type = :entity_type, "
                                "tenant_id = :tenant_id, "
                                "updated_at = :updated_at"
                            ),
                            "ConditionExpression": version_condition,
                            "ExpressionAttributeNames": {
                                "#revision": "revision",
                            },
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(version_values)
                            ),
                        }
                    },
                ],
                ClientRequestToken=self._api_key_transaction_token(
                    "policy-node"
                ),
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if any(
                self._api_key_condition_failed(exc, index)
                for index in (0, 1)
            ):
                raise PersistenceConflictError(
                    "tenant policy hierarchy changed concurrently"
                ) from exc
            self._record_write_failure(
                "tenant policy node",
                f"{tenant_id}/{node.node_id}",
            )
            raise RuntimeError("tenant policy node write failed") from exc
        return next_revision

    async def get_tenant_policy_hierarchy_revision(
        self,
        tenant_id: str,
    ) -> int:
        """Strongly read one tenant hierarchy's monotonic revision."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("tenant policy persistence is disabled")

        def _get() -> dict | None:
            return self._get_table().get_item(
                Key={
                    "PK": f"TENANT#{tenant_id}",
                    "SK": "POLICY_HIERARCHY#VERSION",
                },
                ConsistentRead=True,
            ).get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error(
                "Failed to read tenant policy revision for %s",
                tenant_id,
                exc_info=True,
            )
            raise RuntimeError(
                "tenant policy revision read failed"
            ) from exc
        if item is None:
            return 0
        if (
            item.get("entity_type")
            != "tenant_policy_hierarchy_version"
            or item.get("tenant_id") != tenant_id
        ):
            raise RuntimeError("tenant policy revision row is malformed")
        try:
            return _require_revision(item.get("revision", 0))
        except ValueError as exc:
            raise RuntimeError(
                "tenant policy revision row is malformed"
            ) from exc

    async def load_tenant_policy_nodes_snapshot(
        self,
        tenant_id: str,
    ) -> tuple[list[PolicyNode], int]:
        """Read a hierarchy only when its revision is stable around the query."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            return [], 0

        def _query() -> list[dict]:
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            condition = (
                Key("PK").eq(f"TENANT#{tenant_id}")
                & Key("SK").begins_with("POLICY_NODE#")
            )
            items: list[dict] = []
            response = table.query(
                KeyConditionExpression=condition,
                ConsistentRead=True,
            )
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.query(
                    KeyConditionExpression=condition,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                    ConsistentRead=True,
                )
                items.extend(response.get("Items", []))
            return items

        for _attempt in range(3):
            before = await self.get_tenant_policy_hierarchy_revision(
                tenant_id
            )
            try:
                rows = await asyncio.to_thread(_query)
            except Exception as exc:
                logger.error(
                    "Failed to load tenant policy nodes for %s",
                    tenant_id,
                    exc_info=True,
                )
                raise RuntimeError(
                    "tenant policy node load failed"
                ) from exc
            after = await self.get_tenant_policy_hierarchy_revision(
                tenant_id
            )
            if before != after:
                continue
            nodes = [
                self.deserialize_policy_node(
                    self._convert_decimals_to_native(row)
                )
                for row in rows
            ]
            return nodes, after
        raise RuntimeError(
            "tenant policy hierarchy changed during repeated reads"
        )

    async def load_tenant_policy_nodes(
        self,
        tenant_id: str,
    ) -> list[PolicyNode] | None:
        """Load one tenant hierarchy; return ``None`` on a store failure."""
        try:
            nodes, _revision = (
                await self.load_tenant_policy_nodes_snapshot(tenant_id)
            )
        except Exception:
            return None
        return nodes

    # --- Cedar policy persistence ---
    #
    # Distinct from PolicyNode above, which is the cost/quota hierarchy. These are
    # the Cedar authorization statements written through POST /admin/policies. The
    # policy's ``name`` is its identity, matching that route's update-by-name
    # behaviour, so re-submitting a name overwrites rather than duplicating.

    @staticmethod
    def serialize_cedar_policy(policy: dict) -> dict:
        return {
            "PK": f"CEDAR_POLICY#{policy['name']}",
            "SK": "CONFIG",
            "entity_type": "cedar_policy",
            "name": policy["name"],
            "description": policy.get("description", ""),
            "policy_text": policy.get("policy_text", ""),
            "mode": policy.get("mode", "LOG_ONLY"),
        }

    @staticmethod
    def deserialize_cedar_policy(item: dict) -> dict:
        policy = {
            "name": item["name"],
            "description": item.get("description", ""),
            "policy_text": item.get("policy_text", ""),
            # Defaulting to LOG_ONLY on a missing attribute keeps a malformed item
            # from silently becoming enforcing.
            "mode": item.get("mode", "LOG_ONLY"),
        }
        if item.get("tenant_id") is not None:
            policy["tenant_id"] = item["tenant_id"]
        return policy

    @staticmethod
    def _tenant_cedar_policy_key(
        tenant_id: str,
        name: str,
    ) -> dict[str, str]:
        tenant_id = _require_tenant_id(tenant_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Cedar policy name must be a non-empty string")
        return {
            "PK": f"TENANT#{tenant_id}",
            "SK": f"CEDAR_POLICY#{name}",
        }

    @staticmethod
    def _tenant_cedar_version_key(tenant_id: str) -> dict[str, str]:
        return {
            "PK": f"TENANT#{_require_tenant_id(tenant_id)}",
            "SK": "CEDAR_POLICY#VERSION",
        }

    @classmethod
    def serialize_tenant_cedar_policy(
        cls,
        tenant_id: str,
        policy: dict,
    ) -> dict:
        tenant_id = _require_tenant_id(tenant_id)
        declared_tenant = policy.get("tenant_id")
        if declared_tenant not in (None, tenant_id):
            raise ValueError("Cedar policy tenant_id does not match tenant_id")
        return {
            **cls._tenant_cedar_policy_key(tenant_id, policy["name"]),
            "entity_type": "tenant_cedar_policy",
            "tenant_id": tenant_id,
            "name": policy["name"],
            "description": policy.get("description", ""),
            "policy_text": policy.get("policy_text", ""),
            "mode": policy.get("mode", "LOG_ONLY"),
        }

    async def save_tenant_cedar_policy(
        self,
        tenant_id: str,
        policy: dict,
    ) -> int | None:
        """Atomically store one tenant policy and advance its refresh version."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("tenant Cedar policy persistence is disabled")
        item = self.serialize_tenant_cedar_policy(tenant_id, policy)
        version_key = self._tenant_cedar_version_key(tenant_id)

        def _write() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "tenant Cedar policy persistence requires transactions"
                )
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(item),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._serialize_dynamo_map(version_key),
                            "UpdateExpression": (
                                "SET entity_type = :entity_type, "
                                "tenant_id = :tenant_id ADD #version :one"
                            ),
                            "ExpressionAttributeNames": {
                                "#version": "version",
                            },
                            "ExpressionAttributeValues": (
                                self._serialize_dynamo_map(
                                    {
                                        ":entity_type": (
                                            "tenant_cedar_policy_version"
                                        ),
                                        ":tenant_id": tenant_id,
                                        ":one": 1,
                                    }
                                )
                            ),
                        }
                    },
                ],
                ClientRequestToken=self._api_key_transaction_token(
                    "tenant-cedar"
                ),
            )

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            self._record_write_failure(
                "tenant Cedar policy",
                f"{tenant_id}/{policy.get('name', '?')}",
            )
            raise RuntimeError(
                "tenant Cedar policy transaction failed"
            ) from exc
        return await self.get_tenant_cedar_policy_version(tenant_id)

    async def get_tenant_cedar_policy_version(
        self,
        tenant_id: str,
    ) -> int | None:
        """Read one tenant's policy version; zero is a known empty state."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            return None

        def _get() -> dict | None:
            response = self._get_table().get_item(
                Key=self._tenant_cedar_version_key(tenant_id),
                ConsistentRead=True,
            )
            return response.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                return 0
            native = self._convert_decimals_to_native(item)
            version = native.get("version")
            if (
                native.get("entity_type")
                != "tenant_cedar_policy_version"
                or native.get("tenant_id") != tenant_id
                or not isinstance(version, int)
                or isinstance(version, bool)
                or version < 0
            ):
                raise ValueError(
                    "malformed tenant Cedar policy version row"
                )
            return version
        except Exception:
            logger.warning(
                "Failed to read tenant Cedar policy version for %s",
                tenant_id,
                exc_info=True,
            )
            return None

    async def load_tenant_cedar_policies_or_none(
        self,
        tenant_id: str,
    ) -> list[dict] | None:
        """Load only one tenant's policies; None means the read failed."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            return None

        def _query() -> list[dict]:
            from boto3.dynamodb.conditions import Key

            table = self._get_table()
            condition = (
                Key("PK").eq(f"TENANT#{tenant_id}")
                & Key("SK").begins_with("CEDAR_POLICY#")
            )
            response = table.query(
                KeyConditionExpression=condition,
                ConsistentRead=True,
            )
            items = response.get("Items", [])
            while "LastEvaluatedKey" in response:
                response = table.query(
                    KeyConditionExpression=condition,
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                    ConsistentRead=True,
                )
                items.extend(response.get("Items", []))
            return [
                item
                for item in items
                if item.get("entity_type") == "tenant_cedar_policy"
            ]

        try:
            raw_items = await asyncio.to_thread(_query)
        except Exception:
            logger.error(
                "Failed to load tenant Cedar policies for %s",
                tenant_id,
                exc_info=True,
            )
            return None
        return [
            self.deserialize_cedar_policy(
                self._convert_decimals_to_native(item)
            )
            for item in raw_items
        ]

    async def save_cedar_policy(self, policy: dict) -> None:
        if not self._enabled:
            return

        def _put():
            self._get_table().put_item(Item=self.serialize_cedar_policy(policy))

        try:
            await asyncio.to_thread(_put)
        except Exception:
            self._record_write_failure("cedar policy", policy.get("name", "?"))
            # Don't advertise a change that isn't in the table: a bumped version
            # would make every other instance re-scan and adopt a set that does
            # not include this policy, reporting a successful reload of the old
            # rules.
            return
        await self.bump_policy_version()

    async def bump_policy_version(self) -> int | None:
        """Atomically increment the shared policy version, returning the new one.

        The signal other instances poll instead of re-scanning the policy table
        on every request: one small ``GetItem`` per instance per window, and a
        full reload only when the number actually moves. Same ``ADD`` pattern as
        the spend counters, and atomic for the same reason — two operators
        writing different policies concurrently must produce two distinct
        versions, or one write is invisible to the fleet.
        """
        if not self._enabled:
            return None

        def _add():
            resp = self._get_table().update_item(
                Key={"PK": "CEDAR_POLICY#VERSION", "SK": "TOTAL"},
                UpdateExpression="ADD #v :one",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":one": Decimal("1")},
                ReturnValues="UPDATED_NEW",
            )
            return resp.get("Attributes", {}).get("version")

        try:
            version = await asyncio.to_thread(_add)
            return int(version) if version is not None else None
        except Exception:
            self._record_write_failure("cedar policy version", "TOTAL")
            return None

    async def get_policy_version(self) -> int | None:
        """Read the shared policy version, or None if it could not be read.

        Absent returns 0, not None: no policy has ever been written through the
        API, which is a *known* state and the one every gateway starts in.
        Conflating it with "unreadable" would mean the caller never records a
        successful check, so it would re-read this counter on every single request
        for the whole life of a deployment that only uses seed-file policies.

        None is reserved for a genuine read failure, so the caller can keep
        enforcing what it has and retry rather than advance its clock.
        """
        if not self._enabled:
            return None

        def _get():
            resp = self._get_table().get_item(
                Key={"PK": "CEDAR_POLICY#VERSION", "SK": "TOTAL"}
            )
            item = resp.get("Item")
            return item.get("version", 0) if item else 0

        try:
            return int(await asyncio.to_thread(_get))
        except Exception:
            logger.warning("Failed to read the shared policy version", exc_info=True)
            return None

    async def load_all_cedar_policies_or_none(self) -> list[dict] | None:
        """Like ``load_all_cedar_policies``, but None on failure instead of ``[]``.

        The original returns ``[]`` on failure so a Dynamo outage cannot block
        startup, accepting that the Cedar layer fails open. That trade is wrong
        for a live reload: adopting ``[]`` would *drop every policy the fleet is
        enforcing* because a scan timed out, turning a read failure into a
        fleet-wide authorization bypass.
        """
        if not self._enabled:
            return None
        self._last_policy_scan_failed = False
        policies = await self.load_all_cedar_policies()
        return None if self._last_policy_scan_failed else policies

    async def load_all_cedar_policies(self) -> list[dict]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr

            table = self._get_table()
            items = []
            response = table.scan(FilterExpression=Attr("entity_type").eq("cedar_policy"))
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("cedar_policy"),
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))
            return items

        try:
            raw_items = await asyncio.to_thread(_scan)
            policies = [self.deserialize_cedar_policy(item) for item in raw_items]
            self._last_policy_scan_failed = False
            return policies
        except Exception:
            # Returning [] rather than raising keeps a Dynamo outage from blocking
            # startup. It does mean booting with no policies, which — because an
            # ungoverned action is ALLOW — fails open on the Cedar layer while
            # auth, admin RBAC, and quotas stay enforced. Logged at ERROR because
            # that difference matters to whoever reads the boot log.
            #
            # A live reload must NOT accept that trade, which is what
            # ``load_all_cedar_policies_or_none`` is for: dropping the enforced
            # set because a scan failed is a bypass, not a degraded boot.
            logger.error("Failed to load Cedar policies from DynamoDB", exc_info=True)
            self._last_policy_scan_failed = True
            return []

    # --- Event destinations (webhooks / SNS / CloudWatch) ---
    #
    # Written through POST and DELETE /admin/webhooks. A destination's ``name`` is
    # its identity, matching the dispatcher's own remove-by-name behaviour.
    #
    # Stored as one item holding the whole set, not a row per destination —
    # verified necessary, not stylistic. With a row each, a *deletion* is
    # unrepresentable whenever demo seeding is on: the delete removes the row, the
    # next boot re-seeds the destination, and there is no row left to say "an
    # operator removed this". A deleted destination silently resumed receiving
    # security events. The whole-set item makes the stored list authoritative, so
    # a delete is a rewrite that survives.
    #
    # This is the same reasoning as the region topology below, and it also means
    # "saved but empty" is distinguishable from "nothing saved" — hence the
    # ``| None`` return rather than a bare list.

    @staticmethod
    def serialize_event_destinations(
        destinations: list[dict],
        *,
        revision: int = 0,
    ) -> dict:
        """Serialize the full destination set ({name, destination_type, ...} each)."""
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ValueError(
                "destination revision must be a non-negative integer"
            )
        return {
            "PK": "EVENT_DESTINATIONS",
            "SK": "CONFIG",
            "entity_type": "event_destination",
            "revision": revision,
            # json.dumps rather than a native list of maps so `event_filter=None`
            # (meaning "every event type") survives the round trip: DynamoDB drops
            # empty/None attributes, which would make an unfiltered destination
            # indistinguishable from one filtered to nothing.
            "destinations": json.dumps([
                {
                    "name": d["name"],
                    "destination_type": d.get("destination_type", "webhook"),
                    "config": d.get("config", {}),
                    "event_filter": d.get("event_filter"),
                    "enabled": bool(d.get("enabled", True)),
                }
                for d in destinations
            ]),
        }

    async def save_event_destinations(
        self,
        destinations: list[dict],
        expected_revision: int | None = None,
    ) -> int:
        """Conditionally replace the legacy destination set."""
        if not self._enabled:
            raise RuntimeError("event destination persistence is disabled")
        if expected_revision is None:
            snapshot = await self.load_event_destinations_snapshot()
            expected_revision = snapshot[1] if snapshot is not None else 0
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError(
                "expected destination revision must be a non-negative integer"
            )
        next_revision = expected_revision + 1

        def _put():
            kwargs = {
                "Item": self.serialize_event_destinations(
                    destinations,
                    revision=next_revision,
                ),
                "ExpressionAttributeNames": {"#revision": "revision"},
            }
            if expected_revision == 0:
                kwargs["ConditionExpression"] = (
                    "attribute_not_exists(#revision)"
                )
            else:
                kwargs.update(
                    {
                        "ConditionExpression": "#revision = :expected",
                        "ExpressionAttributeValues": {
                            ":expected": expected_revision
                        },
                    }
                )
            self._get_table().put_item(**kwargs)

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    "event destinations changed concurrently"
                ) from exc
            self._record_write_failure("event destinations", "set")
            raise RuntimeError("event destination write failed") from exc
        return next_revision

    async def load_event_destinations_snapshot(
        self,
    ) -> tuple[list[dict], int] | None:
        """Return the legacy destination set and its CAS revision.

        None means "fall back to seeded/config destinations"; ``[]`` means an
        operator deliberately removed every destination and nothing should be
        restored over the top.
        """
        if not self._enabled:
            raise RuntimeError("event destination persistence is disabled")

        def _get():
            return self._get_table().get_item(
                Key={"PK": "EVENT_DESTINATIONS", "SK": "CONFIG"},
                ConsistentRead=True,
            ).get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error("Failed to load event destinations from DynamoDB", exc_info=True)
            raise RuntimeError("event destination read failed") from exc
        if not item:
            return None
        revision = item.get("revision", 0)
        if (
            not isinstance(revision, (int, Decimal))
            or isinstance(revision, bool)
            or int(revision) != revision
            or revision < 0
        ):
            raise RuntimeError("event destination revision is malformed")
        raw = item.get("destinations", "[]")
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        destinations = [
            {
                "name": d["name"],
                "destination_type": d.get("destination_type", "webhook"),
                "config": d.get("config", {}),
                "event_filter": d.get("event_filter"),
                "enabled": bool(d.get("enabled", True)),
            }
            for d in parsed
        ]
        return destinations, int(revision)

    async def load_event_destinations(self) -> list[dict] | None:
        """Compatibility loader that omits the conditional-write revision."""
        snapshot = await self.load_event_destinations_snapshot()
        return snapshot[0] if snapshot is not None else None

    @staticmethod
    def serialize_tenant_event_destinations(
        tenant_id: str,
        destinations: list[dict],
        *,
        revision: int = 0,
    ) -> dict:
        """Serialize one tenant's authoritative destination set."""
        tenant_id = _require_tenant_id(tenant_id)
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ValueError("destination revision must be a non-negative integer")
        item = DynamoPersistence.serialize_event_destinations(
            destinations,
            revision=revision,
        )
        item.update(
            {
                "PK": f"TENANT#{tenant_id}",
                "SK": "EVENT_DESTINATIONS#CONFIG",
                "entity_type": "tenant_event_destination",
                "tenant_id": tenant_id,
                "revision": revision,
            }
        )
        return item

    async def save_tenant_event_destinations(
        self,
        tenant_id: str,
        destinations: list[dict],
        expected_revision: int | None = None,
    ) -> int:
        """Conditionally replace one tenant's full destination set.

        A destination set is one authoritative document so deletions survive.
        The revision condition prevents two gateway tasks from loading the same
        document and silently overwriting each other's later edits.
        """
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("tenant event destination persistence is disabled")
        if expected_revision is None:
            snapshot = await self.load_tenant_event_destinations_snapshot(
                tenant_id
            )
            expected_revision = snapshot[1] if snapshot is not None else 0
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError(
                "expected destination revision must be a non-negative integer"
            )
        next_revision = expected_revision + 1

        def _put() -> None:
            kwargs = {
                "Item": self.serialize_tenant_event_destinations(
                    tenant_id,
                    destinations,
                    revision=next_revision,
                ),
                "ExpressionAttributeNames": {"#revision": "revision"},
            }
            if expected_revision == 0:
                kwargs["ConditionExpression"] = (
                    "attribute_not_exists(#revision)"
                )
            else:
                kwargs.update(
                    {
                        "ConditionExpression": "#revision = :expected",
                        "ExpressionAttributeValues": {
                            ":expected": expected_revision
                        },
                    }
                )
            self._get_table().put_item(**kwargs)

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    "tenant event destinations changed concurrently"
                ) from exc
            self._record_write_failure(
                "tenant event destinations",
                tenant_id,
            )
            raise RuntimeError(
                "tenant event destination write failed"
            ) from exc
        return next_revision

    async def load_tenant_event_destinations_snapshot(
        self,
        tenant_id: str,
    ) -> tuple[list[dict], int] | None:
        """Load a tenant destination set and its CAS revision."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            raise RuntimeError("tenant event destination persistence is disabled")

        def _get() -> dict | None:
            response = self._get_table().get_item(
                Key={
                    "PK": f"TENANT#{tenant_id}",
                    "SK": "EVENT_DESTINATIONS#CONFIG",
                },
                ConsistentRead=True,
            )
            return response.get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error(
                "Failed to load tenant event destinations for %s",
                tenant_id,
                exc_info=True,
            )
            raise RuntimeError(
                "tenant event destination read failed"
            ) from exc
        if not item:
            return None
        if item.get("tenant_id") != tenant_id:
            raise RuntimeError("tenant event destination row is malformed")
        revision = item.get("revision", 0)
        if (
            not isinstance(revision, (int, Decimal))
            or isinstance(revision, bool)
            or int(revision) != revision
            or revision < 0
        ):
            raise RuntimeError("tenant event destination revision is malformed")
        raw = item.get("destinations", "[]")
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        destinations = [
            {
                "name": destination["name"],
                "destination_type": destination.get(
                    "destination_type",
                    "webhook",
                ),
                "config": destination.get("config", {}),
                "event_filter": destination.get("event_filter"),
                "enabled": bool(destination.get("enabled", True)),
            }
            for destination in parsed
        ]
        return destinations, int(revision)

    async def load_tenant_event_destinations(
        self,
        tenant_id: str,
    ) -> list[dict] | None:
        """Compatibility loader that omits the conditional-write revision."""
        snapshot = await self.load_tenant_event_destinations_snapshot(tenant_id)
        return snapshot[0] if snapshot is not None else None

    # --- Multi-region topology (hub config + spokes) ---
    #
    # Written through PUT /admin/regions/config and the /admin/regions/spokes
    # routes. Stored as one item rather than a row per spoke: the hub-level
    # settings and the spoke list are edited as a unit, a spoke's
    # ``failover_priority`` is only meaningful relative to its siblings, and a
    # partial read of a topology would route traffic on a set of regions no
    # operator configured.
    #
    # ``status`` is deliberately not persisted. It is health-check state, not
    # configuration; restoring a stale UNHEALTHY would keep a recovered region out
    # of rotation until the next probe, and a stale HEALTHY would send traffic to a
    # region that is still down. Spokes come back at their dataclass default and
    # the first health check corrects it.

    @staticmethod
    def serialize_region_topology(config, *, revision: int | None = None) -> dict:
        if revision is None:
            revision = getattr(config, "revision", 0)
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ValueError("topology revision must be a non-negative integer")
        return {
            "PK": "REGION_TOPOLOGY",
            "SK": "CONFIG",
            "entity_type": "region_topology",
            "revision": revision,
            "hub_region": config.hub_region,
            "health_check_interval_seconds": config.health_check_interval_seconds,
            "failover_threshold_consecutive": config.failover_threshold_consecutive,
            "failover_cooldown_seconds": config.failover_cooldown_seconds,
            "data_residency_strict": bool(config.data_residency_strict),
            "spokes": json.dumps([
                {
                    "region": s.region,
                    "role": s.role.value,
                    "weight": s.weight,
                    "endpoint": s.endpoint,
                    "providers": s.providers,
                    "models": s.models,
                    "data_residency_zones": s.data_residency_zones,
                    "health_check_url": s.health_check_url,
                    "max_latency_ms": s.max_latency_ms,
                    "failover_priority": s.failover_priority,
                }
                for s in config.spokes
            ]),
        }

    async def save_region_topology(
        self,
        config,
        expected_revision: int | None = None,
    ) -> int | None:
        """Conditionally replace the topology and return its new revision."""
        if not self._enabled:
            return None
        if expected_revision is None:
            expected_revision = getattr(config, "revision", 0)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError(
                "expected topology revision must be a non-negative integer"
            )
        next_revision = expected_revision + 1

        def _put():
            kwargs = {
                "Item": self.serialize_region_topology(
                    config,
                    revision=next_revision,
                ),
                "ExpressionAttributeNames": {"#revision": "revision"},
            }
            if expected_revision == 0:
                kwargs["ConditionExpression"] = (
                    "attribute_not_exists(#revision)"
                )
            else:
                kwargs.update(
                    {
                        "ConditionExpression": "#revision = :expected",
                        "ExpressionAttributeValues": {
                            ":expected": expected_revision
                        },
                    }
                )
            self._get_table().put_item(**kwargs)

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if self._api_key_condition_failed(exc, 0):
                raise PersistenceConflictError(
                    "region topology changed concurrently"
                ) from exc
            self._record_write_failure("region topology", config.hub_region)
            return None
        return next_revision

    async def load_region_topology_snapshot(self) -> dict | None:
        """Return a consistent topology snapshot or raise on store failure."""
        if not self._enabled:
            return None

        def _get():
            return self._get_table().get_item(
                Key={"PK": "REGION_TOPOLOGY", "SK": "CONFIG"},
                ConsistentRead=True,
            ).get("Item")

        try:
            item = await asyncio.to_thread(_get)
        except Exception as exc:
            logger.error(
                "Failed to load region topology from DynamoDB",
                exc_info=True,
            )
            raise RuntimeError("region topology read failed") from exc
        if not item:
            return None
        revision = item.get("revision", 0)
        if (
            not isinstance(revision, (int, Decimal))
            or isinstance(revision, bool)
            or int(revision) != revision
            or revision < 0
        ):
            raise RuntimeError("region topology revision is malformed")
        spokes_raw = item.get("spokes", "[]")
        return {
            "revision": int(revision),
            "hub_region": item.get("hub_region", ""),
            "health_check_interval_seconds": int(
                item.get("health_check_interval_seconds", 30)
            ),
            "failover_threshold_consecutive": int(
                item.get("failover_threshold_consecutive", 3)
            ),
            "failover_cooldown_seconds": int(
                item.get("failover_cooldown_seconds", 60)
            ),
            "data_residency_strict": bool(
                item.get("data_residency_strict", False)
            ),
            "spokes": (
                json.loads(spokes_raw)
                if isinstance(spokes_raw, str)
                else spokes_raw
            ),
        }

    async def load_region_topology(self) -> dict | None:
        """Return the stored topology as a dict, or None if none was saved.

        None and "saved but empty" are different states: the first means fall back
        to the config file, the second means an operator removed every spoke.
        """
        return await self.load_region_topology_snapshot()

    # --- SCIM identity (users + groups) ---

    @staticmethod
    def _scim_user_key(
        user_id: str,
        tenant_id: str = "",
    ) -> dict[str, str]:
        if not tenant_id:
            return {"PK": f"SCIM#USER#{user_id}", "SK": "SCIM_USER"}
        return {
            "PK": f"TENANT#{_require_tenant_id(tenant_id)}",
            "SK": f"SCIM#USER#{user_id}",
        }

    @staticmethod
    def _scim_group_key(
        group_id: str,
        tenant_id: str = "",
    ) -> dict[str, str]:
        if not tenant_id:
            return {"PK": f"SCIM#GROUP#{group_id}", "SK": "SCIM_GROUP"}
        return {
            "PK": f"TENANT#{_require_tenant_id(tenant_id)}",
            "SK": f"SCIM#GROUP#{group_id}",
        }

    @staticmethod
    def _scim_username_key(
        tenant_id: str,
        user_name: str,
    ) -> dict[str, str]:
        import hashlib

        digest = hashlib.sha256(user_name.casefold().encode()).hexdigest()
        return {
            "PK": f"TENANT#{_require_tenant_id(tenant_id)}",
            "SK": f"SCIM#USERNAME#{digest}",
        }

    @staticmethod
    def _tenant_scim_version_key(tenant_id: str) -> dict[str, str]:
        return {
            "PK": f"TENANT#{_require_tenant_id(tenant_id)}",
            "SK": "SCIM#VERSION",
        }

    def _tenant_scim_version_operation(
        self,
        tenant_id: str,
    ) -> dict:
        tenant_id = _require_tenant_id(tenant_id)
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": self._serialize_dynamo_map(
                    self._tenant_scim_version_key(tenant_id)
                ),
                "UpdateExpression": (
                    "SET entity_type = :entity_type, "
                    "tenant_id = :tenant_id ADD #version :one"
                ),
                "ExpressionAttributeNames": {
                    "#version": "version",
                },
                "ExpressionAttributeValues": self._serialize_dynamo_map(
                    {
                        ":entity_type": "tenant_scim_version",
                        ":tenant_id": tenant_id,
                        ":one": 1,
                    }
                ),
            }
        }

    @staticmethod
    def _serialize_scim_user(user: ScimUser) -> dict:
        item = {
            **DynamoPersistence._scim_user_key(user.id, user.tenant_id),
            "entity_type": "scim_user",
            "id": user.id,
            "user_name": user.user_name,
            "active": user.active,
            "external_id": user.external_id or "",
            "display_name": user.display_name,
            "emails": user.emails,
            "groups": user.groups,
            "roles": user.roles,
            "project_id": user.project_id,
            "project_ids": user.project_ids,
            "issuer": user.issuer,
            "subject": user.subject,
            "authorization_version": user.authorization_version,
            "deleted": user.deleted,
            "created_at": user.created_at.isoformat(),
            "updated_at": user.updated_at.isoformat(),
        }
        if user.tenant_id:
            item["tenant_id"] = user.tenant_id
        return item

    @staticmethod
    def _deserialize_scim_user(item: dict) -> ScimUser:
        from datetime import datetime as _dt

        return ScimUser(
            id=item["id"],
            user_name=item["user_name"],
            tenant_id=item.get("tenant_id", ""),
            issuer=item.get("issuer", ""),
            subject=item.get("subject", ""),
            active=bool(item.get("active", True)),
            external_id=item.get("external_id") or None,
            display_name=item.get("display_name", ""),
            emails=list(item.get("emails", []) or []),
            groups=list(item.get("groups", []) or []),
            roles=list(item.get("roles", []) or []),
            project_id=item.get("project_id", ""),
            project_ids=list(item.get("project_ids", []) or []),
            authorization_version=int(
                item.get("authorization_version", 1)
            ),
            deleted=bool(item.get("deleted", False)),
            created_at=(
                _dt.fromisoformat(item["created_at"])
                if item.get("created_at")
                else datetime.now(timezone.utc)
            ),
            updated_at=(
                _dt.fromisoformat(item["updated_at"])
                if item.get("updated_at")
                else datetime.now(timezone.utc)
            ),
        )

    @staticmethod
    def _serialize_scim_group(group: ScimGroup) -> dict:
        item = {
            **DynamoPersistence._scim_group_key(
                group.id,
                group.tenant_id,
            ),
            "entity_type": "scim_group",
            "id": group.id,
            "display_name": group.display_name,
            "external_id": group.external_id or "",
            "members": group.members,
            "roles": group.roles,
            "authorization_version": group.authorization_version,
            "deleted": group.deleted,
            "created_at": group.created_at.isoformat(),
            "updated_at": group.updated_at.isoformat(),
        }
        if group.tenant_id:
            item["tenant_id"] = group.tenant_id
        return item

    @staticmethod
    def _deserialize_scim_group(item: dict) -> ScimGroup:
        from datetime import datetime as _dt

        return ScimGroup(
            id=item["id"],
            display_name=item["display_name"],
            tenant_id=item.get("tenant_id", ""),
            external_id=item.get("external_id") or None,
            members=list(item.get("members", []) or []),
            roles=list(item.get("roles", []) or []),
            authorization_version=int(
                item.get("authorization_version", 1)
            ),
            deleted=bool(item.get("deleted", False)),
            created_at=(
                _dt.fromisoformat(item["created_at"])
                if item.get("created_at")
                else datetime.now(timezone.utc)
            ),
            updated_at=(
                _dt.fromisoformat(item["updated_at"])
                if item.get("updated_at")
                else datetime.now(timezone.utc)
            ),
        )

    async def save_scim_user(self, user: ScimUser) -> None:
        if not self._enabled:
            return
        item = self._serialize_scim_user(user)
        try:
            await asyncio.to_thread(lambda: self._get_table().put_item(Item=item))
        except Exception as exc:
            self._record_write_failure("SCIM user", user.id)
            raise RuntimeError("SCIM user persistence failed") from exc

    @staticmethod
    def _validate_scim_principal(
        user: ScimUser,
        principal: Principal,
    ) -> None:
        from src.gateway.models import MembershipStatus

        expected_status = (
            MembershipStatus.ACTIVE
            if user.active and not user.deleted
            else MembershipStatus.DEPROVISIONED
        )
        expected_projects = {
            project_id
            for project_id in user.project_ids
            if project_id.strip()
        }
        if user.project_id.strip():
            expected_projects.add(user.project_id)
        if (
            not user.tenant_id
            or not user.issuer
            or not user.subject
            or principal.principal_id != f"scim:{user.id}"
            or principal.tenant_id != user.tenant_id
            or principal.issuer != user.issuer
            or principal.subject != user.subject
            or principal.authorization_version
            != user.authorization_version
            or principal.membership_status is not expected_status
            or principal.project_ids != frozenset(expected_projects)
        ):
            raise ValueError(
                "SCIM principal does not match provisioned identity"
            )

    async def save_scim_user_with_principal(
        self,
        user: ScimUser,
        principal: Principal,
        *,
        expected_authorization_version: int | None,
        previous_user: ScimUser | None,
    ) -> None:
        """Atomically create or version-update a tenant SCIM identity."""
        self._validate_scim_principal(user, principal)
        if expected_authorization_version is None:
            if previous_user is not None or user.authorization_version != 1:
                raise ValueError(
                    "new SCIM users must start at authorization_version 1"
                )
        else:
            if (
                previous_user is None
                or previous_user.authorization_version
                != expected_authorization_version
                or user.authorization_version
                != expected_authorization_version + 1
                or previous_user.id != user.id
                or previous_user.tenant_id != user.tenant_id
                or previous_user.issuer != user.issuer
                or previous_user.subject != user.subject
            ):
                raise ValueError("invalid SCIM authorization version update")
        if not self._enabled:
            raise RuntimeError("canonical SCIM persistence is disabled")

        def _write() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "canonical SCIM persistence requires transactions"
                )
            principal_row = self._serialize_principal(principal)
            user_row = self._serialize_scim_user(user)
            if expected_authorization_version is None:
                condition = (
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                )
                operations = [
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(user_row),
                            "ConditionExpression": condition,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(
                                {
                                    **self._scim_username_key(
                                        user.tenant_id,
                                        user.user_name,
                                    ),
                                    "entity_type": "scim_username_lookup",
                                    "tenant_id": user.tenant_id,
                                    "user_id": user.id,
                                }
                            ),
                            "ConditionExpression": condition,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(principal_row),
                            "ConditionExpression": condition,
                        }
                    },
                ]
            else:
                expected_values = self._serialize_dynamo_map(
                    {":expected": expected_authorization_version}
                )
                operations = [
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(user_row),
                            "ConditionExpression": (
                                "authorization_version = :expected"
                            ),
                            "ExpressionAttributeValues": expected_values,
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._serialize_dynamo_map(principal_row),
                            "ConditionExpression": (
                                "authorization_version = :expected"
                            ),
                            "ExpressionAttributeValues": expected_values,
                        }
                    },
                ]
                assert previous_user is not None
                old_username = previous_user.user_name.casefold()
                new_username = user.user_name.casefold()
                if user.deleted or old_username != new_username:
                    old_key = self._scim_username_key(
                        user.tenant_id,
                        previous_user.user_name,
                    )
                    operations.append(
                        {
                            "Delete": {
                                "TableName": self._table_name,
                                "Key": self._serialize_dynamo_map(old_key),
                                "ConditionExpression": "user_id = :user_id",
                                "ExpressionAttributeValues": (
                                    self._serialize_dynamo_map(
                                        {":user_id": user.id}
                                    )
                                ),
                            }
                        }
                    )
                if not user.deleted and old_username != new_username:
                    operations.append(
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": self._serialize_dynamo_map(
                                    {
                                        **self._scim_username_key(
                                            user.tenant_id,
                                            user.user_name,
                                        ),
                                        "entity_type": (
                                            "scim_username_lookup"
                                        ),
                                        "tenant_id": user.tenant_id,
                                        "user_id": user.id,
                                    }
                                ),
                                "ConditionExpression": (
                                    "attribute_not_exists(PK) "
                                    "AND attribute_not_exists(SK)"
                                ),
                            }
                        }
                    )
            operations.append(
                self._tenant_scim_version_operation(user.tenant_id)
            )
            client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=self._api_key_transaction_token("scim-user"),
            )

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            if any(
                self._api_key_condition_failed(exc, index)
                for index in range(5)
            ):
                from src.gateway.auth.scim_service import ScimConflictError

                raise ScimConflictError(
                    "SCIM identity changed concurrently or already exists"
                ) from exc
            self._record_write_failure("SCIM user/principal", user.id)
            raise RuntimeError(
                "SCIM identity transaction failed"
            ) from exc

    async def save_scim_group(self, group: ScimGroup) -> None:
        if not self._enabled:
            return
        item = self._serialize_scim_group(group)
        try:
            await asyncio.to_thread(lambda: self._get_table().put_item(Item=item))
        except Exception as exc:
            self._record_write_failure("SCIM group", group.id)
            raise RuntimeError("SCIM group persistence failed") from exc

    async def save_scim_group_with_principals(
        self,
        group: ScimGroup,
        *,
        expected_authorization_version: int | None,
        previous_group: ScimGroup | None,
        user_updates: list[tuple[ScimUser, Principal, int]],
    ) -> None:
        """Atomically version a group and every principal its roles affect."""
        if not group.tenant_id:
            raise ValueError("canonical SCIM groups require tenant_id")
        if len(user_updates) > 49:
            raise ValueError(
                "SCIM group transactions support at most 49 affected users"
            )
        if expected_authorization_version is None:
            if (
                previous_group is not None
                or group.authorization_version != 1
            ):
                raise ValueError(
                    "new SCIM groups must start at authorization_version 1"
                )
        elif (
            previous_group is None
            or previous_group.id != group.id
            or previous_group.tenant_id != group.tenant_id
            or previous_group.authorization_version
            != expected_authorization_version
            or group.authorization_version
            != expected_authorization_version + 1
        ):
            raise ValueError("invalid SCIM group authorization version update")
        for user, principal, expected in user_updates:
            self._validate_scim_principal(user, principal)
            if (
                user.tenant_id != group.tenant_id
                or user.authorization_version != expected + 1
            ):
                raise ValueError(
                    "SCIM group user update has an invalid version"
                )
        if not self._enabled:
            raise RuntimeError("canonical SCIM persistence is disabled")

        def _write() -> None:
            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "canonical SCIM persistence requires transactions"
                )
            group_operation: dict
            if expected_authorization_version is None:
                group_operation = {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": self._serialize_dynamo_map(
                            self._serialize_scim_group(group)
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(PK) "
                            "AND attribute_not_exists(SK)"
                        ),
                    }
                }
            else:
                group_operation = {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": self._serialize_dynamo_map(
                            self._serialize_scim_group(group)
                        ),
                        "ConditionExpression": (
                            "authorization_version = :expected"
                        ),
                        "ExpressionAttributeValues": (
                            self._serialize_dynamo_map(
                                {
                                    ":expected": (
                                        expected_authorization_version
                                    )
                                }
                            )
                        ),
                    }
                }
            operations = [group_operation]
            for user, principal, expected in user_updates:
                expected_values = self._serialize_dynamo_map(
                    {":expected": expected}
                )
                operations.extend(
                    [
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": self._serialize_dynamo_map(
                                    self._serialize_scim_user(user)
                                ),
                                "ConditionExpression": (
                                    "authorization_version = :expected"
                                ),
                                "ExpressionAttributeValues": expected_values,
                            }
                        },
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": self._serialize_dynamo_map(
                                    self._serialize_principal(principal)
                                ),
                                "ConditionExpression": (
                                    "authorization_version = :expected"
                                ),
                                "ExpressionAttributeValues": expected_values,
                            }
                        },
                    ]
                )
            operations.append(
                self._tenant_scim_version_operation(group.tenant_id)
            )
            client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=self._api_key_transaction_token(
                    "scim-group"
                ),
            )

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            if any(
                self._api_key_condition_failed(exc, index)
                for index in range(1 + 2 * len(user_updates))
            ):
                from src.gateway.auth.scim_service import ScimConflictError

                raise ScimConflictError(
                    "SCIM group or membership changed concurrently"
                ) from exc
            self._record_write_failure("SCIM group/principals", group.id)
            raise RuntimeError(
                "SCIM group transaction failed"
            ) from exc

    async def set_tenant_project_membership(
        self,
        tenant_id: str,
        project_id: str,
        scim_user_id: str,
        *,
        granted: bool,
    ) -> tuple[Project, bool]:
        """Atomically align a project member and its canonical principal grant."""
        tenant_id = _require_tenant_id(tenant_id)
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if not isinstance(scim_user_id, str) or not scim_user_id.strip():
            raise ValueError("user_id must be a non-empty SCIM user id")
        if not self._enabled:
            raise RuntimeError("canonical project membership is disabled")

        project_key = {
            "PK": tenant_project_partition_key(tenant_id),
            "SK": tenant_project_sort_key(project_id),
        }
        user_key = self._scim_user_key(scim_user_id, tenant_id)

        def _write() -> tuple[Project, bool]:
            from src.gateway.auth.dynamo_principal_repository import (
                DynamoPrincipalRepository,
                identity_partition_key,
                membership_sort_key,
            )

            table = self._get_table()
            client = getattr(getattr(table, "meta", None), "client", None)
            if client is None:
                raise RuntimeError(
                    "canonical project membership requires transactions"
                )

            project_response = table.get_item(
                Key=project_key,
                ConsistentRead=True,
            )
            user_response = table.get_item(
                Key=user_key,
                ConsistentRead=True,
            )
            project_row = project_response.get("Item")
            user_row = user_response.get("Item")
            if not isinstance(project_row, dict):
                raise CanonicalMembershipNotFoundError(
                    f"Project '{project_id}' not found"
                )
            if not isinstance(user_row, dict):
                raise CanonicalMembershipNotFoundError(
                    f"SCIM user '{scim_user_id}' not found"
                )

            project = self.deserialize_project(
                self._convert_decimals_to_native(project_row)
            )
            user = self._deserialize_scim_user(
                self._convert_decimals_to_native(user_row)
            )
            if (
                project.tenant_id != tenant_id
                or project.project_id != project_id
                or user.tenant_id != tenant_id
                or user.id != scim_user_id
            ):
                raise RuntimeError(
                    "canonical project membership row is malformed"
                )

            principal_key = {
                "PK": identity_partition_key(user.issuer, user.subject),
                "SK": membership_sort_key(tenant_id),
            }
            principal_response = table.get_item(
                Key=principal_key,
                ConsistentRead=True,
            )
            principal_row = principal_response.get("Item")
            if not isinstance(principal_row, dict):
                raise CanonicalMembershipNotFoundError(
                    f"Principal for SCIM user '{scim_user_id}' not found"
                )
            principal = DynamoPrincipalRepository.deserialize(
                self._convert_decimals_to_native(principal_row)
            )
            self._validate_scim_principal(user, principal)

            principal_id = f"scim:{scim_user_id}"
            current_grant = project_id in principal.project_ids
            listed = principal_id in project.members
            if granted and current_grant and listed:
                return project, False
            if not granted and not current_grant and not listed:
                return project, False
            if granted and (not user.active or user.deleted):
                raise ValueError(
                    "cannot grant a project to a deprovisioned SCIM user"
                )

            members = [
                member
                for member in project.members
                if member not in {scim_user_id, principal_id}
            ]
            if granted:
                members.append(principal_id)
            updated_project = replace(
                project,
                members=members,
                revision=project.revision + 1,
            )

            operator_projects = set(user.project_ids)
            idp_project = user.project_id
            if granted:
                operator_projects.add(project_id)
            else:
                operator_projects.discard(project_id)
                if idp_project == project_id:
                    idp_project = ""
            updated_user = replace(
                user,
                project_id=idp_project,
                project_ids=sorted(operator_projects),
                authorization_version=user.authorization_version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            effective_projects = set(updated_user.project_ids)
            if updated_user.project_id:
                effective_projects.add(updated_user.project_id)
            updated_principal = replace(
                principal,
                project_ids=frozenset(effective_projects),
                authorization_version=(
                    principal.authorization_version + 1
                ),
            )
            self._validate_scim_principal(
                updated_user,
                updated_principal,
            )

            expected_project_values = self._serialize_dynamo_map(
                {
                    ":entity_type": "project",
                    ":tenant_id": tenant_id,
                    ":project_id": project_id,
                    ":expected_revision": project.revision,
                    ":zero": 0,
                }
            )
            expected_version_values = self._serialize_dynamo_map(
                {":expected": user.authorization_version}
            )
            operations = [
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": self._serialize_dynamo_map(
                            self._convert_floats_to_decimal(
                                self.serialize_project(updated_project)
                            )
                        ),
                        "ConditionExpression": (
                            "entity_type = :entity_type "
                            "AND tenant_id = :tenant_id "
                            "AND project_id = :project_id "
                            "AND (#revision = :expected_revision "
                            "OR (attribute_not_exists(#revision) "
                            "AND :expected_revision = :zero))"
                        ),
                        "ExpressionAttributeNames": {
                            "#revision": "revision",
                        },
                        "ExpressionAttributeValues": (
                            expected_project_values
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": self._serialize_dynamo_map(
                            self._serialize_scim_user(updated_user)
                        ),
                        "ConditionExpression": (
                            "authorization_version = :expected"
                        ),
                        "ExpressionAttributeValues": (
                            expected_version_values
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": self._serialize_dynamo_map(
                            self._serialize_principal(
                                updated_principal
                            )
                        ),
                        "ConditionExpression": (
                            "authorization_version = :expected"
                        ),
                        "ExpressionAttributeValues": (
                            expected_version_values
                        ),
                    }
                },
                self._tenant_scim_version_operation(tenant_id),
            ]
            client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=self._api_key_transaction_token(
                    "project-membership"
                ),
            )
            return updated_project, True

        try:
            return await asyncio.to_thread(_write)
        except (
            CanonicalMembershipNotFoundError,
            ValueError,
        ):
            raise
        except Exception as exc:
            if any(
                self._api_key_condition_failed(exc, index)
                for index in range(3)
            ):
                raise CanonicalMembershipConflictError(
                    "project membership changed concurrently"
                ) from exc
            self._record_write_failure(
                "canonical project membership",
                f"{tenant_id}/{project_id}/{scim_user_id}",
            )
            raise RuntimeError(
                "canonical project membership transaction failed"
            ) from exc

    async def get_tenant_scim_version(
        self,
        tenant_id: str,
    ) -> int | None:
        """Read a tenant's SCIM version; zero is a known empty directory."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            return None

        def _get() -> dict | None:
            response = self._get_table().get_item(
                Key=self._tenant_scim_version_key(tenant_id),
                ConsistentRead=True,
            )
            return response.get("Item")

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                return 0
            native = self._convert_decimals_to_native(item)
            version = native.get("version")
            if (
                native.get("entity_type") != "tenant_scim_version"
                or native.get("tenant_id") != tenant_id
                or not isinstance(version, int)
                or isinstance(version, bool)
                or version < 0
            ):
                raise ValueError("malformed tenant SCIM version row")
            return version
        except Exception:
            logger.error(
                "Failed to read tenant SCIM version for %s",
                tenant_id,
                exc_info=True,
            )
            return None

    async def load_tenant_scim_snapshot_or_none(
        self,
        tenant_id: str,
    ) -> tuple[list[ScimUser], list[ScimGroup]] | None:
        """Strongly load one tenant's SCIM directory; None means read failure."""
        tenant_id = _require_tenant_id(tenant_id)
        if not self._enabled:
            return None

        def _query() -> list[dict]:
            from boto3.dynamodb.conditions import Key

            condition = (
                Key("PK").eq(f"TENANT#{tenant_id}")
                & Key("SK").begins_with("SCIM#")
            )
            kwargs = {
                "KeyConditionExpression": condition,
                "ConsistentRead": True,
            }
            items: list[dict] = []
            table = self._get_table()
            while True:
                response = table.query(**kwargs)
                page = response.get("Items", [])
                if not isinstance(page, list):
                    raise ValueError("DynamoDB query returned malformed Items")
                items.extend(page)
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    return items
                kwargs["ExclusiveStartKey"] = last_key

        try:
            raw_items = await asyncio.to_thread(_query)
            native_items = [
                self._convert_decimals_to_native(item)
                for item in raw_items
            ]
            users = [
                self._deserialize_scim_user(item)
                for item in native_items
                if item.get("entity_type") == "scim_user"
            ]
            groups = [
                self._deserialize_scim_group(item)
                for item in native_items
                if item.get("entity_type") == "scim_group"
            ]
            if any(user.tenant_id != tenant_id for user in users) or any(
                group.tenant_id != tenant_id for group in groups
            ):
                raise ValueError(
                    "tenant SCIM query returned a mismatched owner"
                )
            return users, groups
        except Exception:
            logger.error(
                "Failed to load tenant SCIM snapshot for %s",
                tenant_id,
                exc_info=True,
            )
            return None

    async def delete_scim_user(
        self,
        user_id: str,
        tenant_id: str = "",
    ) -> None:
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(
                lambda: self._get_table().delete_item(
                    Key=self._scim_user_key(user_id, tenant_id)
                )
            )
        except Exception as exc:
            self._record_write_failure("SCIM user delete", user_id)
            raise RuntimeError("SCIM user delete failed") from exc

    async def delete_scim_group(
        self,
        group_id: str,
        tenant_id: str = "",
    ) -> None:
        if not self._enabled:
            return
        try:
            await asyncio.to_thread(
                lambda: self._get_table().delete_item(
                    Key=self._scim_group_key(group_id, tenant_id)
                )
            )
        except Exception as exc:
            self._record_write_failure("SCIM group delete", group_id)
            raise RuntimeError("SCIM group delete failed") from exc

    async def load_scim_users(self) -> list[ScimUser]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr
            table = self._get_table()
            items, response = [], table.scan(
                FilterExpression=Attr("entity_type").eq("scim_user"))
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("scim_user"),
                    ExclusiveStartKey=response["LastEvaluatedKey"])
                items.extend(response.get("Items", []))
            return items

        try:
            raw = await asyncio.to_thread(_scan)
            return [self._deserialize_scim_user(self._convert_decimals_to_native(i)) for i in raw]
        except Exception as exc:
            logger.warning("Failed to load SCIM users from DynamoDB", exc_info=True)
            raise RuntimeError("SCIM user load failed") from exc

    async def load_scim_groups(self) -> list[ScimGroup]:
        if not self._enabled:
            return []

        def _scan():
            from boto3.dynamodb.conditions import Attr
            table = self._get_table()
            items, response = [], table.scan(
                FilterExpression=Attr("entity_type").eq("scim_group"))
            items.extend(response.get("Items", []))
            while "LastEvaluatedKey" in response:
                response = table.scan(
                    FilterExpression=Attr("entity_type").eq("scim_group"),
                    ExclusiveStartKey=response["LastEvaluatedKey"])
                items.extend(response.get("Items", []))
            return items

        try:
            raw = await asyncio.to_thread(_scan)
            return [self._deserialize_scim_group(self._convert_decimals_to_native(i)) for i in raw]
        except Exception as exc:
            logger.warning("Failed to load SCIM groups from DynamoDB", exc_info=True)
            raise RuntimeError("SCIM group load failed") from exc

    # --- Generic item write (used by audit trail) ---

    async def put_item(self, item: dict) -> None:
        """Write a raw item to DynamoDB. Used by subsystems that manage their own schema."""
        if not self._enabled:
            return

        def _put():
            table = self._get_table()
            table.put_item(Item=item)

        await asyncio.to_thread(_put)
