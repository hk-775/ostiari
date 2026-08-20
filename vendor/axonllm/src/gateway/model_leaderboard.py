"""Model leaderboard — loads and serves benchmark rankings from YAML config."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.gateway.models import ModelScore

logger = logging.getLogger(__name__)


class ModelLeaderboard:
    """Loads and serves model benchmark rankings from YAML config."""

    def __init__(self) -> None:
        self._rankings: dict[str, list[ModelScore]] = {}
        self._config: dict = {}

    def load(self, config_path: str, valid_models: set[str] | None = None) -> None:
        """Load leaderboard from YAML file. Skip models not in valid_models."""
        path = Path(config_path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.error("Leaderboard config file not found: %s", config_path)
            return
        except OSError as exc:
            logger.error("Error reading leaderboard config: %s", exc)
            return

        self._parse_yaml(raw, valid_models)

    @classmethod
    def from_yaml(cls, yaml_str: str, valid_models: set[str] | None = None) -> "ModelLeaderboard":
        """Parse YAML string into ModelLeaderboard."""
        leaderboard = cls()
        leaderboard._parse_yaml(yaml_str, valid_models)
        return leaderboard

    def get_rankings(self, task_type: str) -> list[ModelScore]:
        """Return models ranked by score descending for a task type.

        Returns empty list if task_type not found.
        """
        return self._rankings.get(task_type, [])

    def get_score(self, task_type: str, model_name: str) -> float | None:
        """Get a specific model's score for a task type."""
        rankings = self._rankings.get(task_type, [])
        for ms in rankings:
            if ms.model_name == model_name:
                return ms.score
        return None

    @property
    def config(self) -> dict:
        """Return the raw smart_routing config section."""
        return self._config

    def _parse_yaml(self, yaml_str: str, valid_models: set[str] | None) -> None:
        """Parse YAML content and populate rankings."""
        try:
            data = yaml.safe_load(yaml_str)
        except yaml.YAMLError as exc:
            logger.error("Malformed leaderboard YAML: %s", exc)
            return

        if not isinstance(data, dict):
            logger.error("Leaderboard YAML must be a mapping, got %s", type(data).__name__)
            return

        # Store smart_routing config section
        smart_routing = data.get("smart_routing")
        if isinstance(smart_routing, dict):
            self._config = smart_routing

        task_types = data.get("task_types")
        if not isinstance(task_types, dict):
            logger.warning("No 'task_types' section found in leaderboard YAML")
            return

        for task_type, task_data in task_types.items():
            if not isinstance(task_data, dict):
                logger.warning("Invalid task type entry for '%s', skipping", task_type)
                continue

            models_list = task_data.get("models")
            if not isinstance(models_list, list):
                logger.warning("No 'models' list for task type '%s', skipping", task_type)
                continue

            scores: list[ModelScore] = []
            for entry in models_list:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                score = entry.get("score")
                if name is None or score is None:
                    continue

                # Validate against valid_models if provided
                if valid_models is not None and name not in valid_models:
                    logger.warning(
                        "Model '%s' in leaderboard not found in registry, skipping",
                        name,
                    )
                    continue

                try:
                    scores.append(ModelScore(model_name=str(name), score=float(score)))
                except (ValueError, TypeError) as exc:
                    logger.warning("Invalid score for model '%s': %s", name, exc)
                    continue

            # Sort descending by score
            scores.sort(key=lambda ms: ms.score, reverse=True)
            self._rankings[task_type] = scores
