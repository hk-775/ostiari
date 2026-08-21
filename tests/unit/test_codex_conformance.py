"""Contracts for the exact supported Codex CLI profile."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "config/codex/model-catalog.json"
EXAMPLE_CONFIG = ROOT / "config/codex/config.toml.example"
HARNESS = ROOT / "tools/codex_conformance.py"


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("codex_conformance", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_catalog_disables_unsupported_responses_capabilities() -> None:
    models = json.loads(CATALOG.read_text())["models"]
    assert len(models) == 1
    model = models[0]
    assert model["slug"] == "ostiari-codex"
    assert model["supported_reasoning_levels"] == []
    assert model["supports_reasoning_summary_parameter"] is False
    assert model["default_reasoning_summary"] == "none"
    assert model["support_verbosity"] is False
    assert model["default_verbosity"] is None
    assert model["service_tiers"] == []
    assert model["supports_search_tool"] is False
    assert model["node_repl_disabled"] is True


def test_codex_harness_generates_a_safe_shell_call() -> None:
    harness = _harness()
    name, arguments = harness._safe_tool_call(
        [
            {
                "type": "function",
                "function": {
                    "name": "shell_command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "timeout_ms": {"type": "integer"},
                        },
                        "required": ["command"],
                    },
                },
            }
        ]
    )
    assert name == "shell_command"
    assert arguments == {"command": "printf OSTIARI_TOOL_OK"}


def test_codex_example_uses_the_reviewed_responses_profile() -> None:
    config = EXAMPLE_CONFIG.read_text()
    assert 'model = "ostiari-codex"' in config
    assert 'model_provider = "ostiari"' in config
    assert 'wire_api = "responses"' in config
    assert 'env_key = "OSTIARI_CODEX_TOKEN"' in config
    assert 'requires_openai_auth = false' in config
    assert 'model_catalog_json = "/absolute/path/to/ostiari/' in config


def test_ci_runs_the_exact_codex_version_and_blocks_artifacts_on_failure() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "Codex 0.148.0 conformance" in workflow
    assert "npm install --global @openai/codex@0.148.0" in workflow
    assert 'test "$(codex --version)" = "codex-cli 0.148.0"' in workflow
    assert "python tools/codex_conformance.py" in workflow
    assert "codex-conformance" in workflow.split("production-artifacts:", 1)[1]
