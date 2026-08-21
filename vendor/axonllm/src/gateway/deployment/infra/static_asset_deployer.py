"""CloudFormation custom resource for exact-version static-site deployment."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import stat
import tempfile
import zipfile
from pathlib import PurePosixPath

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_COMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_ENTRIES = 10_000
_CHUNK_BYTES = 1024 * 1024


def _required(properties: dict, name: str) -> str:
    value = properties.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _archive_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    if not entries or len(entries) > _MAX_ENTRIES:
        raise ValueError("static artifact entry count is invalid")
    if sum(entry.file_size for entry in entries) > _MAX_UNCOMPRESSED_BYTES:
        raise ValueError("static artifact exceeds its uncompressed size limit")
    names = [entry.filename for entry in entries]
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("static artifact paths must be unique and sorted")
    lowered: set[str] = set()
    for entry in entries:
        path = PurePosixPath(entry.filename)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or entry.is_dir()
            or entry.flag_bits & 0x1
        ):
            raise ValueError(f"unsafe static artifact entry: {entry.filename}")
        lowered_name = path.as_posix().lower()
        if lowered_name in lowered:
            raise ValueError("static artifact contains case-colliding paths")
        lowered.add(lowered_name)
        mode = entry.external_attr >> 16
        if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
            raise ValueError(f"non-regular static artifact entry: {entry.filename}")
    return entries


def _download_version(
    s3_client,
    *,
    bucket: str,
    key: str,
    version: str,
    expected_sha256: str,
    destination,
) -> None:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("SourceSha256 must be a lowercase SHA-256 digest")
    response = s3_client.get_object(
        Bucket=bucket,
        Key=key,
        VersionId=version,
    )
    body = response["Body"]
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = body.read(_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > _MAX_COMPRESSED_BYTES:
            raise ValueError("static artifact exceeds its compressed size limit")
        digest.update(chunk)
        destination.write(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("static artifact digest does not match SourceSha256")
    destination.flush()
    destination.seek(0)


def _list_keys(s3_client, bucket: str) -> set[str]:
    keys: set[str] = set()
    token = None
    while True:
        request = {"Bucket": bucket}
        if token is not None:
            request["ContinuationToken"] = token
        response = s3_client.list_objects_v2(**request)
        keys.update(
            item["Key"]
            for item in response.get("Contents", [])
            if isinstance(item, dict) and isinstance(item.get("Key"), str)
        )
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if not isinstance(token, str) or not token:
            raise ValueError("S3 listing truncated without a continuation token")


def _delete_keys(s3_client, bucket: str, keys: set[str]) -> None:
    ordered = sorted(keys)
    for index in range(0, len(ordered), 1_000):
        batch = ordered[index : index + 1_000]
        response = s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
        )
        errors = response.get("Errors", [])
        if errors:
            raise RuntimeError(f"unable to delete {len(errors)} static object(s)")


def _content_headers(name: str) -> dict[str, str]:
    content_type, encoding = mimetypes.guess_type(name)
    headers = {
        "CacheControl": (
            "no-cache"
            if name.endswith(".html")
            else "public,max-age=3600"
        ),
        "ContentType": content_type or "application/octet-stream",
    }
    if encoding:
        headers["ContentEncoding"] = encoding
    return headers


def _invalidate(cloudfront_client, distribution_id: str, request_id: str) -> None:
    cloudfront_client.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "CallerReference": request_id,
            "Paths": {"Items": ["/*"], "Quantity": 1},
        },
    )


def _distribution_ids(properties: dict) -> tuple[str, ...]:
    primary = _required(properties, "DistributionId")
    additional = properties.get("AdditionalDistributionId")
    if additional in (None, ""):
        return (primary,)
    if not isinstance(additional, str):
        raise ValueError("AdditionalDistributionId must be a string")
    return tuple(dict.fromkeys((primary, additional)))


def deploy(
    properties: dict,
    *,
    request_id: str,
    s3_client,
    cloudfront_client,
) -> dict[str, object]:
    """Deploy an exact, verified source version into the private site bucket."""

    source_bucket = _required(properties, "SourceBucket")
    source_key = _required(properties, "SourceKey")
    source_version = _required(properties, "SourceVersion")
    source_sha256 = _required(properties, "SourceSha256")
    destination_bucket = _required(properties, "DestinationBucket")
    distribution_ids = _distribution_ids(properties)

    with tempfile.SpooledTemporaryFile(
        max_size=16 * 1024 * 1024,
        mode="w+b",
    ) as artifact:
        _download_version(
            s3_client,
            bucket=source_bucket,
            key=source_key,
            version=source_version,
            expected_sha256=source_sha256,
            destination=artifact,
        )
        with zipfile.ZipFile(artifact) as archive:
            entries = _archive_entries(archive)
            desired = {entry.filename for entry in entries}
            for entry in entries:
                payload = archive.read(entry)
                s3_client.put_object(
                    Bucket=destination_bucket,
                    Key=entry.filename,
                    Body=payload,
                    **_content_headers(entry.filename),
                )

    stale = _list_keys(s3_client, destination_bucket) - desired
    _delete_keys(s3_client, destination_bucket, stale)
    for distribution_id in distribution_ids:
        _invalidate(cloudfront_client, distribution_id, request_id)
    return {
        "ObjectCount": len(desired),
        "SourceSha256": source_sha256,
        "SourceVersion": source_version,
    }


def remove(
    properties: dict,
    *,
    request_id: str,
    s3_client,
    cloudfront_client,
) -> dict[str, object]:
    """Remove namespaced static objects; production retains them."""

    destination_bucket = _required(properties, "DestinationBucket")
    distribution_ids = _distribution_ids(properties)
    if properties.get("RetainOnDelete") == "true":
        return {"Retained": True}
    keys = _list_keys(s3_client, destination_bucket)
    _delete_keys(s3_client, destination_bucket, keys)
    for distribution_id in distribution_ids:
        _invalidate(cloudfront_client, distribution_id, request_id)
    return {"DeletedObjectCount": len(keys), "Retained": False}


def handler(event, context) -> None:
    """CloudFormation callback entry point."""

    import boto3
    import cfnresponse

    physical_id = event.get("PhysicalResourceId") or (
        "axonllm-static-assets:"
        + str(event.get("ResourceProperties", {}).get("DestinationBucket", ""))
    )
    try:
        properties = event["ResourceProperties"]
        request_id = _required(event, "RequestId")
        if event.get("RequestType") == "Delete":
            data = remove(
                properties,
                request_id=request_id,
                s3_client=boto3.client("s3"),
                cloudfront_client=boto3.client("cloudfront"),
            )
        else:
            data = deploy(
                properties,
                request_id=request_id,
                s3_client=boto3.client("s3"),
                cloudfront_client=boto3.client("cloudfront"),
            )
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            data,
            physicalResourceId=physical_id,
        )
    except Exception as exc:
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {"Error": str(exc)[:1_024]},
            physicalResourceId=physical_id,
        )
