"""Tenant-scoped security event dispatch to external systems."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

from src.gateway.security.audit_trail import LEGACY_TENANT_ID

logger = logging.getLogger(__name__)

_DNS_RESOLUTION_TIMEOUT_SECONDS = 3.0
_MAX_WEBHOOK_TIMEOUT_SECONDS = 30.0
_OUTBOX_SCHEMA_VERSION = 1
_OUTBOX_WAIT_TIME_SECONDS = 10
_OUTBOX_RETRY_BASE_SECONDS = 5
_OUTBOX_RETRY_MAX_SECONDS = 300
_OUTBOX_MAX_MESSAGE_BYTES = 256 * 1024
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_AWS_REGION_RE = re.compile(r"[a-z]{2}(?:-gov)?-[a-z0-9-]+-\d\Z")
_AWS_ACCOUNT_RE = re.compile(r"\d{12}\Z")
_SNS_TOPIC_RE = re.compile(r"(?=.{1,256}\Z)[A-Za-z0-9_-]+(?:\.fifo)?\Z")
_SQS_FIFO_QUEUE_RE = re.compile(r"[A-Za-z0-9_-]{1,75}\.fifo\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_LOG_GROUP_RE = re.compile(r"[.\-_/#A-Za-z0-9]{1,512}\Z")
_LOG_STREAM_RE = re.compile(r"[^:*]{1,512}\Z")
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_INTERNAL_HOST_SUFFIXES = (
    ".corp",
    ".example",
    ".home",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
)
_CONTROLLED_WEBHOOK_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "content-type",
        "host",
        "idempotency-key",
        "proxy-authorization",
        "transfer-encoding",
        "x-axon-event-id",
    }
)

HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]
DestinationRefresher = Callable[[str], Awaitable[None]]


class DestinationValidationError(ValueError):
    """An outbound event destination is unsafe or malformed."""


def _fifo_queue_url(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("security event outbox queue URL must be a non-empty string")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("security event outbox queue URL is invalid") from exc
    queue_name = parsed.path.rsplit("/", 1)[-1]
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _SQS_FIFO_QUEUE_RE.fullmatch(queue_name)
    ):
        raise ValueError("security event outbox requires a FIFO SQS queue URL")
    return value


@dataclass(frozen=True)
class _WebhookURL:
    hostname: str
    host_header: str
    port: int
    path: str
    query: str


@dataclass(frozen=True)
class _ValidatedWebhookTarget:
    connect_url: str
    hostname: str
    host_header: str


@dataclass(frozen=True)
class _SNSTarget:
    topic_arn: str
    region: str


@dataclass(frozen=True)
class _CloudWatchTarget:
    log_group: str
    log_stream: str
    region: str


def _partition_for_region(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


def _canonical_hostname(hostname: str) -> str:
    if "%" in hostname or "\\" in hostname:
        raise DestinationValidationError("webhook hostname contains invalid encoding")
    hostname = hostname.rstrip(".").lower()
    if not hostname:
        raise DestinationValidationError("webhook URL requires a hostname")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            legacy_ipv4 = socket.inet_aton(hostname)
        except OSError:
            pass
        else:
            return ipaddress.IPv4Address(legacy_ipv4).compressed
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise DestinationValidationError("webhook hostname is invalid") from exc
        if len(hostname) > 253:
            raise DestinationValidationError("webhook hostname is too long")
        labels = hostname.split(".")
        if len(labels) < 2:
            raise DestinationValidationError(
                "webhook hostname must be a public fully-qualified domain name"
            )
        if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
            raise DestinationValidationError("webhook hostname is invalid")
        if any(
            hostname == suffix[1:] or hostname.endswith(suffix)
            for suffix in _INTERNAL_HOST_SUFFIXES
        ):
            raise DestinationValidationError(
                "webhook hostname uses a private or special-use namespace"
            )
        return hostname
    return address.compressed


def _parse_webhook_url(value: object) -> _WebhookURL:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DestinationValidationError("webhook config.url must be a non-empty string")
    if len(value) > 2048 or any(
        ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise DestinationValidationError("webhook URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
        hostname = parsed.hostname
    except ValueError as exc:
        raise DestinationValidationError("webhook URL is invalid") from exc
    if parsed.scheme.lower() != "https":
        raise DestinationValidationError("webhook URL must use HTTPS")
    if not parsed.netloc or hostname is None:
        raise DestinationValidationError("webhook URL requires a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise DestinationValidationError("webhook URL must not contain user information")
    if "\\" in parsed.netloc or parsed.netloc.endswith(":"):
        raise DestinationValidationError("webhook URL authority is invalid")
    if parsed.fragment:
        raise DestinationValidationError("webhook URL must not contain a fragment")
    if not 1 <= port <= 65535:
        raise DestinationValidationError("webhook URL port is invalid")

    hostname = _canonical_hostname(hostname)
    host_literal = f"[{hostname}]" if ":" in hostname else hostname
    explicit_port = parsed.port is not None
    host_header = f"{host_literal}:{port}" if explicit_port else host_literal
    return _WebhookURL(
        hostname=hostname,
        host_header=host_header,
        port=port,
        path=parsed.path,
        query=parsed.query,
    )


def _public_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise DestinationValidationError(
            "webhook hostname resolved to an invalid address"
        ) from exc
    if (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise DestinationValidationError(
            "webhook hostname resolved to a non-public address"
        )
    return address


def _webhook_headers(config: dict) -> dict[str, str]:
    raw_headers = config.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise DestinationValidationError("webhook config.headers must be an object")
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise DestinationValidationError("webhook headers must contain strings")
        name = raw_name.strip()
        if (
            not _HEADER_NAME_RE.fullmatch(name)
            or name.lower() in _CONTROLLED_WEBHOOK_HEADERS
            or any(character in raw_value for character in "\r\n")
        ):
            raise DestinationValidationError("webhook headers contain an unsafe field")
        headers[name] = raw_value
    return headers


def _webhook_timeout(config: dict) -> float:
    value = config.get("timeout", config.get("timeout_seconds", 5.0))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DestinationValidationError("webhook timeout must be numeric")
    timeout = float(value)
    if not 0 < timeout <= _MAX_WEBHOOK_TIMEOUT_SECONDS:
        raise DestinationValidationError(
            f"webhook timeout must be between 0 and {_MAX_WEBHOOK_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def _parse_aws_arn(
    value: object,
    *,
    service: str,
    region: str,
    account_id: str,
) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise DestinationValidationError(f"{service} destination ARN is required")
    parts = value.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[2] != service:
        raise DestinationValidationError(f"{service} destination ARN is invalid")
    partition, arn_region, arn_account, resource = (
        parts[1],
        parts[3],
        parts[4],
        parts[5],
    )
    if any(character in value for character in "*?[]"):
        raise DestinationValidationError(
            f"{service} destination ARN must identify one concrete resource"
        )
    if (
        partition != _partition_for_region(region)
        or arn_region != region
        or arn_account != account_id
        or not resource
    ):
        raise DestinationValidationError(
            f"{service} destination must use the configured AWS account and region"
        )
    return resource


def _parse_log_group_arn(
    value: object,
    *,
    region: str,
    account_id: str,
) -> tuple[str, str]:
    """Return a normalized ARN and name for one concrete log group."""
    normalized = value
    if isinstance(normalized, str) and normalized.endswith(":*"):
        normalized = normalized[:-2]
    resource = _parse_aws_arn(
        normalized,
        service="logs",
        region=region,
        account_id=account_id,
    )
    prefix = "log-group:"
    if not resource.startswith(prefix):
        raise DestinationValidationError(
            "CloudWatch destination ARN must identify one log group"
        )
    return normalized, resource[len(prefix) :]


def _configured_arn_allowlist(
    values: Sequence[str] | None,
    environment_name: str,
) -> tuple[str, ...]:
    if values is None:
        configured = os.environ.get(environment_name)
        values = (configured,) if configured else ()
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{environment_name} allowlist must be a sequence")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(
                f"{environment_name} allowlist contains an invalid ARN"
            )
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _aws_error_code(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return None
    details = response.get("Error")
    if not isinstance(details, dict):
        return None
    code = details.get("Code")
    return code if isinstance(code, str) else None


async def _resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(record[4][0] for record in records)


def _normalize_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    return tenant_id


class DestinationType(Enum):
    WEBHOOK = "webhook"
    CLOUDWATCH = "cloudwatch"
    SNS = "sns"


@dataclass
class EventDestination:
    """A configured destination owned by exactly one tenant."""

    name: str
    destination_type: DestinationType
    config: dict = field(default_factory=dict)
    event_filter: list[str] | None = None
    enabled: bool = True
    tenant_id: str = LEGACY_TENANT_ID

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("destination name must be a non-empty string")
        self.tenant_id = _normalize_tenant_id(self.tenant_id)


@dataclass
class SecurityEvent:
    """A security event ready for tenant-local dispatch."""

    event_id: str
    event_type: str
    timestamp: str
    source: str = "axonllm"
    severity: str = "info"
    user_id: str = ""
    project_id: str = ""
    data: dict = field(default_factory=dict)
    tenant_id: str = LEGACY_TENANT_ID

    def __post_init__(self) -> None:
        self.tenant_id = _normalize_tenant_id(self.tenant_id)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "source": self.source,
            "severity": self.severity,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "data": self.data,
        }


_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "timestamp",
        "source",
        "severity",
        "tenant_id",
        "user_id",
        "project_id",
        "data",
    }
)
_DESTINATION_FIELDS = frozenset(
    {
        "name",
        "destination_type",
        "config",
        "event_filter",
        "enabled",
        "tenant_id",
    }
)
_OUTBOX_FIELDS = frozenset(
    {
        "schema_version",
        "delivery_id",
        "tenant_id",
        "event",
        "destination",
    }
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _required_string(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise DestinationValidationError(
            f"security event outbox {field_name} must be a string"
        )
    return value


def _event_snapshot(event: SecurityEvent) -> dict:
    if not isinstance(event, SecurityEvent):
        raise DestinationValidationError(
            "security event outbox requires a SecurityEvent"
        )
    snapshot = event.to_dict()
    if set(snapshot) != _EVENT_FIELDS:
        raise DestinationValidationError("security event snapshot fields are invalid")
    for field_name in (
        "event_id",
        "event_type",
        "timestamp",
        "source",
        "severity",
        "tenant_id",
    ):
        _required_string(snapshot[field_name], field_name)
    for field_name in ("user_id", "project_id"):
        _required_string(snapshot[field_name], field_name, allow_empty=True)
    if not isinstance(snapshot["data"], dict):
        raise DestinationValidationError("security event data must be an object")
    return snapshot


def _destination_snapshot(destination: EventDestination) -> dict:
    if not isinstance(destination, EventDestination):
        raise DestinationValidationError(
            "security event outbox requires an EventDestination"
        )
    if not isinstance(destination.destination_type, DestinationType):
        raise DestinationValidationError("security event destination type is invalid")
    _required_string(destination.name, "destination name")
    _required_string(destination.tenant_id, "destination tenant_id")
    if not isinstance(destination.config, dict):
        raise DestinationValidationError("destination config must be an object")
    if (
        destination.event_filter is not None
        and (
            not isinstance(destination.event_filter, list)
            or not all(
                isinstance(event_type, str) and bool(event_type.strip())
                for event_type in destination.event_filter
            )
        )
    ):
        raise DestinationValidationError(
            "destination event_filter must contain event type strings"
        )
    if not isinstance(destination.enabled, bool):
        raise DestinationValidationError("destination enabled must be boolean")
    return {
        "name": destination.name,
        "destination_type": destination.destination_type.value,
        "config": destination.config,
        "event_filter": destination.event_filter,
        "enabled": destination.enabled,
        "tenant_id": destination.tenant_id,
    }


def _delivery_identity(
    event: SecurityEvent,
    destination: EventDestination,
) -> str:
    material = {
        "tenant_id": event.tenant_id,
        "event": _event_snapshot(event),
        "destination": _destination_snapshot(destination),
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _destination_group_identity(destination: EventDestination) -> str:
    material = {
        "tenant_id": destination.tenant_id,
        "destination_name": destination.name,
        "destination_type": destination.destination_type.value,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _outbox_message_body(
    event: SecurityEvent,
    destination: EventDestination,
) -> str:
    if event.tenant_id != destination.tenant_id:
        raise DestinationValidationError("cross-tenant event dispatch refused")
    event_value = _event_snapshot(event)
    destination_value = _destination_snapshot(destination)
    envelope = {
        "schema_version": _OUTBOX_SCHEMA_VERSION,
        "delivery_id": _delivery_identity(event, destination),
        "tenant_id": event.tenant_id,
        "event": event_value,
        "destination": destination_value,
    }
    body = _canonical_json(envelope)
    if len(body.encode("utf-8")) > _OUTBOX_MAX_MESSAGE_BYTES:
        raise DestinationValidationError("security event outbox message is too large")
    return body


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode_outbox_message(body: object) -> tuple[SecurityEvent, EventDestination]:
    if not isinstance(body, str) or not body:
        raise DestinationValidationError("security event outbox body is invalid")
    if len(body.encode("utf-8")) > _OUTBOX_MAX_MESSAGE_BYTES:
        raise DestinationValidationError("security event outbox message is too large")
    try:
        envelope = json.loads(
            body,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError) as exc:
        raise DestinationValidationError(
            "security event outbox body is not valid JSON"
        ) from exc
    if not isinstance(envelope, dict) or set(envelope) != _OUTBOX_FIELDS:
        raise DestinationValidationError(
            "security event outbox envelope fields are invalid"
        )
    if (
        isinstance(envelope["schema_version"], bool)
        or envelope["schema_version"] != _OUTBOX_SCHEMA_VERSION
    ):
        raise DestinationValidationError(
            "security event outbox schema version is unsupported"
        )

    tenant_id = _required_string(envelope["tenant_id"], "tenant_id")
    event_value = envelope["event"]
    if not isinstance(event_value, dict) or set(event_value) != _EVENT_FIELDS:
        raise DestinationValidationError(
            "security event outbox event fields are invalid"
        )
    for field_name in (
        "event_id",
        "event_type",
        "timestamp",
        "source",
        "severity",
        "tenant_id",
    ):
        _required_string(event_value[field_name], field_name)
    for field_name in ("user_id", "project_id"):
        _required_string(event_value[field_name], field_name, allow_empty=True)
    if not isinstance(event_value["data"], dict):
        raise DestinationValidationError("security event data must be an object")
    if event_value["tenant_id"] != tenant_id:
        raise DestinationValidationError(
            "security event outbox event tenant does not match envelope"
        )
    event = SecurityEvent(**event_value)

    destination_value = envelope["destination"]
    if (
        not isinstance(destination_value, dict)
        or set(destination_value) != _DESTINATION_FIELDS
    ):
        raise DestinationValidationError(
            "security event outbox destination fields are invalid"
        )
    destination_tenant = _required_string(
        destination_value["tenant_id"],
        "destination tenant_id",
    )
    if destination_tenant != tenant_id:
        raise DestinationValidationError(
            "security event outbox destination tenant does not match envelope"
        )
    _required_string(destination_value["name"], "destination name")
    raw_destination_type = destination_value["destination_type"]
    if not isinstance(raw_destination_type, str):
        raise DestinationValidationError(
            "security event outbox destination type is invalid"
        )
    try:
        destination_type = DestinationType(raw_destination_type)
    except ValueError as exc:
        raise DestinationValidationError(
            "security event outbox destination type is invalid"
        ) from exc
    config = destination_value["config"]
    if not isinstance(config, dict):
        raise DestinationValidationError("destination config must be an object")
    event_filter = destination_value["event_filter"]
    if (
        event_filter is not None
        and (
            not isinstance(event_filter, list)
            or not all(
                isinstance(event_type, str) and bool(event_type.strip())
                for event_type in event_filter
            )
        )
    ):
        raise DestinationValidationError(
            "destination event_filter must contain event type strings"
        )
    if destination_value["enabled"] is not True:
        raise DestinationValidationError(
            "security event outbox destination must be enabled"
        )
    if event_filter and event.event_type not in event_filter:
        raise DestinationValidationError(
            "security event outbox destination does not match the event"
        )
    destination = EventDestination(
        name=destination_value["name"],
        destination_type=destination_type,
        config=config,
        event_filter=event_filter,
        enabled=True,
        tenant_id=destination_tenant,
    )

    delivery_id = envelope["delivery_id"]
    if (
        not isinstance(delivery_id, str)
        or not _SHA256_RE.fullmatch(delivery_id)
        or delivery_id != _delivery_identity(event, destination)
    ):
        raise DestinationValidationError(
            "security event outbox delivery identity is invalid"
        )
    return event, destination


def _event_id_header(event_id: object) -> str:
    if (
        not isinstance(event_id, str)
        or not event_id
        or len(event_id) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in event_id)
    ):
        raise DestinationValidationError(
            "security event ID cannot be used as a webhook header"
        )
    return event_id


class EventDispatcher:
    """Dispatch security events only to destinations in the event's tenant."""

    def __init__(
        self,
        *,
        resolver: HostResolver | None = None,
        aws_region: str | None = None,
        aws_account_id: str | None = None,
        outbox_queue_url: str | None = None,
        sqs_client: object | None = None,
        allowed_sns_topic_arns: Sequence[str] | None = None,
        allowed_log_group_arns: Sequence[str] | None = None,
    ) -> None:
        self._destinations_by_tenant: dict[str, list[EventDestination]] = {}
        self._dispatch_counts: dict[str, int] = {}
        self._error_counts: dict[str, int] = {}
        self._http_client = None
        self._owns_http_client = False
        self._resolver = resolver or _resolve_host
        self._aws_region = (
            aws_region
            if aws_region is not None
            else os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        self._aws_account_id = (
            aws_account_id
            if aws_account_id is not None
            else os.environ.get("AXON_AWS_ACCOUNT_ID")
            or os.environ.get("AWS_ACCOUNT_ID")
        )
        self._destination_refresher: DestinationRefresher | None = None
        self._allowed_sns_topic_arns = _configured_arn_allowlist(
            allowed_sns_topic_arns,
            "AXON_SECURITY_EVENT_SNS_TOPIC_ARN",
        )
        self._allowed_log_group_arns = _configured_arn_allowlist(
            allowed_log_group_arns,
            "AXON_SECURITY_EVENT_LOG_GROUP_ARN",
        )
        configured_queue_url = (
            outbox_queue_url
            if outbox_queue_url is not None
            else os.environ.get("AXON_EVENT_OUTBOX_QUEUE_URL")
        )
        self._outbox_queue_url = (
            _fifo_queue_url(configured_queue_url)
            if configured_queue_url is not None
            else None
        )
        self._sqs_client = sqs_client
        self._owns_sqs_client = False
        self._worker_task: asyncio.Task[None] | None = None

    async def validate_destination(
        self,
        destination: EventDestination,
    ) -> None:
        """Validate a destination before it becomes durable configuration."""
        if not isinstance(destination.config, dict):
            raise DestinationValidationError("destination config must be an object")
        if destination.destination_type == DestinationType.WEBHOOK:
            _webhook_headers(destination.config)
            _webhook_timeout(destination.config)
            await self._resolve_webhook_target(destination.config.get("url"))
        elif destination.destination_type == DestinationType.SNS:
            self._sns_target(destination.config)
        elif destination.destination_type == DestinationType.CLOUDWATCH:
            self._cloudwatch_target(destination.config)
        else:
            raise DestinationValidationError("security event destination type is invalid")

    async def _resolve_webhook_target(
        self,
        url: object,
    ) -> _ValidatedWebhookTarget:
        parsed = _parse_webhook_url(url)
        try:
            direct_address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            try:
                resolved = await asyncio.wait_for(
                    self._resolver(parsed.hostname, parsed.port),
                    timeout=_DNS_RESOLUTION_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise DestinationValidationError(
                    "webhook hostname could not be resolved"
                ) from exc
            if isinstance(resolved, (str, bytes)) or not resolved:
                raise DestinationValidationError(
                    "webhook hostname did not resolve to an address"
                )
            raw_addresses = resolved
        else:
            raw_addresses = (direct_address.compressed,)

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for raw_address in raw_addresses:
            if not isinstance(raw_address, str):
                raise DestinationValidationError(
                    "webhook hostname resolved to an invalid address"
                )
            address = _public_ip_address(raw_address)
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise DestinationValidationError(
                "webhook hostname did not resolve to an address"
            )

        address = addresses[0]
        address_literal = (
            f"[{address.compressed}]"
            if isinstance(address, ipaddress.IPv6Address)
            else address.compressed
        )
        connect_url = urlunsplit(
            (
                "https",
                f"{address_literal}:{parsed.port}",
                parsed.path,
                parsed.query,
                "",
            )
        )
        return _ValidatedWebhookTarget(
            connect_url=connect_url,
            hostname=parsed.hostname,
            host_header=parsed.host_header,
        )

    def _aws_context(self) -> tuple[str, str]:
        region = self._aws_region
        account_id = self._aws_account_id
        if not isinstance(region, str) or not _AWS_REGION_RE.fullmatch(region):
            raise DestinationValidationError(
                "configured AWS region is missing or invalid"
            )
        if (
            not isinstance(account_id, str)
            or not _AWS_ACCOUNT_RE.fullmatch(account_id)
            or set(account_id) == {"0"}
        ):
            raise DestinationValidationError(
                "configured AWS account ID is missing or invalid"
            )
        return region, account_id

    def _sns_target(self, config: dict) -> _SNSTarget:
        region, account_id = self._aws_context()
        requested_region = config.get("region", region)
        if requested_region != region:
            raise DestinationValidationError(
                "SNS destination region must match the configured AWS region"
            )
        topic_arn = config.get("topic_arn")
        resource = _parse_aws_arn(
            topic_arn,
            service="sns",
            region=region,
            account_id=account_id,
        )
        if not _SNS_TOPIC_RE.fullmatch(resource):
            raise DestinationValidationError(
                "SNS destination ARN must identify one concrete topic"
            )
        if (
            self._allowed_sns_topic_arns
            and topic_arn not in self._allowed_sns_topic_arns
        ):
            raise DestinationValidationError(
                "SNS destination is not in the runtime allowlist"
            )
        return _SNSTarget(topic_arn=topic_arn, region=region)

    def _cloudwatch_target(self, config: dict) -> _CloudWatchTarget:
        region, account_id = self._aws_context()
        requested_region = config.get("region", region)
        if requested_region != region:
            raise DestinationValidationError(
                "CloudWatch destination region must match the configured AWS region"
            )

        raw_log_group = config.get("log_group")
        log_group_arn = config.get("log_group_arn")
        if raw_log_group is None and log_group_arn is None:
            raw_log_group = "/axonllm/security"
        if log_group_arn is None and isinstance(raw_log_group, str) and raw_log_group.startswith("arn:"):
            log_group_arn = raw_log_group
            raw_log_group = None
        if log_group_arn is not None:
            _, arn_log_group = _parse_log_group_arn(
                log_group_arn,
                region=region,
                account_id=account_id,
            )
            if raw_log_group is not None and raw_log_group != arn_log_group:
                raise DestinationValidationError(
                    "CloudWatch log_group does not match log_group_arn"
                )
            log_group = arn_log_group
        else:
            log_group = raw_log_group

        log_stream = config.get("log_stream", "events")
        if not isinstance(log_group, str) or not _LOG_GROUP_RE.fullmatch(log_group):
            raise DestinationValidationError(
                "CloudWatch destination must identify one concrete log group"
            )
        if not isinstance(log_stream, str) or not _LOG_STREAM_RE.fullmatch(log_stream):
            raise DestinationValidationError(
                "CloudWatch destination must identify one concrete log stream"
            )
        if self._allowed_log_group_arns:
            allowed_groups = {
                _parse_log_group_arn(
                    allowed_arn,
                    region=region,
                    account_id=account_id,
                )[1]
                for allowed_arn in self._allowed_log_group_arns
            }
            if log_group not in allowed_groups:
                raise DestinationValidationError(
                    "CloudWatch destination is not in the runtime allowlist"
                )
        return _CloudWatchTarget(
            log_group=log_group,
            log_stream=log_stream,
            region=region,
        )

    @property
    def _destinations(self) -> list[EventDestination]:
        """Legacy destination-list compatibility."""
        return self._destinations_by_tenant.setdefault(LEGACY_TENANT_ID, [])

    @_destinations.setter
    def _destinations(self, value: list[EventDestination]) -> None:
        self.replace_destinations(LEGACY_TENANT_ID, value)

    @property
    def _dispatch_count(self) -> int:
        return self._dispatch_counts.get(LEGACY_TENANT_ID, 0)

    @_dispatch_count.setter
    def _dispatch_count(self, value: int) -> None:
        self._dispatch_counts[LEGACY_TENANT_ID] = value

    @property
    def _error_count(self) -> int:
        return self._error_counts.get(LEGACY_TENANT_ID, 0)

    @_error_count.setter
    def _error_count(self, value: int) -> None:
        self._error_counts[LEGACY_TENANT_ID] = value

    def add_destination(
        self,
        destination: EventDestination,
        *,
        tenant_id: str | None = None,
    ) -> None:
        tenant_id = _normalize_tenant_id(tenant_id or destination.tenant_id)
        if destination.tenant_id != tenant_id:
            raise ValueError("destination tenant_id does not match target tenant")
        self._destinations_by_tenant.setdefault(tenant_id, []).append(destination)

    def remove_destination(
        self,
        name: str,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> bool:
        tenant_id = _normalize_tenant_id(tenant_id)
        current = self._destinations_by_tenant.get(tenant_id, [])
        remaining = [destination for destination in current if destination.name != name]
        if len(remaining) == len(current):
            return False
        self._destinations_by_tenant[tenant_id] = remaining
        return True

    def replace_destinations(
        self,
        tenant_id: str,
        destinations: list[EventDestination],
    ) -> None:
        """Atomically replace one tenant's in-memory destination snapshot."""
        tenant_id = _normalize_tenant_id(tenant_id)
        if any(destination.tenant_id != tenant_id for destination in destinations):
            raise ValueError("destination set contains a different tenant")
        self._destinations_by_tenant[tenant_id] = list(destinations)

    def destinations_for_tenant(
        self,
        tenant_id: str,
    ) -> list[EventDestination]:
        tenant_id = _normalize_tenant_id(tenant_id)
        return list(self._destinations_by_tenant.get(tenant_id, []))

    def get_destination(
        self,
        tenant_id: str,
        name: str,
    ) -> EventDestination | None:
        return next(
            (destination for destination in self.destinations_for_tenant(tenant_id) if destination.name == name),
            None,
        )

    @property
    def destinations(self) -> list[EventDestination]:
        """Legacy destination-list compatibility."""
        return self.destinations_for_tenant(LEGACY_TENANT_ID)

    def set_destination_refresher(
        self,
        refresher: DestinationRefresher | None,
    ) -> None:
        """Install the fleet-sync hook called before tenant event delivery."""
        self._destination_refresher = refresher

    @property
    def outbox_enabled(self) -> bool:
        return self._outbox_queue_url is not None

    @property
    def worker_running(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    def _get_sqs_client(self):
        if self._sqs_client is None:
            import boto3

            self._sqs_client = boto3.client("sqs", region_name=self._aws_region)
            self._owns_sqs_client = True
        return self._sqs_client

    async def _sqs_call(self, operation: str, **kwargs):
        client = self._get_sqs_client()
        method = getattr(client, operation, None)
        if not callable(method):
            raise RuntimeError(f"SQS client does not support {operation}")
        return await asyncio.to_thread(method, **kwargs)

    async def check_readiness(self) -> bool:
        """Verify outbox queue access without receiving any messages."""
        if self._outbox_queue_url is None:
            return True
        try:
            response = await self._sqs_call(
                "get_queue_attributes",
                QueueUrl=self._outbox_queue_url,
                AttributeNames=["QueueArn", "FifoQueue"],
            )
            if not isinstance(response, dict):
                return False
            attributes = response.get("Attributes")
            return (
                isinstance(attributes, dict)
                and attributes.get("FifoQueue") == "true"
                and isinstance(attributes.get("QueueArn"), str)
                and attributes["QueueArn"].endswith(".fifo")
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Security event outbox readiness check failed")
            return False

    async def start(self) -> None:
        """Start one bounded, cancellation-safe outbox worker."""
        if self._outbox_queue_url is None:
            raise RuntimeError("security event outbox is not configured")
        if self.worker_running:
            return
        task = asyncio.create_task(
            self._outbox_worker(),
            name="axon-security-event-outbox",
        )
        task.add_done_callback(self._consume_worker_result)
        self._worker_task = task

    @staticmethod
    def _consume_worker_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("Security event outbox worker terminated unexpectedly")

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        """Stop the worker within a fixed timeout and close owned HTTP state."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")

        task = self._worker_task
        self._worker_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=float(timeout_seconds))
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                logger.error("Security event outbox worker did not stop in time")
            except Exception:
                logger.error("Security event outbox worker failed during shutdown")

        client = self._http_client
        if self._owns_http_client and client is not None:
            self._http_client = None
            self._owns_http_client = False
            close = getattr(client, "aclose", None)
            if callable(close):
                try:
                    await asyncio.wait_for(
                        close(),
                        timeout=float(timeout_seconds),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error(
                        "Security event HTTP client failed during shutdown"
                    )

        sqs_client = self._sqs_client
        if self._owns_sqs_client and sqs_client is not None:
            self._sqs_client = None
            self._owns_sqs_client = False
            close = getattr(sqs_client, "close", None)
            if callable(close):
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(close),
                        timeout=float(timeout_seconds),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error(
                        "Security event SQS client failed during shutdown"
                    )

    async def _outbox_worker(self) -> None:
        receive_failures = 0
        while True:
            try:
                response = await self._sqs_call(
                    "receive_message",
                    QueueUrl=self._outbox_queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=_OUTBOX_WAIT_TIME_SECONDS,
                    AttributeNames=["ApproximateReceiveCount"],
                )
                if not isinstance(response, dict):
                    raise RuntimeError("SQS receive response is invalid")
                messages = response.get("Messages", [])
                if not isinstance(messages, list):
                    raise RuntimeError("SQS receive messages are invalid")
            except asyncio.CancelledError:
                raise
            except Exception:
                receive_failures += 1
                logger.error("Security event outbox receive failed")
                await asyncio.sleep(min(2 ** (receive_failures - 1), 30))
                continue

            receive_failures = 0
            for message in messages:
                await self._process_outbox_message(message)
            if not messages:
                await asyncio.sleep(0)

    @staticmethod
    def _receive_count(message: object) -> int:
        if not isinstance(message, dict):
            return 1
        attributes = message.get("Attributes")
        if not isinstance(attributes, dict):
            return 1
        try:
            count = int(attributes.get("ApproximateReceiveCount", "1"))
        except (TypeError, ValueError):
            return 1
        return max(1, count)

    async def _defer_outbox_message(
        self,
        message: object,
        receive_count: int,
    ) -> None:
        if not isinstance(message, dict):
            return
        receipt_handle = message.get("ReceiptHandle")
        if not isinstance(receipt_handle, str) or not receipt_handle:
            logger.error(
                "Security event outbox failure cannot update message visibility"
            )
            return
        exponent = min(max(receive_count - 1, 0), 16)
        visibility_timeout = min(
            _OUTBOX_RETRY_BASE_SECONDS * (2**exponent),
            _OUTBOX_RETRY_MAX_SECONDS,
        )
        try:
            await self._sqs_call(
                "change_message_visibility",
                QueueUrl=self._outbox_queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=visibility_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Security event outbox visibility update failed")

    async def _process_outbox_message(self, message: object) -> bool:
        receive_count = self._receive_count(message)
        try:
            if not isinstance(message, dict):
                raise DestinationValidationError("SQS message is invalid")
            receipt_handle = message.get("ReceiptHandle")
            if not isinstance(receipt_handle, str) or not receipt_handle:
                raise DestinationValidationError(
                    "SQS message receipt handle is invalid"
                )
            tenant_id = await self.deliver_outbox_body(message.get("Body"))
            await self._sqs_call(
                "delete_message",
                QueueUrl=self._outbox_queue_url,
                ReceiptHandle=receipt_handle,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Security event outbox delivery failed tenant=%s attempt=%d",
                locals().get("tenant_id", "unknown"),
                receive_count,
            )
            await self._defer_outbox_message(message, receive_count)
            return False

        return True

    async def deliver_outbox_body(self, body: object) -> str:
        """Deliver one validated outbox envelope without managing SQS state."""

        tenant_id: str | None = None
        try:
            event, destination = _decode_outbox_message(body)
            tenant_id = event.tenant_id
            await self._send_to_destination(event, destination)
        except asyncio.CancelledError:
            raise
        except Exception:
            if tenant_id is not None:
                self._error_counts[tenant_id] = (
                    self._error_counts.get(tenant_id, 0) + 1
                )
            raise

        self._dispatch_counts[tenant_id] = (
            self._dispatch_counts.get(tenant_id, 0) + 1
        )
        return tenant_id

    async def _enqueue_outbox_message(
        self,
        event: SecurityEvent,
        destination: EventDestination,
    ) -> None:
        await self._sqs_call(
            "send_message",
            QueueUrl=self._outbox_queue_url,
            MessageBody=_outbox_message_body(event, destination),
            MessageGroupId=_destination_group_identity(destination),
            MessageDeduplicationId=_delivery_identity(event, destination),
        )

    async def dispatch(self, event: SecurityEvent) -> None:
        """Dispatch an event to matching destinations in its tenant only."""
        if self._destination_refresher is not None:
            try:
                await self._destination_refresher(event.tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._error_counts[event.tenant_id] = (
                    self._error_counts.get(event.tenant_id, 0) + 1
                )
                logger.error(
                    "Security destination refresh failed tenant=%s event=%s",
                    event.tenant_id,
                    event.event_id,
                )
                return

        destinations = [
            destination
            for destination in self.destinations_for_tenant(event.tenant_id)
            if destination.enabled
            and (
                not destination.event_filter
                or event.event_type in destination.event_filter
            )
        ]
        if self._outbox_queue_url is not None:
            enqueue_errors: list[Exception] = []
            for destination in destinations:
                try:
                    await self._enqueue_outbox_message(event, destination)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    enqueue_errors.append(exc)
                    self._error_counts[event.tenant_id] = (
                        self._error_counts.get(event.tenant_id, 0) + 1
                    )
                    logger.error(
                        "Security event outbox enqueue failed "
                        "tenant=%s event=%s destination=%s",
                        event.tenant_id,
                        event.event_id,
                        destination.name,
                    )
            if enqueue_errors:
                raise RuntimeError(
                    "security event could not be durably enqueued"
                ) from enqueue_errors[0]
            return

        tasks = [
            self._send_to_destination(event, destination)
            for destination in destinations
        ]
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self._error_counts[event.tenant_id] = self._error_counts.get(event.tenant_id, 0) + 1
                logger.error(
                    "Security event dispatch failed tenant=%s event=%s",
                    event.tenant_id,
                    event.event_id,
                )
            else:
                self._dispatch_counts[event.tenant_id] = self._dispatch_counts.get(event.tenant_id, 0) + 1

    async def dispatch_injection_event(
        self,
        event_id: str,
        user_id: str,
        project_id: str,
        threat_level: str,
        patterns: list[str],
        blocked: bool,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> None:
        event = SecurityEvent(
            event_id=event_id,
            event_type=("injection_blocked" if blocked else "injection_detected"),
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="critical" if blocked else "warning",
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            data={
                "threat_level": threat_level,
                "patterns": patterns,
                "blocked": blocked,
            },
        )
        await self.dispatch(event)

    async def dispatch_pii_event(
        self,
        event_id: str,
        user_id: str,
        project_id: str,
        redacted_types: list[str],
        count: int,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> None:
        event = SecurityEvent(
            event_id=event_id,
            event_type="pii_redaction",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="info",
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            data={"redacted_types": redacted_types, "count": count},
        )
        await self.dispatch(event)

    async def dispatch_auth_failure(
        self,
        event_id: str,
        source_ip: str,
        reason: str,
        *,
        tenant_id: str = LEGACY_TENANT_ID,
    ) -> None:
        event = SecurityEvent(
            event_id=event_id,
            event_type="auth_failure",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="warning",
            tenant_id=tenant_id,
            data={"source_ip": source_ip, "reason": reason},
        )
        await self.dispatch(event)

    async def _send_to_destination(
        self,
        event: SecurityEvent,
        destination: EventDestination,
    ) -> None:
        if destination.tenant_id != event.tenant_id:
            raise ValueError("cross-tenant event dispatch refused")
        if destination.destination_type == DestinationType.WEBHOOK:
            await self._send_webhook(event, destination)
        elif destination.destination_type == DestinationType.SNS:
            await self._send_sns(event, destination)
        elif destination.destination_type == DestinationType.CLOUDWATCH:
            await self._send_cloudwatch(event, destination)
        else:
            raise DestinationValidationError("security event destination type is invalid")

    def _get_http_client(self):
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
                http2=False,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            )
            self._owns_http_client = True
        return self._http_client

    async def _send_webhook(
        self,
        event: SecurityEvent,
        destination: EventDestination,
    ) -> None:
        try:
            target = await self._resolve_webhook_target(
                destination.config.get("url")
            )
            configured_headers = _webhook_headers(destination.config)
            timeout = _webhook_timeout(destination.config)
            client = self._get_http_client()
            response = await client.post(
                target.connect_url,
                json=event.to_dict(),
                headers={
                    **configured_headers,
                    "Content-Type": "application/json",
                    "Host": target.host_header,
                    "Connection": "close",
                    "Idempotency-Key": _delivery_identity(event, destination),
                    "X-Axon-Event-ID": _event_id_header(event.event_id),
                },
                timeout=timeout,
                follow_redirects=False,
                extensions={"sni_hostname": target.hostname},
            )
            if response.status_code >= 300:
                raise RuntimeError("webhook returned an error response")
        except ImportError as exc:
            raise RuntimeError("webhook dispatch dependency unavailable") from exc
        except Exception as exc:
            raise RuntimeError(f"Webhook {destination.name!r} delivery failed") from exc

    async def _send_sns(
        self,
        event: SecurityEvent,
        destination: EventDestination,
    ) -> None:
        target = self._sns_target(destination.config)

        def _publish() -> None:
            import boto3

            client = boto3.client("sns", region_name=target.region)
            publish_kwargs = {
                "TopicArn": target.topic_arn,
                "Message": json.dumps(event.to_dict()),
                "Subject": f"AxonLLM Security: {event.event_type}",
                "MessageAttributes": {
                    "event_type": {
                        "DataType": "String",
                        "StringValue": event.event_type,
                    },
                    "severity": {
                        "DataType": "String",
                        "StringValue": event.severity,
                    },
                },
            }
            if target.topic_arn.endswith(".fifo"):
                publish_kwargs.update(
                    {
                        "MessageGroupId": _destination_group_identity(destination),
                        "MessageDeduplicationId": _delivery_identity(
                            event,
                            destination,
                        ),
                    }
                )
            client.publish(**publish_kwargs)

        await asyncio.to_thread(_publish)

    async def _send_cloudwatch(
        self,
        event: SecurityEvent,
        destination: EventDestination,
    ) -> None:
        target = self._cloudwatch_target(destination.config)

        def _put_log() -> None:
            import boto3

            client = boto3.client("logs", region_name=target.region)
            request = {
                "logGroupName": target.log_group,
                "logStreamName": target.log_stream,
                "logEvents": [
                    {
                        "timestamp": int(time.time() * 1000),
                        "message": json.dumps(event.to_dict()),
                    }
                ],
            }
            try:
                client.put_log_events(**request)
            except Exception as error:
                if _aws_error_code(error) != "ResourceNotFoundException":
                    raise
                try:
                    client.create_log_stream(
                        logGroupName=target.log_group,
                        logStreamName=target.log_stream,
                    )
                except Exception as create_error:
                    if (
                        _aws_error_code(create_error)
                        != "ResourceAlreadyExistsException"
                    ):
                        raise
                client.put_log_events(**request)

        await asyncio.to_thread(_put_log)

    def stats_for_tenant(self, tenant_id: str) -> dict:
        tenant_id = _normalize_tenant_id(tenant_id)
        return {
            "tenant_id": tenant_id,
            "destinations": len(self._destinations_by_tenant.get(tenant_id, [])),
            "dispatched": self._dispatch_counts.get(tenant_id, 0),
            "errors": self._error_counts.get(tenant_id, 0),
        }

    @property
    def stats(self) -> dict:
        """Legacy stats compatibility."""
        stats = self.stats_for_tenant(LEGACY_TENANT_ID)
        stats.pop("tenant_id")
        return stats
