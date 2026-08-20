"""Deterministic artifacts for the serverless AxonLLM control plane."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_STATIC_SITE_TYPES = frozenset(
    {
        ".css",
        ".drawio",
        ".html",
        ".js",
        ".json",
        ".mp3",
        ".mp4",
        ".png",
        ".svg",
        ".vtt",
    }
)
_STATIC_SITE_DIRS = frozenset({"narration"})
_SOURCE_EXCLUDED_PARTS = frozenset(
    {
        "__pycache__",
        "deployment",
        "static",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_MAX_STATIC_BYTES = 100 * 1024 * 1024
_MAX_LAMBDA_BYTES = 240 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactMetadata:
    """Content-addressed artifact metadata safe to publish in a receipt."""

    entry_count: int
    file_name: str
    sha256: str
    size_bytes: int
    tree_sha256: str


@dataclass(frozen=True)
class ArtifactReceipt:
    """Non-secret, deterministic build receipt."""

    control_api: ArtifactMetadata
    schema: str
    source_revision: str
    static_assets: ArtifactMetadata

    def to_json(self) -> bytes:
        def metadata(value: ArtifactMetadata) -> dict[str, object]:
            return {
                "entryCount": value.entry_count,
                "fileName": value.file_name,
                "sha256": value.sha256,
                "sizeBytes": value.size_bytes,
                "treeSha256": value.tree_sha256,
            }

        return (
            json.dumps(
                {
                    "artifacts": {
                        "controlApi": metadata(self.control_api),
                        "staticAssets": metadata(self.static_assets),
                    },
                    "schema": self.schema,
                    "sourceRevision": self.source_revision,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


def _regular_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        raise ValueError(f"directory does not exist: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {path}")
        if path.is_file():
            yield path


def _assert_safe_archive_path(path: PurePosixPath) -> None:
    if path.is_absolute() or not path.parts:
        raise ValueError(f"archive path must be relative: {path}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"archive path contains an unsafe component: {path}")
    if path.name.lower() in _SENSITIVE_NAMES:
        raise ValueError(f"sensitive file is not allowed in an artifact: {path}")
    if path.suffix.lower() in {".p12", ".pfx"}:
        raise ValueError(f"private-key container is not allowed: {path}")


def _add_entry(
    entries: dict[PurePosixPath, Path],
    archive_path: PurePosixPath,
    source_path: Path,
) -> None:
    _assert_safe_archive_path(archive_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError(f"artifact source must be a regular file: {source_path}")
    if archive_path in entries:
        raise ValueError(f"duplicate artifact path: {archive_path}")
    lowered = archive_path.as_posix().lower()
    if any(existing.as_posix().lower() == lowered for existing in entries):
        raise ValueError(f"case-colliding artifact path: {archive_path}")
    entries[archive_path] = source_path


def static_asset_entries(repository: Path) -> dict[PurePosixPath, Path]:
    """Return the exact public files copied to the CloudFront S3 origin."""

    repository = repository.resolve()
    dashboard = repository / "src/gateway/admin/static"
    site = repository / "site"
    entries: dict[PurePosixPath, Path] = {}

    index = dashboard / "index.html"
    _add_entry(entries, PurePosixPath("index.html"), index)
    for path in _regular_files(dashboard):
        relative = path.relative_to(dashboard)
        if relative == Path("index.html"):
            continue
        _add_entry(
            entries,
            PurePosixPath("admin/static") / PurePosixPath(relative.as_posix()),
            path,
        )

    for path in _regular_files(site):
        relative = path.relative_to(site)
        if relative == Path("index.html"):
            continue
        archive_path = PurePosixPath(relative.as_posix())
        if archive_path.suffix.lower() not in _STATIC_SITE_TYPES:
            continue
        if len(archive_path.parts) == 1:
            _add_entry(entries, archive_path, path)
            continue
        if (
            len(archive_path.parts) == 2
            and archive_path.parts[0] in _STATIC_SITE_DIRS
        ):
            _add_entry(entries, archive_path, path)
    return entries


def _runtime_source_entries(repository: Path) -> dict[PurePosixPath, Path]:
    entries: dict[PurePosixPath, Path] = {}
    for root_name in ("axonllm", "src"):
        root = repository / root_name
        for path in _regular_files(root):
            relative = path.relative_to(repository)
            if path.suffix != ".py" and path.name != "py.typed":
                continue
            if any(part in _SOURCE_EXCLUDED_PARTS for part in relative.parts):
                continue
            _add_entry(
                entries,
                PurePosixPath(relative.as_posix()),
                path,
            )

    config = repository / "config"
    for path in _regular_files(config):
        relative = path.relative_to(repository)
        if len(relative.parts) != 2:
            continue
        if path.suffix not in {".yaml", ".example"}:
            continue
        _add_entry(entries, PurePosixPath(relative.as_posix()), path)

    architecture = repository / "docs/architecture.svg"
    _add_entry(
        entries,
        PurePosixPath("docs/architecture.svg"),
        architecture,
    )
    return entries


def lambda_artifact_entries(
    repository: Path,
    dependency_root: Path,
) -> dict[PurePosixPath, Path]:
    """Return application and pre-resolved dependency files for Lambda."""

    repository = repository.resolve()
    entries = _runtime_source_entries(repository)
    dependency_root = dependency_root.resolve()
    for path in _regular_files(dependency_root):
        relative = path.relative_to(dependency_root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if relative.parts and relative.parts[0] in {"bin", "scripts"}:
            continue
        _add_entry(
            entries,
            PurePosixPath(relative.as_posix()),
            path,
        )
    return entries


def _tree_sha256(entries: Mapping[PurePosixPath, Path]) -> str:
    digest = hashlib.sha256()
    for archive_path, source_path in sorted(
        entries.items(),
        key=lambda item: item[0].as_posix(),
    ):
        payload = source_path.read_bytes()
        digest.update(archive_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _archive_tree_sha256(
    archive: zipfile.ZipFile,
    information: list[zipfile.ZipInfo],
) -> str:
    digest = hashlib.sha256()
    for item in information:
        payload = archive.read(item)
        digest.update(item.filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_zip(
    output_directory: Path,
    *,
    prefix: str,
    entries: Mapping[PurePosixPath, Path],
    maximum_uncompressed_bytes: int,
) -> ArtifactMetadata:
    if not entries:
        raise ValueError(f"{prefix} artifact has no files")
    total_bytes = sum(path.stat().st_size for path in entries.values())
    if total_bytes > maximum_uncompressed_bytes:
        raise ValueError(
            f"{prefix} artifact is {total_bytes} bytes, exceeding "
            f"the {maximum_uncompressed_bytes}-byte limit"
        )

    output_directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_directory,
        prefix=f".{prefix}-",
        suffix=".zip",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for archive_path, source_path in sorted(
                entries.items(),
                key=lambda item: item[0].as_posix(),
            ):
                information = zipfile.ZipInfo(
                    archive_path.as_posix(),
                    date_time=_ZIP_TIMESTAMP,
                )
                information.compress_type = zipfile.ZIP_DEFLATED
                information.create_system = 3
                information.external_attr = (
                    stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR
                    | stat.S_IRGRP | stat.S_IROTH
                ) << 16
                archive.writestr(
                    information,
                    source_path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )

        payload = temporary.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        file_name = f"{prefix}-{sha256}.zip"
        destination = output_directory / file_name
        if destination.exists():
            if destination.read_bytes() != payload:
                raise ValueError(
                    f"content-addressed artifact mismatch: {destination}"
                )
            temporary.unlink()
        else:
            temporary.replace(destination)
        return ArtifactMetadata(
            entry_count=len(entries),
            file_name=file_name,
            sha256=sha256,
            size_bytes=len(payload),
            tree_sha256=_tree_sha256(entries),
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build_artifacts(
    repository: Path,
    output_directory: Path,
    dependency_root: Path,
    source_revision: str,
) -> ArtifactReceipt:
    """Build both serverless artifacts and their deterministic receipt."""

    if _COMMIT.fullmatch(source_revision) is None:
        raise ValueError(
            "source_revision must be a full lowercase Git commit SHA"
        )
    repository = repository.resolve()
    output_directory = output_directory.resolve()
    static_metadata = _write_zip(
        output_directory,
        prefix="axonllm-static-assets",
        entries=static_asset_entries(repository),
        maximum_uncompressed_bytes=_MAX_STATIC_BYTES,
    )
    control_metadata = _write_zip(
        output_directory,
        prefix="axonllm-control-api",
        entries=lambda_artifact_entries(repository, dependency_root),
        maximum_uncompressed_bytes=_MAX_LAMBDA_BYTES,
    )
    receipt = ArtifactReceipt(
        control_api=control_metadata,
        schema="axonllm.serverless-control-artifacts/v1",
        source_revision=source_revision,
        static_assets=static_metadata,
    )
    receipt_path = output_directory / "serverless-control-artifacts.json"
    payload = receipt.to_json()
    if receipt_path.exists() and receipt_path.read_bytes() != payload:
        raise ValueError(f"artifact receipt already differs: {receipt_path}")
    receipt_path.write_bytes(payload)
    return receipt


def _read_metadata(
    value: object,
    *,
    prefix: str,
) -> ArtifactMetadata:
    if not isinstance(value, dict) or set(value) != {
        "entryCount",
        "fileName",
        "sha256",
        "sizeBytes",
        "treeSha256",
    }:
        raise ValueError(f"{prefix} receipt metadata is invalid")
    entry_count = value["entryCount"]
    file_name = value["fileName"]
    sha256 = value["sha256"]
    size_bytes = value["sizeBytes"]
    tree_sha256 = value["treeSha256"]
    if (
        not isinstance(entry_count, int)
        or isinstance(entry_count, bool)
        or entry_count <= 0
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes <= 0
        or not isinstance(file_name, str)
        or not isinstance(sha256, str)
        or not isinstance(tree_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", tree_sha256) is None
    ):
        raise ValueError(f"{prefix} receipt metadata is invalid")
    expected_name = f"{prefix}-{sha256}.zip"
    if file_name != expected_name:
        raise ValueError(
            f"{prefix} artifact name is not content-addressed"
        )
    return ArtifactMetadata(
        entry_count=entry_count,
        file_name=file_name,
        sha256=sha256,
        size_bytes=size_bytes,
        tree_sha256=tree_sha256,
    )


def _verify_zip(
    directory: Path,
    metadata: ArtifactMetadata,
    *,
    maximum_uncompressed_bytes: int,
) -> None:
    path = directory / metadata.file_name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"artifact is not a regular file: {path}")
    payload = path.read_bytes()
    if len(payload) != metadata.size_bytes:
        raise ValueError(f"artifact size does not match receipt: {path}")
    if hashlib.sha256(payload).hexdigest() != metadata.sha256:
        raise ValueError(f"artifact digest does not match receipt: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            information = archive.infolist()
            names = [item.filename for item in information]
            if names != sorted(names) or len(names) != len(set(names)):
                raise ValueError(
                    f"artifact paths are not unique and sorted: {path}"
                )
            if len(information) != metadata.entry_count:
                raise ValueError(
                    f"artifact entry count does not match receipt: {path}"
                )
            if sum(item.file_size for item in information) > (
                maximum_uncompressed_bytes
            ):
                raise ValueError(
                    f"artifact exceeds the uncompressed size limit: {path}"
                )
            lowered: set[str] = set()
            for item in information:
                archive_path = PurePosixPath(item.filename)
                _assert_safe_archive_path(archive_path)
                if item.is_dir():
                    raise ValueError(
                        f"artifact contains a directory entry: {item.filename}"
                    )
                if archive_path.as_posix().lower() in lowered:
                    raise ValueError(
                        f"artifact contains case-colliding paths: {path}"
                    )
                lowered.add(archive_path.as_posix().lower())
                if item.date_time != _ZIP_TIMESTAMP:
                    raise ValueError(
                        f"artifact timestamp is not deterministic: {path}"
                    )
                mode = item.external_attr >> 16
                if (
                    stat.S_IFMT(mode) != stat.S_IFREG
                    or stat.S_IMODE(mode) != 0o644
                ):
                    raise ValueError(
                        f"artifact entry mode is unsafe: {item.filename}"
                    )
            if (
                _archive_tree_sha256(archive, information)
                != metadata.tree_sha256
            ):
                raise ValueError(
                    f"artifact tree digest does not match receipt: {path}"
                )
    except zipfile.BadZipFile as exc:
        raise ValueError(f"artifact is not a valid ZIP file: {path}") from exc


def verify_artifacts(
    directory: Path,
    *,
    expected_source_revision: str | None = None,
) -> ArtifactReceipt:
    """Verify an artifact directory without trusting its receipt."""

    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError(f"artifact directory does not exist: {directory}")
    receipt_path = directory / "serverless-control-artifacts.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("serverless artifact receipt is missing")
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("serverless artifact receipt is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "artifacts",
        "schema",
        "sourceRevision",
    }:
        raise ValueError("serverless artifact receipt shape is invalid")
    if value["schema"] != "axonllm.serverless-control-artifacts/v1":
        raise ValueError("serverless artifact receipt schema is unsupported")
    source_revision = value["sourceRevision"]
    if (
        not isinstance(source_revision, str)
        or _COMMIT.fullmatch(source_revision) is None
    ):
        raise ValueError("serverless artifact source revision is invalid")
    if (
        expected_source_revision is not None
        and source_revision != expected_source_revision
    ):
        raise ValueError("serverless artifact source revision does not match")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "controlApi",
        "staticAssets",
    }:
        raise ValueError("serverless artifact receipt targets are invalid")
    control_api = _read_metadata(
        artifacts["controlApi"],
        prefix="axonllm-control-api",
    )
    static_assets = _read_metadata(
        artifacts["staticAssets"],
        prefix="axonllm-static-assets",
    )
    expected_files = {
        "serverless-control-artifacts.json",
        control_api.file_name,
        static_assets.file_name,
    }
    directory_entries = list(directory.iterdir())
    if any(
        path.is_symlink() or not path.is_file()
        for path in directory_entries
    ):
        raise ValueError(
            "serverless artifact directory contains non-regular entries"
        )
    actual_files = {path.name for path in directory_entries}
    if actual_files != expected_files:
        raise ValueError("serverless artifact directory contains extra files")
    _verify_zip(
        directory,
        control_api,
        maximum_uncompressed_bytes=_MAX_LAMBDA_BYTES,
    )
    _verify_zip(
        directory,
        static_assets,
        maximum_uncompressed_bytes=_MAX_STATIC_BYTES,
    )
    return ArtifactReceipt(
        control_api=control_api,
        schema=value["schema"],
        source_revision=source_revision,
        static_assets=static_assets,
    )


__all__ = [
    "ArtifactMetadata",
    "ArtifactReceipt",
    "build_artifacts",
    "lambda_artifact_entries",
    "static_asset_entries",
    "verify_artifacts",
]
