"""Ensemble configuration loader — loads and serves ensemble presets from YAML.

Mirrors :class:`~src.gateway.model_leaderboard.ModelLeaderboard`'s
load / ``from_yaml`` / accessor conventions. Parses ``config/ensemble.yaml``
into :class:`~src.gateway.models.EnsemblePreset` objects, applying defaults at
parse time and validating each preset via
:meth:`~src.gateway.ensemble.EnsembleStrategy.validate_preset`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.gateway.ensemble import (
    DEFAULT_FALLBACK_POLICY,
    DEFAULT_QUORUM,
    EnsembleStrategy,
)
from src.gateway.models import EnsemblePreset

logger = logging.getLogger(__name__)


class EnsembleConfig:
    """Loads and serves ensemble presets from YAML config."""

    def __init__(self) -> None:
        self._presets: dict[str, EnsemblePreset] = {}
        self._default_preset_name: str | None = None

    def load(self, config_path: str) -> None:
        """Load presets from a YAML file. Missing file → empty config + warning."""
        path = Path(config_path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning(
                "Ensemble config file not found: %s — using empty config", config_path
            )
            return
        except OSError as exc:
            logger.error("Error reading ensemble config: %s", exc)
            return

        self._parse_yaml(raw)

    @classmethod
    def from_yaml(cls, yaml_str: str) -> "EnsembleConfig":
        """Parse a YAML string into a populated EnsembleConfig."""
        config = cls()
        config._parse_yaml(yaml_str)
        return config

    def get_preset(self, name: str) -> EnsemblePreset | None:
        """Return the preset with the given name, or None if not present."""
        return self._presets.get(name)

    def default_preset(self) -> EnsemblePreset | None:
        """Return the preset named by default_preset, or None when unset."""
        if self._default_preset_name is None:
            return None
        return self._presets.get(self._default_preset_name)

    @property
    def presets(self) -> dict[str, EnsemblePreset]:
        """Return all loaded presets keyed by name."""
        return self._presets

    @property
    def is_configured(self) -> bool:
        """True when at least one preset has been loaded."""
        return bool(self._presets)

    def _parse_yaml(self, yaml_str: str) -> None:
        """Parse YAML content and populate presets.

        Malformed top-level YAML is logged and leaves the config empty, mirroring
        :class:`ModelLeaderboard`. ``EnsembleConfigError`` raised during preset
        validation is allowed to propagate so the offending preset is surfaced.
        """
        try:
            data = yaml.safe_load(yaml_str)
        except yaml.YAMLError as exc:
            logger.error("Malformed ensemble YAML: %s", exc)
            return

        if not isinstance(data, dict):
            logger.error(
                "Ensemble YAML must be a mapping, got %s", type(data).__name__
            )
            return

        ensemble = data.get("ensemble")
        if not isinstance(ensemble, dict):
            logger.warning("No 'ensemble' section found in ensemble YAML")
            return

        presets_raw = ensemble.get("presets")
        if not isinstance(presets_raw, dict):
            logger.warning("No 'presets' mapping found in ensemble YAML")
            return

        default_preset_name = ensemble.get("default_preset")
        self._default_preset_name = (
            str(default_preset_name) if default_preset_name is not None else None
        )

        for name, entry in presets_raw.items():
            if not isinstance(entry, dict):
                logger.warning("Invalid preset entry for '%s', skipping", name)
                continue

            preset = EnsemblePreset(
                name=str(name),
                panel=list(entry.get("panel") or []),
                judge=entry.get("judge"),
                quorum=entry.get("quorum", DEFAULT_QUORUM),
                fallback_policy=entry.get("fallback_policy", DEFAULT_FALLBACK_POLICY),
                cost_ceiling=entry.get("cost_ceiling"),
                ranking_criteria=entry.get("ranking_criteria", "length"),
            )

            # Validate; EnsembleConfigError identifies the offending preset and
            # is allowed to propagate to the caller.
            EnsembleStrategy.validate_preset(preset)

            self._presets[preset.name] = preset
