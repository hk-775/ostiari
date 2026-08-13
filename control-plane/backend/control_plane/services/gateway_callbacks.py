"""Validation for control-plane callbacks into registered gateways."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from control_plane.env import is_production


class GatewayCallbackError(ValueError):
    """Raised when a gateway endpoint is unsafe for control-plane callbacks."""


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def callback_allowlist() -> list[str]:
    raw = os.environ.get("OSTIARI_GATEWAY_CALLBACK_ALLOW", "")
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def _resolve(host: str) -> list[IPAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise GatewayCallbackError(
            f"gateway callback host '{host}' could not be resolved"
        ) from exc
    addresses: list[IPAddress] = []
    for info in infos:
        value = info[4][0]
        if not isinstance(value, str):
            continue
        try:
            addresses.append(ipaddress.ip_address(value.split("%", 1)[0]))
        except ValueError:
            continue
    if not addresses:
        raise GatewayCallbackError(
            f"gateway callback host '{host}' resolved to no usable address"
        )
    return addresses


def _is_link_local_or_metadata(address: IPAddress) -> bool:
    return address.is_link_local or str(address) == "fd00:ec2::254"


def _allowed(host: str, address: IPAddress, entries: list[str]) -> bool:
    normalized_host = host.rstrip(".").lower()
    for entry in entries:
        normalized_entry = entry.rstrip(".").lower()
        if normalized_entry == normalized_host:
            return True
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def validate_gateway_callback(url: str) -> str:
    """Return a normalized callback URL or raise on an unsafe destination."""
    if not isinstance(url, str) or not url.strip():
        raise GatewayCallbackError("gateway callback must be a non-empty URL")
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise GatewayCallbackError("gateway callback must use http or https")
    if not parsed.hostname:
        raise GatewayCallbackError("gateway callback must include a host")
    if parsed.username or parsed.password:
        raise GatewayCallbackError("gateway callback may not contain userinfo")
    if parsed.query or parsed.fragment:
        raise GatewayCallbackError(
            "gateway callback may not contain a query string or fragment"
        )

    try:
        addresses = _resolve(parsed.hostname)
    except GatewayCallbackError:
        if is_production():
            raise
        addresses = []
    for address in addresses:
        if _is_link_local_or_metadata(address):
            raise GatewayCallbackError(
                "gateway callback may not target link-local or metadata addresses"
            )

    if is_production():
        entries = callback_allowlist()
        if not entries:
            raise GatewayCallbackError(
                "OSTIARI_GATEWAY_CALLBACK_ALLOW is required in production"
            )
        disallowed = [
            str(address)
            for address in addresses
            if not _allowed(parsed.hostname, address, entries)
        ]
        if disallowed:
            raise GatewayCallbackError(
                "gateway callback destination is not in "
                "OSTIARI_GATEWAY_CALLBACK_ALLOW"
            )

    return value.rstrip("/")
