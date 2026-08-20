"""Ensemble routing strategy helpers (scatter-gather-synthesize pattern).

This module provides :class:`EnsembleStrategy`, a stateless collection of pure
helper functions used by ``Router.ensemble_route()`` to orchestrate the
scatter-gather-synthesize flow:

- synthesis prompt construction for the judge model,
- quorum evaluation,
- survivor ranking for the best-single fallback policy,
- cost-multiplier estimation, and
- preset validation.

The helpers perform no I/O and make no provider calls. The ``Router`` performs
the actual scatter/gather/synthesize using these helpers.
"""

from .models import EnsemblePreset, PanelMemberResult

# --- Module constants ---

DEFAULT_QUORUM = 1
DEFAULT_FALLBACK_POLICY = "error"
PER_MEMBER_TIMEOUT_SECONDS = 60.0


class EnsembleConfigError(Exception):
    """Raised when an ensemble preset is structurally invalid."""


def _extract_content(result: PanelMemberResult) -> str:
    """Return the completion text for a survivor result, or an empty string.

    The text lives at ``response.choices[0]["message"]["content"]`` in the
    OpenAI-compatible :class:`ChatCompletionResponse` structure. Missing or
    malformed structures degrade gracefully to an empty string so ranking and
    prompt construction never raise on unexpected provider output.
    """
    response = result.response
    if response is None:
        return ""
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


class EnsembleStrategy:
    """Stateless helpers for ensemble orchestration.

    Pure functions only — no I/O, no provider calls. The Router performs the
    actual scatter/gather/synthesize using these helpers.
    """

    DEFAULT_QUORUM = DEFAULT_QUORUM
    DEFAULT_FALLBACK_POLICY = DEFAULT_FALLBACK_POLICY
    PER_MEMBER_TIMEOUT_SECONDS = PER_MEMBER_TIMEOUT_SECONDS

    @staticmethod
    def validate_preset(preset: EnsemblePreset) -> None:
        """Validate a preset, raising :class:`EnsembleConfigError` on violation.

        Enforces:
        - ``name``: 1..128 chars
        - ``panel``: 1..10 model identifiers
        - ``quorum``: 1..len(panel)
        - ``fallback_policy`` in {"best-single", "error"}
        """
        if not (1 <= len(preset.name) <= 128):
            raise EnsembleConfigError(f"preset name length invalid: {preset.name!r}")
        if not (1 <= len(preset.panel) <= 10):
            raise EnsembleConfigError(
                f"preset '{preset.name}' panel size {len(preset.panel)} "
                f"out of range 1..10"
            )
        if not (1 <= preset.quorum <= len(preset.panel)):
            raise EnsembleConfigError(
                f"preset '{preset.name}' quorum {preset.quorum} out of range "
                f"1..{len(preset.panel)}"
            )
        if preset.fallback_policy not in ("best-single", "error"):
            raise EnsembleConfigError(
                f"preset '{preset.name}' invalid fallback_policy "
                f"{preset.fallback_policy!r}"
            )

    @staticmethod
    def evaluate_quorum(survivor_count: int, quorum: int) -> bool:
        """Return ``True`` iff ``survivor_count >= quorum``."""
        return survivor_count >= quorum

    @staticmethod
    def estimate_cost_multiplier(panel_size: int) -> float:
        """Estimated cost factor vs a single call: N panel + 1 judge."""
        return float(panel_size + 1)

    @staticmethod
    def rank_survivors(
        survivors: list[PanelMemberResult], criteria: str = "length"
    ) -> list[PanelMemberResult]:
        """Return survivors ordered best-first per the ranking criteria.

        The default "length" criteria ranks by completion length (a proxy for
        completeness) in descending order. Ranking is deterministic: ties are
        broken stably by the survivor's original panel order, preserving model
        ordering for equal-length completions.
        """
        # enumerate() preserves the original panel order as a stable tie-breaker
        # since sorted() is stable: equal keys keep their input ordering.
        indexed = list(enumerate(survivors))
        if criteria == "length":
            indexed.sort(key=lambda pair: (-len(_extract_content(pair[1])), pair[0]))
        else:
            # Unknown criteria: preserve panel order deterministically.
            indexed.sort(key=lambda pair: pair[0])
        return [result for _, result in indexed]

    @staticmethod
    def build_synthesis_prompt(
        survivors: list[PanelMemberResult],
        original_prompt: str,
        criteria: str = "length",
    ) -> list[dict]:
        """Build the judge's message list.

        Survivor responses are templated as labeled blocks::

            [Response 1 — <model>]:
            <content>

            [Response 2 — <model>]:
            <content>

        The judge is instructed to identify consensus, contradictions, gaps, and
        unique insights, then produce exactly one final answer grounded only in
        the survivor content.
        """
        ranked = EnsembleStrategy.rank_survivors(survivors, criteria)

        blocks: list[str] = []
        for index, survivor in enumerate(ranked, start=1):
            content = _extract_content(survivor)
            blocks.append(f"[Response {index} — {survivor.model}]:\n{content}")
        candidate_block = "\n\n".join(blocks)

        system_message = (
            "You are a synthesis judge. You are given the original user request "
            "and N independent candidate responses from different models. Do NOT "
            "introduce facts that are not supported by at least one candidate "
            "response."
        )

        user_message = (
            f"Original request:\n{original_prompt}\n\n"
            f"Candidate responses:\n{candidate_block}\n\n"
            "Your task:\n"
            "1. CONSENSUS: what do the candidates agree on?\n"
            "2. CONTRADICTIONS: where do they disagree?\n"
            "3. GAPS: what does the request ask that no candidate addresses?\n"
            "4. UNIQUE INSIGHTS: valuable points raised by only one candidate.\n"
            "Then write a single, final, grounded answer to the original request "
            "that reflects only content present in the candidate responses."
        )

        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]
