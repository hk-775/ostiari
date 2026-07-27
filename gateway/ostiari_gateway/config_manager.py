"""Config manager — handles dynamic configuration from control plane."""

import logging
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ostiari import Guard
from ostiari.models import OstiariConfig, ThresholdConfig
from ostiari_gateway.models import PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.tool_proxy import ToolProxy

log = logging.getLogger("ostiari.sidecar")


class ConfigManager:
    """Manages sidecar configuration and synchronizes Guard + ToolProxy state."""

    def __init__(self) -> None:
        self._tool_proxy = ToolProxy()
        self._guard: Guard | None = None
        self._config: SidecarConfig = SidecarConfig()
        self._policy_file: Path | None = None

    @property
    def tool_proxy(self) -> ToolProxy:
        return self._tool_proxy

    @property
    def guard(self) -> Guard:
        if self._guard is None:
            self._guard = Guard()
            self._guard.start()
        return self._guard

    @property
    def config(self) -> SidecarConfig:
        return self._config

    def apply_config(self, config: SidecarConfig) -> dict[str, Any]:
        """Apply a full configuration (tools + policy). Returns summary."""
        self._config = config

        # Register tools
        self._tool_proxy.clear()
        for tool in config.tools:
            self._tool_proxy.register(tool)

        # Apply policy
        self._apply_policy(config.policy)

        return {
            "tools_registered": len(config.tools),
            "policy_applied": True,
            "sidecar_id": config.sidecar_id,
        }

    def apply_tools(self, tools: list[ToolDefinition]) -> dict[str, Any]:
        """Hot-reload tool definitions."""
        self._tool_proxy.clear()
        for tool in tools:
            self._tool_proxy.register(tool)
        self._config.tools = tools
        return {"tools_registered": len(tools)}

    def add_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        """Add a single tool without clearing others."""
        self._tool_proxy.register(tool)
        existing = [t for t in self._config.tools if t.name != tool.name]
        existing.append(tool)
        self._config.tools = existing
        return {"tool": tool.name, "status": "registered"}

    def remove_tool(self, name: str) -> dict[str, Any]:
        """Remove a single tool."""
        removed = self._tool_proxy.unregister(name)
        if removed:
            self._config.tools = [t for t in self._config.tools if t.name != name]
        return {"tool": name, "removed": removed}

    def apply_policy(self, policy: PolicyConfig) -> dict[str, Any]:
        """Hot-reload policy."""
        self._apply_policy(policy)
        self._config.policy = policy
        return {"policy_applied": True}

    def _apply_policy(self, policy: PolicyConfig) -> None:
        """Write policy to temp file and configure guard."""
        policy_data: dict[str, Any] = {}
        if policy.allow:
            policy_data["allow"] = policy.allow
        if policy.block:
            policy_data["block"] = policy.block
        if policy.rules:
            policy_data["rules"] = policy.rules
        if policy.thresholds:
            policy_data["thresholds"] = policy.thresholds

        if not policy_data:
            return

        # Write to temp file for Guard to consume. delete=False because the file
        # must outlive the handle — Guard reads it by path, and it's rewritten in
        # place on every config push. The `with` only scopes the handle, not the
        # file's lifetime.
        if self._policy_file is None:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", prefix="ostiari_policy_", delete=False
            ) as f:
                self._policy_file = Path(f.name)

        self._policy_file.write_text(yaml.dump(policy_data))

        # Reconfigure guard
        if self._guard is not None:
            self._guard.shutdown()

        thresholds = None
        if policy.thresholds and "global" in policy.thresholds:
            g = policy.thresholds["global"]
            thresholds = ThresholdConfig(
                allow_max=g.get("allow_max", 30),
                intervene_max=g.get("intervene_max", 70),
            )

        self._guard = Guard(
            config=OstiariConfig(thresholds=thresholds or ThresholdConfig())
        )
        self._guard.configure(str(self._policy_file))
        self._guard.start()
        log.info("Policy reloaded from control plane")

    async def shutdown(self) -> None:
        await self._tool_proxy.close()
        if self._guard is not None:
            self._guard.shutdown()
        if self._policy_file is not None and self._policy_file.exists():
            self._policy_file.unlink()
