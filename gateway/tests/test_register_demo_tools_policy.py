"""Tests for register_demo_tools._fix_block_policy.

The Scenarios tab demonstrates the guard stopping destructive tool calls, which
only works if a policy actually blocks them. This function used to only *patch*
an existing policy, so the demo silently depended on a row added by hand in an
earlier session: recreate the database and crm-agent came up with an empty block
list, the destructive scenarios executed for real, and the Policies page showed
nothing for that gateway. These tests pin the create path and the two merge
hazards the function also guards against.
"""

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "register_demo_tools.py"


def _load():
    """Import the script by path — it lives beside the package, not inside it."""
    spec = importlib.util.spec_from_file_location("register_demo_tools", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["register_demo_tools"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


class FakeCP:
    """Records the calls _fix_block_policy makes and serves a policy list."""

    def __init__(self, policies):
        self.policies = policies
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []

    def req(self, method, url, body=None):
        if method == "GET" and url.endswith("/api/policies"):
            return 200, self.policies
        if method == "POST" and url.endswith("/api/policies"):
            self.posts.append((url, body))
            return 200, {"id": 99, **body}
        if method == "PATCH":
            self.patches.append((url, body))
            return 200, {}
        raise AssertionError(f"unexpected {method} {url}")


def _install(mod, cp):
    mod._req = cp.req  # type: ignore[assignment]


class TestBlockPolicyCreate:
    def test_creates_the_policy_when_none_exists(self, mod, capsys):
        """The regression: an empty policy list must produce a POST, not a no-op.
        Without it the destructive scenarios execute for real."""
        cp = FakeCP([])
        _install(mod, cp)
        mod._fix_block_policy()

        assert len(cp.posts) == 1, "no policy was created"
        _url, body = cp.posts[0]
        assert body["name"] == "block-destructive"
        assert body["gateway_id"] == mod.GATEWAY_ID
        assert body["content"]["block"] == mod.BLOCK_PATTERNS

    def test_created_patterns_match_the_destructive_tool_names(self, mod):
        """fnmatch is the matcher, and '*.delete' does not match 'db_delete' —
        the original bug. Every destructive tool the script registers must be
        matched by at least one pattern, or the guard demo shows nothing."""
        import fnmatch

        destructive = [t["name"] for t in mod.TOOLS
                       if any(k in t["name"] for k in ("delete", "drop", "destroy"))]
        assert destructive, "no destructive tools registered — demo can't show a block"
        for name in destructive:
            assert any(fnmatch.fnmatch(name, pat) for pat in mod.BLOCK_PATTERNS), (
                f"{name} is registered but no BLOCK_PATTERN matches it"
            )

    def test_does_not_create_a_duplicate_when_one_exists(self, mod):
        cp = FakeCP([{
            "id": 1, "name": "block-destructive", "gateway_id": mod.GATEWAY_ID,
            "content": {"block": mod.BLOCK_PATTERNS},
        }])
        _install(mod, cp)
        mod._fix_block_policy()
        assert cp.posts == []
        assert cp.patches == [], "content already correct — nothing to patch"

    def test_ignores_another_gateways_policy_of_the_same_name(self, mod):
        """A block-destructive on ops-agent must not satisfy crm-agent's need —
        policies are pushed per gateway, so crm would end up unguarded."""
        cp = FakeCP([{
            "id": 1, "name": "block-destructive", "gateway_id": "ops-agent",
            "content": {"block": mod.BLOCK_PATTERNS},
        }])
        _install(mod, cp)
        mod._fix_block_policy()
        assert len(cp.posts) == 1
        assert cp.posts[0][1]["gateway_id"] == mod.GATEWAY_ID

    def test_repairs_stale_patterns_on_an_existing_policy(self, mod):
        """The pre-existing bug: '*.delete' never matches 'db_delete'."""
        cp = FakeCP([{
            "id": 7, "name": "block-destructive", "gateway_id": mod.GATEWAY_ID,
            "content": {"block": ["*.delete"]},
        }])
        _install(mod, cp)
        mod._fix_block_policy()
        assert cp.posts == []
        assert len(cp.patches) == 1
        url, body = cp.patches[0]
        assert url.endswith("/api/policies/7")
        assert body["content"]["block"] == mod.BLOCK_PATTERNS

    def test_strips_an_empty_block_list_that_would_clobber_the_merge(self, mod):
        """The control plane merges policies with dict.update, so another active
        crm-agent policy carrying block: [] wipes the real block list."""
        cp = FakeCP([
            {"id": 1, "name": "block-destructive", "gateway_id": mod.GATEWAY_ID,
             "content": {"block": mod.BLOCK_PATTERNS}},
            {"id": 2, "name": "allow-reads", "gateway_id": mod.GATEWAY_ID,
             "content": {"block": [], "allow": ["db_query"]}},
        ])
        _install(mod, cp)
        mod._fix_block_policy()

        assert len(cp.patches) == 1
        url, body = cp.patches[0]
        assert url.endswith("/api/policies/2")
        assert "block" not in body["content"], "empty block list still present"
        assert body["content"]["allow"] == ["db_query"], "allow list must survive"

    def test_creates_and_repairs_in_the_same_run(self, mod):
        """A fresh DB plus a leftover clobbering policy: both paths must fire."""
        cp = FakeCP([
            {"id": 5, "name": "allow-reads", "gateway_id": mod.GATEWAY_ID,
             "content": {"block": []}},
        ])
        _install(mod, cp)
        mod._fix_block_policy()
        assert len(cp.posts) == 1 and len(cp.patches) == 1

    def test_a_read_failure_is_survivable(self, mod, capsys):
        """The control plane being unreachable must not raise — this runs inside
        `make demo-full`, where an exception would abort the remaining seeders."""
        class Broken(FakeCP):
            def req(self, method, url, body=None):
                return 500, None

        _install(mod, Broken([]))
        mod._fix_block_policy()          # must not raise
        assert "could not read policies" in capsys.readouterr().out
