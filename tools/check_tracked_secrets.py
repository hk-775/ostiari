"""Fail when high-confidence credentials appear in Git-tracked files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PATTERNS = {
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
}

ALLOWED_FIXTURES = {
    (
        "gateway/tests/test_security_failclosed.py",
        "AKIAIOSFODNN7EXAMPLE",
    ),
    (
        "tests/unit/test_detect.py",
        "AKIAIOSFODNN7EXAMPLE",
    ),
    (
        "tests/unit/test_detect.py",
        "-----BEGIN RSA PRIVATE KEY-----",
    ),
}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"])
    return [
        Path(raw.decode())
        for raw in output.split(b"\0")
        if raw
    ]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        try:
            raw = path.read_bytes()
        except OSError as exc:
            findings.append(f"{path}: cannot read tracked file: {exc}")
            continue
        if b"\0" in raw or len(raw) > 2 * 1024 * 1024:
            continue
        text = raw.decode("utf-8", errors="ignore")
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                if (path.as_posix(), value) in ALLOWED_FIXTURES:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: possible {label}")

    if findings:
        print("Tracked secret scan failed:")
        print("\n".join(f"  {finding}" for finding in findings))
        return 1
    print("Tracked secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
