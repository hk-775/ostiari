"""Unit tests for ostiari.policy.parser."""

import pytest

from ostiari.exceptions import PolicyValidationError
from ostiari.policy.parser import build_line_map, parse_yaml


class TestParseYamlValid:
    def test_all_sections(self, tmp_path):
        f = tmp_path / "policy.yaml"
        f.write_text("""
allow:
  - safe_read
  - safe_list
block:
  - rm_rf
rules:
  - type: risk_adjust
    action: send_email
    risk_adjust: 20
thresholds:
  global:
    allow_max: 25
    intervene_max: 65
""")
        result = parse_yaml(f)
        assert result.allow == ["safe_read", "safe_list"]
        assert result.block == ["rm_rf"]
        assert len(result.rules) == 1
        assert result.rules[0]["type"] == "risk_adjust"
        assert result.thresholds["global"]["allow_max"] == 25
        assert result.source_path == f

    def test_partial_yaml_missing_sections(self, tmp_path):
        f = tmp_path / "policy.yaml"
        f.write_text("block:\n  - dangerous_tool\n")
        result = parse_yaml(f)
        assert result.allow == []
        assert result.block == ["dangerous_tool"]
        assert result.rules == []
        assert result.thresholds == {}

    def test_empty_file(self, tmp_path):
        f = tmp_path / "policy.yaml"
        f.write_text("")
        result = parse_yaml(f)
        assert result.allow == []
        assert result.block == []
        assert result.rules == []

    def test_empty_sections(self, tmp_path):
        f = tmp_path / "policy.yaml"
        f.write_text("allow:\nblock:\nrules:\nthresholds:\n")
        result = parse_yaml(f)
        assert result.allow == []
        assert result.block == []
        assert result.rules == []
        assert result.thresholds == {}


class TestParseYamlErrors:
    def test_invalid_yaml_syntax(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("allow:\n  - good\n  bad indentation")
        with pytest.raises(PolicyValidationError) as exc_info:
            parse_yaml(f)
        assert "YAML syntax" in exc_info.value.message

    def test_unknown_top_level_keys(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("allow:\n  - x\nunknown_key: true\n")
        with pytest.raises(PolicyValidationError) as exc_info:
            parse_yaml(f)
        assert "unknown_key" in exc_info.value.message

    def test_non_dict_root(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(PolicyValidationError) as exc_info:
            parse_yaml(f)
        assert "mapping" in exc_info.value.message

    def test_file_not_found(self, tmp_path):
        f = tmp_path / "nonexistent.yaml"
        with pytest.raises(PolicyValidationError) as exc_info:
            parse_yaml(f)
        assert "Cannot read" in exc_info.value.message


class TestLineMap:
    def test_line_numbers_extracted(self, tmp_path):
        f = tmp_path / "policy.yaml"
        content = "allow:\n  - tool_a\nblock:\n  - tool_b\nrules:\n  - type: risk_adjust\n    action: send_*\n    risk_adjust: 10\n"
        f.write_text(content)
        result = parse_yaml(f)
        assert "allow" in result.line_map
        assert "block" in result.line_map
        assert "rules" in result.line_map
        assert result.line_map["allow"] == 1
        assert result.line_map["block"] == 3
        assert result.line_map["rules"] == 5

    def test_build_line_map_empty(self):
        line_map = build_line_map("")
        assert line_map == {}

    def test_nested_line_numbers(self):
        content = "rules:\n  - type: risk_adjust\n    action: send_*\n"
        line_map = build_line_map(content)
        assert "rules[0].type" in line_map
        assert "rules[0].action" in line_map
