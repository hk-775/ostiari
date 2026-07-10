"""Ostiari configuration loading and merging."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from ostiari.exceptions import ConfigError
from ostiari.models import OstiariConfig

logger = logging.getLogger("ostiari")

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class ConfigLoader:
    """Loads configuration from multiple sources with precedence merge."""

    @staticmethod
    def load(
        path: str | Path | None = None,
        env_prefix: str = "AGENTGUARD_",
        overrides: dict[str, Any] | None = None,
    ) -> OstiariConfig:
        defaults = OstiariConfig().model_dump()
        env_config = ConfigLoader._parse_env(env_prefix)
        yaml_config = ConfigLoader._load_yaml(path)
        merged = _merge(defaults, env_config)
        merged = _merge(merged, yaml_config)
        if overrides:
            merged = _merge(merged, overrides)
        config = OstiariConfig.model_validate(merged)
        errors = ConfigLoader._validate_business_rules(config)
        if errors:
            raise errors[0]
        logger.info("Config loaded (source: %s)", _describe_sources(path, env_config, overrides))
        return config

    @staticmethod
    def _load_yaml(path: str | Path | None) -> dict[str, Any]:
        resolved = _resolve_yaml_path(path)
        if resolved is None:
            return {}
        try:
            with open(resolved) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(
                field="config_file",
                reason=f"Invalid YAML syntax: {e}",
                suggestion="Check YAML formatting and indentation.",
            ) from e
        except OSError as e:
            raise ConfigError(
                field="config_file",
                reason=f"Cannot read file: {e}",
                suggestion=f"Verify the file exists at '{resolved}'.",
            ) from e
        if not isinstance(data, dict):
            raise ConfigError(
                field="config_file",
                reason="YAML root must be a mapping",
                suggestion="Ensure the config file contains key-value pairs at the top level.",
            )
        return data

    @staticmethod
    def _parse_env(prefix: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix) :].lower()
            parts = remainder.split("__")
            target = result
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = _coerce(value)
        return result

    @staticmethod
    def _validate_business_rules(config: OstiariConfig) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if config.log_level not in _VALID_LOG_LEVELS:
            errors.append(
                ConfigError(
                    field="log_level",
                    reason=f"'{config.log_level}' is not valid",
                    suggestion=f"Must be one of: {', '.join(sorted(_VALID_LOG_LEVELS))}",
                )
            )
        if config.adaptive_sensitivity <= 0:
            errors.append(
                ConfigError(
                    field="adaptive_sensitivity",
                    reason="Must be > 0",
                    suggestion="Set a positive float value (default: 2.0).",
                )
            )
        for name, breaker in config.breakers.items():
            if breaker.threshold <= 0:
                errors.append(
                    ConfigError(
                        field=f"breakers.{name}.threshold",
                        reason="Must be > 0",
                        suggestion="Set a positive threshold value.",
                    )
                )
        return errors


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = base.copy()
    for key, value in override.items():
        if value is None:
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _coerce(value: str) -> Any:
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    if "," in value:
        return value.split(",")
    return value


def _resolve_yaml_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path)
    env_path = os.environ.get("AGENTGUARD_CONFIG")
    if env_path:
        return Path(env_path)
    default = Path("ostiari.yaml")
    if default.exists():
        return default
    return None


def _describe_sources(
    path: str | Path | None,
    env_config: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> str:
    sources = ["defaults"]
    if path or os.environ.get("AGENTGUARD_CONFIG") or Path("ostiari.yaml").exists():
        sources.append("yaml")
    if env_config:
        sources.append("env")
    if overrides:
        sources.append("overrides")
    return " + ".join(sources)
