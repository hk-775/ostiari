"""Verify every first-party release surface uses one canonical version."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent


def _module_version(path: str) -> str:
    tree = ast.parse((ROOT / path).read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ) and isinstance(node.value, ast.Constant) and isinstance(
            node.value.value,
            str,
        ):
            return node.value.value
    raise RuntimeError(f"{path} does not define a string __version__")


def _toml(path: str) -> dict:
    return tomllib.loads((ROOT / path).read_text())


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def _yaml_scalar(path: str, key: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*[\"']?([^\"'#\s]+)")
    for line in (ROOT / path).read_text().splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    raise RuntimeError(f"{path} does not define {key}")


def _image_tag(path: str) -> str:
    match = re.search(
        r"(?m)^\s+tag:\s*[\"']?([^\"'#\s]+)",
        (ROOT / path).read_text(),
    )
    if not match:
        raise RuntimeError(f"{path} does not define image.tag")
    return match.group(1)


def _semver(version: str) -> str:
    match = re.fullmatch(
        r"(?P<base>\d+\.\d+\.\d+)(?:(?P<phase>a|b|rc)(?P<number>\d+))?",
        version,
    )
    if not match:
        raise RuntimeError(f"unsupported PEP 440 release version: {version}")
    if not match.group("phase"):
        return match.group("base")
    label = {"a": "alpha", "b": "beta", "rc": "rc"}[match.group("phase")]
    return f"{match.group('base')}-{label}.{match.group('number')}"


def main() -> int:
    canonical = _module_version("src/ostiari/__init__.py")
    semver = _semver(canonical)

    gateway = _toml("gateway/pyproject.toml")["project"]
    control_plane = _toml("control-plane/backend/pyproject.toml")["project"]
    checks: list[tuple[str, str, str]] = [
        ("gateway package", gateway["version"], canonical),
        (
            "gateway root dependency",
            next(
                item
                for item in gateway["dependencies"]
                if item.startswith("ostiari>=")
            ),
            f"ostiari>={canonical}",
        ),
        ("gateway module", _module_version("gateway/ostiari_gateway/__init__.py"), canonical),
        ("control-plane package", control_plane["version"], canonical),
        (
            "control-plane module",
            _module_version("control-plane/backend/control_plane/__init__.py"),
            canonical,
        ),
        (
            "control-plane frontend",
            _json("control-plane/frontend/package.json")["version"],
            semver,
        ),
        (
            "control-plane frontend lock",
            _json("control-plane/frontend/package-lock.json")["version"],
            semver,
        ),
        ("dashboard UI", _json("dashboard-ui/package.json")["version"], semver),
        (
            "dashboard UI lock",
            _json("dashboard-ui/package-lock.json")["version"],
            semver,
        ),
        ("AWS deployment", _json("deploy/aws/package.json")["version"], semver),
        (
            "AWS deployment lock",
            _json("deploy/aws/package-lock.json")["version"],
            semver,
        ),
        (
            "Helm chart",
            _yaml_scalar("deploy/helm/ostiari-gateway/Chart.yaml", "version"),
            semver,
        ),
        (
            "Helm appVersion",
            _yaml_scalar(
                "deploy/helm/ostiari-gateway/Chart.yaml",
                "appVersion",
            ),
            canonical,
        ),
        (
            "Helm image tag",
            _image_tag("deploy/helm/ostiari-gateway/values.yaml"),
            canonical,
        ),
    ]

    failures = [
        f"{label}: found {actual!r}, expected {expected!r}"
        for label, actual, expected in checks
        if actual != expected
    ]
    required_text = {
        "CHANGELOG.md": f"## [{canonical}]",
        "control-plane/frontend/src/components/Layout.tsx": f"v{canonical}",
        "control-plane/frontend/src/pages/LoginPage.tsx": f"v{canonical}",
    }
    failures.extend(
        f"{path}: missing {text!r}"
        for path, text in required_text.items()
        if text not in (ROOT / path).read_text()
    )
    if failures:
        print("Release version check failed:")
        print("\n".join(f"  {failure}" for failure in failures))
        return 1

    print(f"Release versions aligned at {canonical} ({semver})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
