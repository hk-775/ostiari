"""YAML policy file parser with line-number tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ostiari.exceptions import PolicyValidationError

_ALLOWED_TOP_LEVEL_KEYS = {"allow", "block", "rules", "thresholds"}


@dataclass(frozen=True)
class ParsedYAML:
    allow: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)
    source_path: Path = field(default_factory=lambda: Path("."))
    line_map: dict[str, int] = field(default_factory=dict)


def parse_yaml(path: Path) -> ParsedYAML:
    """Parse a YAML policy file into a structured intermediate representation."""
    try:
        with open(path) as f:
            content = f.read()
    except OSError as e:
        raise PolicyValidationError(
            field="file",
            message=f"Cannot read file: {e}",
            suggestion=f"Verify the file exists at '{path}'.",
        ) from e

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise PolicyValidationError(
            field="file",
            message=f"Invalid YAML syntax: {e}",
            suggestion="Check YAML formatting and indentation.",
        ) from e

    if data is None:
        return ParsedYAML(source_path=path)

    if not isinstance(data, dict):
        raise PolicyValidationError(
            field="file",
            message="Policy file root must be a YAML mapping",
            suggestion="Ensure the file contains key-value pairs at the top level.",
        )

    unknown_keys = set(data.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        raise PolicyValidationError(
            field=", ".join(sorted(unknown_keys)),
            message=f"Unknown top-level keys: {sorted(unknown_keys)}",
            suggestion=f"Allowed keys are: {sorted(_ALLOWED_TOP_LEVEL_KEYS)}",
        )

    line_map = build_line_map(content)

    return ParsedYAML(
        allow=data.get("allow", []) or [],
        block=data.get("block", []) or [],
        rules=data.get("rules", []) or [],
        thresholds=data.get("thresholds", {}) or {},
        source_path=path,
        line_map=line_map,
    )


def build_line_map(content: str) -> dict[str, int]:
    """Build a mapping of dotted field paths to line numbers from YAML content."""
    try:
        root = yaml.compose(content)
    except yaml.YAMLError:
        return {}
    if root is None:
        return {}

    line_map: dict[str, int] = {}
    _walk_node(root, "", line_map)
    return line_map


def _walk_node(node: yaml.Node, prefix: str, line_map: dict[str, int]) -> None:
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = key_node.value
            path = f"{prefix}.{key}" if prefix else key
            line_map[path] = key_node.start_mark.line + 1
            _walk_node(value_node, path, line_map)
    elif isinstance(node, yaml.SequenceNode):
        for i, item_node in enumerate(node.value):
            path = f"{prefix}[{i}]"
            line_map[path] = item_node.start_mark.line + 1
            _walk_node(item_node, path, line_map)
