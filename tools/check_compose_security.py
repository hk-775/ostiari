"""Validate the rendered local Compose stack's security invariants."""

from __future__ import annotations

import json
import subprocess

COMPOSE_FILES = (
    "deploy/docker/docker-compose.yml",
    "deploy/docker/docker-compose.demo.yml",
)


def main() -> int:
    command = ["docker", "compose"]
    for path in COMPOSE_FILES:
        command.extend(["-f", path])
    command.extend(["config", "--format", "json"])
    rendered = json.loads(subprocess.check_output(command, text=True))

    for name, service in sorted(rendered["services"].items()):
        configured = str(service.get("user", "")).strip()
        user = configured.split(":", 1)[0].lower()
        if user in {"", "0", "root"}:
            raise RuntimeError(
                f"Compose service {name!r} does not pin a non-root user"
            )
        if service.get("read_only") is not True:
            raise RuntimeError(
                f"Compose service {name!r} does not use a read-only root filesystem"
            )
        if "ALL" not in service.get("cap_drop", []):
            raise RuntimeError(
                f"Compose service {name!r} does not drop all Linux capabilities"
            )
        if "no-new-privileges:true" not in service.get("security_opt", []):
            raise RuntimeError(
                f"Compose service {name!r} permits privilege escalation"
            )
        for port in service.get("ports") or []:
            if port.get("host_ip") != "127.0.0.1":
                raise RuntimeError(
                    f"Compose service {name!r} publishes a non-loopback port"
                )
        print(f"{name}: non-root, read-only, no privileges, loopback-only")

    redis = rendered["services"].get("redis", {})
    if redis.get("ports"):
        raise RuntimeError("Compose Valkey must not be published to the host")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
