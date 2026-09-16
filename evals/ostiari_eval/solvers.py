"""Inspect AI solvers for calling the governed Ostiari model endpoint."""

from __future__ import annotations

import os
import re

from inspect_ai.solver import Generate, Solver, TaskState, solver


def _header_value(value: object, *, maximum: int = 128) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-")
    return (text or "unknown")[:maximum]


@solver
def ostiari_generate(
    *,
    agent_id: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1_000,
    max_retries: int = 2,
    attempt_timeout: int = 120,
) -> Solver:
    """Generate through Ostiari while attaching stable governance identity headers."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        resolved_agent = agent_id or os.getenv("OSTIARI_EVAL_AGENT_ID", "model-eval")
        sample_id = _header_value(state.sample_id)
        headers = {
            "X-Agent-Id": _header_value(resolved_agent, maximum=64),
            "X-Framework": "inspect-ai",
            "X-Session-Id": f"inspect-{sample_id}-e{state.epoch}"[:128],
            "X-Plan": "offline-model-evaluation",
            "X-Step": sample_id,
        }
        return await generate(
            state,
            tool_calls="none",
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=max_retries,
            attempt_timeout=attempt_timeout,
            extra_headers=headers,
        )

    return solve
