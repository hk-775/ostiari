"""Integration tests for policy hot-reload."""

import threading
from pathlib import Path

from ostiari.policy.engine import PolicyEngine


def _write(path: Path, content: str):
    path.write_text(content)


class TestHotReload:
    def test_reload_picks_up_file_changes(self, tmp_path):
        f = tmp_path / "policy.yaml"
        _write(f, "allow:\n  - tool_a\n")

        engine = PolicyEngine()
        engine.load([f])
        result = engine.evaluate("tool_a", {})
        assert result.decision == "allow"

        _write(f, "block:\n  - tool_a\n")
        assert engine.reload() is True

        result = engine.evaluate("tool_a", {})
        assert result.decision == "block"

    def test_invalid_reload_preserves_old(self, tmp_path):
        f = tmp_path / "policy.yaml"
        _write(f, "allow:\n  - tool_a\n")

        engine = PolicyEngine()
        engine.load([f])

        _write(f, "rules:\n  - type: invalid\n    action: x\n")
        assert engine.reload() is False

        result = engine.evaluate("tool_a", {})
        assert result.decision == "allow"

    def test_multi_file_reload(self, tmp_path):
        f1 = tmp_path / "base.yaml"
        f2 = tmp_path / "override.yaml"
        _write(f1, "allow:\n  - tool_a\n")
        _write(f2, "block:\n  - tool_b\n")

        engine = PolicyEngine()
        engine.load([f1, f2])
        assert engine.active_rule_count == 2

        _write(f2, "block:\n  - tool_b\n  - tool_c\n")
        engine.reload()
        assert engine.active_rule_count == 3

    def test_concurrent_evaluation_during_reload(self, tmp_path):
        f = tmp_path / "policy.yaml"
        _write(f, "allow:\n  - tool_a\n")

        engine = PolicyEngine()
        engine.load([f])

        errors = []
        stop = threading.Event()

        def evaluate_loop():
            while not stop.is_set():
                try:
                    result = engine.evaluate("tool_a", {})
                    assert result.decision in ("allow", "evaluate", "block")
                except Exception as e:
                    errors.append(e)
                    break

        threads = [threading.Thread(target=evaluate_loop) for _ in range(4)]
        for t in threads:
            t.start()

        for i in range(20):
            if i % 2 == 0:
                _write(f, "allow:\n  - tool_a\n  - tool_b\n")
            else:
                _write(f, "allow:\n  - tool_a\n")
            engine.reload()

        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
