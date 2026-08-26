"""Fail when high-confidence credentials appear in repository files."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

PATTERNS = {
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
}

AWS_EXAMPLE_ACCESS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
RSA_PRIVATE_KEY_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"

ALLOWED_FIXTURES = {
    (
        "gateway/tests/test_security_failclosed.py",
        AWS_EXAMPLE_ACCESS_KEY,
    ),
    (
        "tests/unit/test_detect.py",
        AWS_EXAMPLE_ACCESS_KEY,
    ),
    (
        "tests/unit/test_detect.py",
        RSA_PRIVATE_KEY_HEADER,
    ),
    (
        "tools/check_tracked_secrets.py",
        AWS_EXAMPLE_ACCESS_KEY,
    ),
    (
        "tools/check_tracked_secrets.py",
        RSA_PRIVATE_KEY_HEADER,
    ),
}


def repository_files() -> list[Path]:
    # Include untracked, non-ignored files so a local pre-commit run also
    # examines newly created files before they enter history.
    output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ]
    )
    return [
        Path(raw.decode())
        for raw in output.split(b"\0")
        if raw
    ]


def _find_in_text(
    path: Path,
    text: str,
    *,
    location_prefix: str = "",
) -> list[str]:
    findings: list[str] = []
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group(0)
            if (path.as_posix(), value) in ALLOWED_FIXTURES:
                continue
            line = text.count("\n", 0, match.start()) + 1
            location = f"{location_prefix}{path}:{line}"
            findings.append(f"{location}: possible {label}")
    return findings


def scan_repository_files() -> list[str]:
    findings: list[str] = []
    for path in repository_files():
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            # A tracked file deleted in the working tree has no current content
            # to scan; its committed content is covered by --history.
            continue
        except OSError as exc:
            findings.append(f"{path}: cannot read repository file: {exc}")
            continue
        if b"\0" in raw or len(raw) > 2 * 1024 * 1024:
            continue
        text = raw.decode("utf-8", errors="ignore")
        findings.extend(_find_in_text(path, text))
    return findings


def history_additions() -> Iterable[tuple[str, Path, str]]:
    """Yield text added by every reachable commit, with its historical path."""
    output = subprocess.check_output(
        [
            "git",
            "log",
            "--all",
            "--format=OSTIARI_COMMIT:%H",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--unified=0",
        ],
        text=True,
        errors="ignore",
    )
    commit = "unknown"
    path: Path | None = None
    for line in output.splitlines():
        if line.startswith("OSTIARI_COMMIT:"):
            commit = line.removeprefix("OSTIARI_COMMIT:")
            path = None
            continue
        if line.startswith("+++ b/"):
            path = Path(line.removeprefix("+++ b/"))
            continue
        if path is not None and line.startswith("+") and not line.startswith("+++"):
            yield commit, path, line[1:]


def scan_history() -> list[str]:
    findings: list[str] = []
    for commit, path, line in history_additions():
        findings.extend(
            _find_in_text(
                path,
                line,
                location_prefix=f"{commit}:",
            )
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history",
        action="store_true",
        help="also scan text introduced by every reachable commit",
    )
    args = parser.parse_args()

    findings = scan_repository_files()
    if args.history:
        findings.extend(scan_history())
    findings = sorted(set(findings))
    if findings:
        print("Secret scan failed:")
        print("\n".join(f"  {finding}" for finding in findings))
        return 1
    scope = (
        "repository files and reachable history"
        if args.history
        else "repository files"
    )
    print(f"Secret scan passed for {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
