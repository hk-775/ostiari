"""Policy fetcher — strategy pattern for remote policy sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ostiari.exceptions import AdapterNotInstalledError


@dataclass
class PolicySource:
    """Configuration for a remote policy source."""

    url: str
    poll_interval: int = 60
    auth: dict[str, str] | None = None


class PolicyFetcher(Protocol):
    """Protocol for policy content fetchers."""

    def fetch(self, source: PolicySource) -> bytes: ...


class FileFetcher:
    """Fetches policy from local filesystem."""

    def fetch(self, source: PolicySource) -> bytes:
        path = source.url.removeprefix("file://")
        return Path(path).read_bytes()


class HttpsFetcher:
    """Fetches policy via HTTPS GET."""

    def fetch(self, source: PolicySource) -> bytes:
        try:
            import httpx
        except ImportError:
            raise AdapterNotInstalledError(
                adapter="HttpsPolicySource",
                install_command="pip install ostiari[policy]",
            ) from None
        headers = source.auth or {}
        resp = httpx.get(source.url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        return resp.content


class S3Fetcher:
    """Fetches policy from AWS S3."""

    def __init__(self) -> None:
        self._client: Any = None

    def fetch(self, source: PolicySource) -> bytes:
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise AdapterNotInstalledError(
                    adapter="S3PolicySource",
                    install_command="pip install ostiari[policy]",
                ) from None
            self._client = boto3.client("s3")
        bucket, key = _parse_s3_url(source.url)
        obj = self._client.get_object(Bucket=bucket, Key=key)
        content: bytes = obj["Body"].read()
        return content


def _parse_s3_url(url: str) -> tuple[str, str]:
    """Parse s3://bucket/key into (bucket, key)."""
    path = url.removeprefix("s3://")
    parts = path.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid S3 URL: {url}")
    return parts[0], parts[1]


_FETCHERS: dict[str, type[FileFetcher | HttpsFetcher | S3Fetcher]] = {
    "file": FileFetcher,
    "http": HttpsFetcher,
    "https": HttpsFetcher,
    "s3": S3Fetcher,
}


def get_fetcher(url: str) -> PolicyFetcher:
    """Return the appropriate fetcher for a URL scheme."""
    scheme = url.split("://", 1)[0].lower() if "://" in url else "file"
    fetcher_cls = _FETCHERS.get(scheme)
    if fetcher_cls is None:
        raise ValueError(
            f"Unsupported policy source scheme: {scheme!r}. Supported: {list(_FETCHERS.keys())}"
        )
    return fetcher_cls()
