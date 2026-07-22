"""SSRF guard for request-supplied outbound URLs.

Ostiari fetches some URLs that a *request* (not the operator) can influence —
OpenAPI import specs and A2A agent-card `url`s. Without validation an attacker
could point those at internal services or the cloud-metadata endpoint
(169.254.169.254) to steal instance credentials (the Capital One class of bug).

This guard is applied ONLY to those request-supplied fetches — never to
operator-configured tool/model endpoints, which are trusted by definition and
routinely point at localhost/internal hosts.

Posture (dev-aware, matching the rest of Ostiari):
  - Cloud metadata / link-local (169.254.0.0/16, fe80::/10) is ALWAYS blocked —
    Ostiari never legitimately fetches instance metadata, in any mode.
  - Scheme is restricted to http/https always.
  - Private / loopback ranges: allowed in dev (so `import-openapi http://localhost`
    and local MCP/A2A work), blocked in production (OSTIARI_ENV=production) unless
    explicitly allowlisted via OSTIARI_SSRF_ALLOW (comma-separated CIDRs/hosts).

Call validate_public_url(url) before fetching; it raises SSRFError on a
disallowed target. DNS names are resolved so a public name that points at an
internal IP is still caught.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class SSRFError(ValueError):
    """Raised when a request-supplied URL targets a disallowed destination."""


_ALLOWED_SCHEMES = ("http", "https")


def _is_production() -> bool:
    return os.environ.get("OSTIARI_ENV", "").strip().lower() in ("production", "prod")


def _allowlist() -> list[str]:
    raw = os.environ.get("OSTIARI_SSRF_ALLOW", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _matches_allowlist(host: str, ip: ipaddress._BaseAddress) -> bool:
    """True if the host or resolved IP is explicitly allowlisted (prod escape hatch)."""
    for entry in _allowlist():
        if entry == host:
            return True
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            # not a CIDR — treat as a hostname literal
            if entry == host:
                return True
    return False


def _resolve_ips(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve a hostname to all its IPs (so DNS can't hide an internal target)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise SSRFError(f"could not resolve host '{host}': {e}") from e
    ips: list[ipaddress._BaseAddress] = []
    for info in infos:
        addr = info[4][0]
        try:
            ips.append(ipaddress.ip_address(addr.split("%")[0]))  # strip scope id
        except ValueError:
            continue
    return ips


def _always_blocked(ip: ipaddress._BaseAddress) -> bool:
    """Ranges that are blocked in EVERY mode (metadata / link-local)."""
    # 169.254.0.0/16 (IPv4 link-local, incl. cloud metadata 169.254.169.254),
    # fe80::/10 (IPv6 link-local), and the metadata mapping fd00:ec2::254.
    return ip.is_link_local or str(ip) in ("fd00:ec2::254",)


def _private_blocked(ip: ipaddress._BaseAddress) -> bool:
    """Ranges blocked in production (private/loopback/reserved)."""
    return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_multicast


def validate_public_url(url: str) -> str:
    """Validate a request-supplied URL before fetching it. Returns the URL or raises.

    Always: http/https only, and never a link-local/metadata address.
    Production: also blocks private/loopback/reserved unless allowlisted.
    Dev: private/loopback allowed (so localhost tooling works).
    """
    if not url or not isinstance(url, str):
        raise SSRFError("empty URL")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SSRFError(f"scheme '{parsed.scheme}' not allowed (only http/https)")
    host = parsed.hostname
    if not host:
        raise SSRFError("URL has no host")

    ips = _resolve_ips(host)
    if not ips:
        raise SSRFError(f"host '{host}' resolved to no usable address")

    prod = _is_production()
    for ip in ips:
        # Metadata / link-local is blocked everywhere, no allowlist escape.
        if _always_blocked(ip):
            raise SSRFError(
                f"target {ip} is a link-local/metadata address — blocked (SSRF protection)")
        if prod and _private_blocked(ip):
            if _matches_allowlist(host, ip):
                continue
            raise SSRFError(
                f"target {ip} is private/internal — blocked in production "
                f"(add to OSTIARI_SSRF_ALLOW to permit)")
    return url
