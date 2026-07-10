"""Unit tests for ostiari.config."""

import pytest

from ostiari.config import ConfigLoader, _coerce, _merge
from ostiari.exceptions import ConfigError
from ostiari.models import OstiariConfig


class TestMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3}
        assert _merge(base, override) == {"a": 1, "b": 3}

    def test_deep_merge_dicts(self):
        base = {"nested": {"a": 1, "b": 2}}
        override = {"nested": {"b": 3, "c": 4}}
        result = _merge(base, override)
        assert result == {"nested": {"a": 1, "b": 3, "c": 4}}

    def test_none_does_not_override(self):
        base = {"a": 1}
        override = {"a": None}
        assert _merge(base, override) == {"a": 1}

    def test_list_replaces_entirely(self):
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        assert _merge(base, override) == {"items": [4, 5]}

    def test_base_not_mutated(self):
        base = {"a": 1}
        _merge(base, {"a": 2})
        assert base == {"a": 1}


class TestCoerce:
    def test_true_values(self):
        assert _coerce("true") is True
        assert _coerce("True") is True
        assert _coerce("1") is True
        assert _coerce("yes") is True

    def test_false_values(self):
        assert _coerce("false") is False
        assert _coerce("False") is False
        assert _coerce("0") is False
        assert _coerce("no") is False

    def test_integer(self):
        assert _coerce("42") == 42
        assert _coerce("100") == 100

    def test_comma_separated_list(self):
        assert _coerce("a,b,c") == ["a", "b", "c"]

    def test_plain_string(self):
        assert _coerce("hello") == "hello"
        assert _coerce("/path/to/file") == "/path/to/file"


class TestConfigLoaderLoadDefaults:
    def test_returns_config_with_defaults(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        config = ConfigLoader.load()
        assert isinstance(config, OstiariConfig)
        assert config.fail_open is True
        assert config.storage_path == "ostiari.db"


class TestConfigLoaderYaml:
    def test_loads_yaml_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        yaml_file = tmp_path / "ostiari.yaml"
        yaml_file.write_text("fail_open: false\nstorage_path: custom.db\n")
        config = ConfigLoader.load()
        assert config.fail_open is False
        assert config.storage_path == "custom.db"

    def test_explicit_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        yaml_file = tmp_path / "custom.yaml"
        yaml_file.write_text("log_level: DEBUG\n")
        config = ConfigLoader.load(path=yaml_file)
        assert config.log_level == "DEBUG"

    def test_invalid_yaml_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text(":\n  - [invalid\n")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            ConfigLoader.load(path=yaml_file)

    def test_non_dict_yaml_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        yaml_file = tmp_path / "list.yaml"
        yaml_file.write_text("- item1\n- item2\n")
        with pytest.raises(ConfigError, match="root must be a mapping"):
            ConfigLoader.load(path=yaml_file)


class TestConfigLoaderEnv:
    def test_top_level_env_var(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        monkeypatch.setenv("AGENTGUARD_FAIL_OPEN", "false")
        config = ConfigLoader.load()
        assert config.fail_open is False

    def test_nested_env_var_double_underscore(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        monkeypatch.setenv("AGENTGUARD_THRESHOLDS__ALLOW_MAX", "20")
        monkeypatch.setenv("AGENTGUARD_THRESHOLDS__INTERVENE_MAX", "80")
        config = ConfigLoader.load()
        assert config.thresholds.allow_max == 20
        assert config.thresholds.intervene_max == 80

    def test_storage_path_with_underscore(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        monkeypatch.setenv("AGENTGUARD_STORAGE_PATH", "/tmp/test.db")
        config = ConfigLoader.load()
        assert config.storage_path == "/tmp/test.db"


class TestConfigLoaderOverrides:
    def test_overrides_take_precedence(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        config = ConfigLoader.load(overrides={"fail_open": False, "log_level": "ERROR"})
        assert config.fail_open is False
        assert config.log_level == "ERROR"


class TestConfigLoaderValidation:
    def test_invalid_log_level(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AGENTGUARD_CONFIG", raising=False)
        with pytest.raises(ConfigError, match="log_level"):
            ConfigLoader.load(overrides={"log_level": "TRACE"})
