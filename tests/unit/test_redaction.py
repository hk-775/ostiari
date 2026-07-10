"""Unit tests for ostiari.storage.redaction."""

import pytest

from ostiari.storage.redaction import (
    REDACTED_VALUE,
    RedactionFilter,
)


@pytest.fixture
def redactor():
    return RedactionFilter()


class TestDefaultPatterns:
    def test_password_redacted(self, redactor):
        data = {"username": "admin", "password": "secret123"}
        result = redactor.redact(data)
        assert result["username"] == "admin"
        assert result["password"] == REDACTED_VALUE

    def test_secret_redacted(self, redactor):
        data = {"app_secret": "abc"}
        assert redactor.redact(data)["app_secret"] == REDACTED_VALUE

    def test_token_redacted(self, redactor):
        data = {"auth_token": "xyz", "refresh_token_id": "123"}
        result = redactor.redact(data)
        assert result["auth_token"] == REDACTED_VALUE
        assert result["refresh_token_id"] == REDACTED_VALUE

    def test_key_redacted(self, redactor):
        data = {"api_key": "k-123", "primary_key": "pk"}
        result = redactor.redact(data)
        assert result["api_key"] == REDACTED_VALUE
        assert result["primary_key"] == REDACTED_VALUE

    def test_credential_redacted(self, redactor):
        data = {"user_credential": "cred"}
        assert redactor.redact(data)["user_credential"] == REDACTED_VALUE

    def test_safe_field_unchanged(self, redactor):
        data = {"name": "Alice", "action": "send_email", "count": 42}
        result = redactor.redact(data)
        assert result == data


class TestRecursion:
    def test_nested_dict(self, redactor):
        data = {"headers": {"Authorization": "Bearer xyz", "Content-Type": "json"}}
        result = redactor.redact(data)
        assert result["headers"]["Content-Type"] == "json"
        # "Authorization" doesn't match default patterns (*password*, *secret*, etc.)
        # but let's test a key that does match
        data2 = {"config": {"db_password": "pass123", "host": "localhost"}}
        result2 = redactor.redact(data2)
        assert result2["config"]["db_password"] == REDACTED_VALUE
        assert result2["config"]["host"] == "localhost"

    def test_deeply_nested(self, redactor):
        data = {"level1": {"level2": {"level3": {"secret_value": "hidden"}}}}
        result = redactor.redact(data)
        assert result["level1"]["level2"]["level3"]["secret_value"] == REDACTED_VALUE

    def test_list_of_dicts(self, redactor):
        data = {"items": [{"api_key": "k1"}, {"name": "safe"}]}
        result = redactor.redact(data)
        assert result["items"][0]["api_key"] == REDACTED_VALUE
        assert result["items"][1]["name"] == "safe"

    def test_list_of_scalars_unchanged(self, redactor):
        data = {"tags": ["one", "two", "three"]}
        result = redactor.redact(data)
        assert result["tags"] == ["one", "two", "three"]


class TestImmutability:
    def test_original_not_mutated(self, redactor):
        data = {"password": "secret", "nested": {"api_key": "key"}}
        original_password = data["password"]
        redactor.redact(data)
        assert data["password"] == original_password
        assert data["nested"]["api_key"] == "key"


class TestCaseInsensitivity:
    def test_uppercase_key(self, redactor):
        data = {"PASSWORD": "secret", "API_KEY": "key"}
        result = redactor.redact(data)
        assert result["PASSWORD"] == REDACTED_VALUE
        assert result["API_KEY"] == REDACTED_VALUE

    def test_mixed_case_key(self, redactor):
        data = {"ApiSecret": "val", "userToken": "tok"}
        result = redactor.redact(data)
        assert result["ApiSecret"] == REDACTED_VALUE
        assert result["userToken"] == REDACTED_VALUE


class TestCustomPatterns:
    def test_additional_patterns(self):
        redactor = RedactionFilter(patterns=["*ssn*", "*phone*"])
        data = {"ssn_number": "123-45-6789", "phone_home": "555-1234", "name": "Alice"}
        result = redactor.redact(data)
        assert result["ssn_number"] == REDACTED_VALUE
        assert result["phone_home"] == REDACTED_VALUE
        assert result["name"] == "Alice"

    def test_custom_plus_defaults(self):
        redactor = RedactionFilter(patterns=["*custom*"])
        data = {"password": "pass", "custom_field": "val", "safe": "ok"}
        result = redactor.redact(data)
        assert result["password"] == REDACTED_VALUE
        assert result["custom_field"] == REDACTED_VALUE
        assert result["safe"] == "ok"


class TestEdgeCases:
    def test_none_input(self, redactor):
        assert redactor.redact(None) is None

    def test_string_input(self, redactor):
        assert redactor.redact("hello") == "hello"

    def test_int_input(self, redactor):
        assert redactor.redact(42) == 42

    def test_empty_dict(self, redactor):
        assert redactor.redact({}) == {}

    def test_empty_list(self, redactor):
        assert redactor.redact([]) == []
