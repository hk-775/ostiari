from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "evals" / "datasets"


def _records(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (DATASETS / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_evaluation_datasets_have_stable_unique_samples() -> None:
    seen: set[str] = set()
    for name in ("contract.jsonl", "quality.jsonl", "safety.jsonl"):
        records = _records(name)
        assert records, f"{name} must not be empty"
        for record in records:
            assert set(record) == {"id", "input", "target", "metadata"}
            assert record["id"] not in seen
            seen.add(record["id"])
            assert isinstance(record["input"], str) and record["input"].strip()
            assert isinstance(record["target"], str) and record["target"].strip()
            assert isinstance(record["metadata"].get("category"), str)


def test_contract_targets_are_json_objects() -> None:
    for record in _records("contract.jsonl"):
        target = json.loads(record["target"])
        assert isinstance(target, dict)
        assert target["decision"] in {"allow", "refuse", "clarify"}


def test_contract_matchers_accept_subset_and_text_operators() -> None:
    pytest.importorskip("inspect_ai")
    from ostiari_eval.scorers import contract_mismatches

    expected = {
        "decision": "allow",
        "tool": "web_search",
        "arguments": {"query": {"$contains": ["lambda", "cold start"]}},
    }
    actual = {
        "decision": "allow",
        "tool": "web_search",
        "arguments": {"query": "AWS Lambda cold-start guidance", "locale": "en-US"},
        "reason": "Public research request",
    }
    assert contract_mismatches(expected, actual) == []


def test_contract_matchers_report_precise_paths() -> None:
    pytest.importorskip("inspect_ai")
    from ostiari_eval.scorers import contract_mismatches

    mismatches = contract_mismatches(
        {"decision": "refuse", "tool": None},
        {"decision": "allow", "tool": "db_delete"},
    )
    assert "$.decision" in mismatches[0]
    assert "$.tool" in mismatches[1]


def test_contract_parser_accepts_json_fences_but_rejects_prose() -> None:
    pytest.importorskip("inspect_ai")
    from ostiari_eval.scorers import parse_json_object

    assert parse_json_object('```json\n{"decision":"allow"}\n```') == {
        "decision": "allow"
    }
    with pytest.raises(json.JSONDecodeError):
        parse_json_object('Here is the answer: {"decision":"allow"}')


def test_task_factories_load_all_datasets() -> None:
    pytest.importorskip("inspect_ai")
    from evals.tasks import ostiari_contract, ostiari_quality, ostiari_safety

    contract = ostiari_contract()
    quality = ostiari_quality(grader_model="mockllm/grader")
    safety = ostiari_safety(grader_model="mockllm/grader")

    assert len(contract.dataset) == len(_records("contract.jsonl"))
    assert len(quality.dataset) == len(_records("quality.jsonl"))
    assert len(safety.dataset) == len(_records("safety.jsonl"))


def test_judged_tasks_require_an_explicit_grader() -> None:
    pytest.importorskip("inspect_ai")
    from evals.tasks import ostiari_quality

    with pytest.raises(ValueError, match="grader_model is required"):
        ostiari_quality(grader_model="")
