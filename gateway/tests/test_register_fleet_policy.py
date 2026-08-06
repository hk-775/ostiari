"""Every destructive tool the fleet seeder registers must be governed.

`devops-agent` carried `"policy": None` on a comment claiming a `devops-strict`
policy already blocked `github.delete_repo`. Nothing in the repo ever created one
— grep found a single hit, the comment itself — so a fresh demo left that tool
callable. A governance product shipping an ungoverned destructive tool is the
worst possible demo bug: it undercuts the thing being demonstrated.

The specific fix is one entry in FLEET. The test worth having is the invariant, so
the next role added to the fleet can't reintroduce it: for each gateway, every
tool whose name suggests a destructive verb must be matched by that gateway's own
block patterns. `_BLOCK` is shared, but the *policy* is per gateway — a policy on
ops-agent does not govern devops-agent.
"""

import fnmatch
import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "register_fleet_tools.py"

# Verbs that mark a tool as destructive. Substring matching, not fnmatch: this is
# how the *test* spots a tool needing governance, independent of how the policy
# matches it — otherwise a too-narrow pattern would excuse itself.
_DESTRUCTIVE_VERBS = ("delete", "drop", "destroy", "remove", "purge", "wipe", "truncate")


def _load():
    """Import the script by path — it lives beside the package, not inside it."""
    spec = importlib.util.spec_from_file_location("register_fleet_tools", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["register_fleet_tools"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


def _destructive(spec) -> list[str]:
    return [name for name, _path, _desc in spec["tools"]
            if any(v in name.lower() for v in _DESTRUCTIVE_VERBS)]


class TestEveryDestructiveToolIsGoverned:
    def test_no_gateway_registers_an_unguarded_destructive_tool(self, mod):
        """The regression, stated as the invariant rather than as one gateway."""
        ungoverned = []
        for gw, spec in mod.FLEET.items():
            destructive = _destructive(spec)
            if not destructive:
                continue
            patterns = (spec.get("policy") or {}).get("block") or []
            for name in destructive:
                if not any(fnmatch.fnmatch(name, p) for p in patterns):
                    ungoverned.append(f"{gw}:{name}")
        assert not ungoverned, (
            "destructive tools registered with no policy blocking them: "
            + ", ".join(ungoverned)
        )

    def test_devops_agent_specifically(self, mod):
        """The one that was broken. Named explicitly so a failure reads clearly."""
        spec = mod.FLEET["devops-agent"]
        assert "github.delete_repo" in _destructive(spec)
        patterns = (spec.get("policy") or {}).get("block") or []
        assert any(fnmatch.fnmatch("github.delete_repo", p) for p in patterns)

    def test_a_policy_on_one_gateway_does_not_count_for_another(self, mod):
        """Policies are created gateway-scoped (`gateway_id` on the POST), so the
        assertion above must read each gateway's *own* policy. Pin that the specs
        don't share a policy object by reference."""
        policies = [id(spec["policy"]) for spec in mod.FLEET.values() if spec.get("policy")]
        assert len(policies) == len(set(policies)), "two gateways share one policy dict"

    def test_policy_names_are_distinct_per_gateway(self, mod):
        """_ensure_policy matches on (name, gateway_id), so duplicate names across
        gateways would still create separate rows — but distinct names keep the
        Policies page readable and make a stray row obvious."""
        names = [spec["policy"]["name"] for spec in mod.FLEET.values() if spec.get("policy")]
        assert len(names) == len(set(names))


class TestBlockPatterns:
    def test_block_catches_the_dotless_name(self, mod):
        """The recurring fnmatch trap: '*.delete' matches 'github.delete' but not
        'db_delete' and not 'github.delete_repo'. '*delete*' catches all three."""
        for name in ("db_delete", "github.delete_repo", "github.delete"):
            assert any(fnmatch.fnmatch(name, p) for p in mod._BLOCK), name

    def test_block_does_not_catch_read_tools(self, mod):
        """An over-broad block list would make the demo look broken instead of
        governed — every allowed tool returning 403 reads as an outage."""
        for name in ("db_query", "web_search", "github.search_code",
                     "github.create_issue", "send_email", "slack.post",
                     "drawio.create_diagram"):
            assert not any(fnmatch.fnmatch(name, p) for p in mod._BLOCK), name


class TestPolicyShape:
    def test_allow_lists_only_name_registered_tools(self, mod):
        """An allow entry for a tool the gateway doesn't have is dead config, and
        usually means a tool was renamed without updating the policy."""
        for gw, spec in mod.FLEET.items():
            policy = spec.get("policy")
            if not policy:
                continue
            registered = {name for name, _p, _d in spec["tools"]}
            unknown = set(policy.get("allow", [])) - registered
            assert not unknown, f"{gw} allows tools it doesn't register: {unknown}"

    def test_allow_and_block_do_not_overlap(self, mod):
        """Block wins in the engine (it checks block rules before allow), so an
        overlap isn't a security hole — but it is a contradiction, and relying on
        evaluation order to resolve it is how the intent gets lost."""
        for gw, spec in mod.FLEET.items():
            policy = spec.get("policy")
            if not policy:
                continue
            for name in policy.get("allow", []):
                assert not any(fnmatch.fnmatch(name, p) for p in policy["block"]), (
                    f"{gw} both allows and blocks {name}"
                )

    def test_gateways_without_destructive_tools_may_skip_a_policy(self, mod):
        """analytics-agent legitimately has `policy: None`. The invariant is about
        destructive tools, not about every gateway carrying a policy — pin that so
        nobody 'fixes' it by adding an empty one."""
        spec = mod.FLEET["analytics-agent"]
        assert _destructive(spec) == []
        assert spec["policy"] is None
