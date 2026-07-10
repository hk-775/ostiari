"""Unit tests for the policy fetcher module."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from ostiari.exceptions import AdapterNotInstalledError
from ostiari.policy.fetcher import (
    FileFetcher,
    HttpsFetcher,
    PolicySource,
    S3Fetcher,
    _parse_s3_url,
    get_fetcher,
)


class TestFileFetcher:
    def test_reads_file(self, tmp_path):
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("rules: []")

        fetcher = FileFetcher()
        source = PolicySource(url=f"file://{policy_file}")
        content = fetcher.fetch(source)
        assert content == b"rules: []"

    def test_missing_file_raises(self, tmp_path):
        fetcher = FileFetcher()
        source = PolicySource(url=f"file://{tmp_path}/nonexistent.yaml")
        with pytest.raises(FileNotFoundError):
            fetcher.fetch(source)


class TestHttpsFetcher:
    def test_guarded_import(self):
        with patch.dict(sys.modules, {"httpx": None}):
            fetcher = HttpsFetcher()
            source = PolicySource(url="https://example.com/policy.yaml")
            with pytest.raises(AdapterNotInstalledError) as exc_info:
                fetcher.fetch(source)
            assert "ostiari[policy]" in str(exc_info.value)


class TestS3Fetcher:
    def test_guarded_import(self):
        with patch.dict(sys.modules, {"boto3": None}):
            fetcher = S3Fetcher()
            source = PolicySource(url="s3://bucket/key.yaml")
            with pytest.raises(AdapterNotInstalledError) as exc_info:
                fetcher.fetch(source)
            assert "ostiari[policy]" in str(exc_info.value)

    def test_client_cached(self):
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_object.return_value = {"Body": MagicMock(read=lambda: b"rules: []")}

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            fetcher = S3Fetcher()
            source = PolicySource(url="s3://mybucket/policies/default.yaml")
            fetcher.fetch(source)
            fetcher.fetch(source)
            mock_boto3.client.assert_called_once_with("s3")


class TestGetFetcher:
    def test_file_scheme(self):
        f = get_fetcher("file:///path/to/policy.yaml")
        assert isinstance(f, FileFetcher)

    def test_https_scheme(self):
        f = get_fetcher("https://example.com/policy.yaml")
        assert isinstance(f, HttpsFetcher)

    def test_s3_scheme(self):
        f = get_fetcher("s3://bucket/key.yaml")
        assert isinstance(f, S3Fetcher)

    def test_unsupported_scheme(self):
        with pytest.raises(ValueError, match="Unsupported"):
            get_fetcher("ftp://server/policy.yaml")


class TestParseS3Url:
    def test_valid(self):
        bucket, key = _parse_s3_url("s3://my-bucket/path/to/policy.yaml")
        assert bucket == "my-bucket"
        assert key == "path/to/policy.yaml"

    def test_invalid(self):
        with pytest.raises(ValueError):
            _parse_s3_url("s3://nobucket")
