"""Task classifier for smart routing — classifies prompts by task type."""

from __future__ import annotations

import re

from src.gateway.models import ClassificationResult

# Matches a genuine arithmetic expression: two numbers joined by an operator.
# Strong operators (+, *, =, ^) may be written without spaces ("2+2", "x=5");
# ambiguous prose punctuation (-, /) must be space-delimited ("10 / 2") so that
# hyphenated words ("year-over-year"), ranges ("3-5%") and dates ("2023-2024")
# do NOT get misread as math.
_ARITHMETIC_RE = re.compile(r"\d\s*[+*=^]\s*\d|\d\s+[-/]\s+\d")

# Postfix factorial ("4!", "what is 10!"). The operand must be adjacent to the
# "!" — an exclamation mark after a word is prose ("I have 3 cats!" does not
# match, since "!" there follows "s"). "!=" and "!!" are excluded: the first is
# the not-equal operator in most languages, the second is emphasis.
_FACTORIAL_RE = re.compile(r"\d!(?![=!])")

# Function-call notation with a numeric argument: "sqrt(16)", "log(100)",
# "sin(pi/2)". Two deliberate restrictions keep this off prose and code:
#
# * The name must be followed by "(", because keyword matching here is
#   substring-based — a bare "ln" would fire inside "explain" and "mod" inside
#   "model", breaking the reasoning and general cases.
# * The argument must start with a number or a math constant, which is what
#   separates the genuinely ambiguous names from their programming senses:
#   log("starting server") is a logging call, log(100) is a logarithm.
#
# Programming builtins that happen to be mathematical (abs, round, floor, ceil,
# pow) are left out entirely — "what does floor() do in Python" is a coding
# question, and the numeric-argument rule alone would not tell the difference.
_MATH_FUNC_RE = re.compile(
    r"\b(sqrt|cbrt|log|log2|log10|ln|exp|sin|cos|tan|asin|acos|atan"
    r"|sinh|cosh|tanh|gcd|lcm|mod)\s*\(\s*(-?[\d.]|pi\b|e\b|tau\b)"
)

# "15% of 200", "15 percent of 200" — a percentage *applied to* a quantity.
# The trailing "of <number>" is what makes this a calculation rather than a
# statistic quoted in prose ("up 12% year-over-year" has no "of" and so does
# not match).
_PERCENT_OF_RE = re.compile(r"\d\s*(?:%|\s*percent)\s+of\s+\d")


class TaskClassifier:
    """Classifies prompts into task types using keyword/heuristic analysis."""

    TASK_KEYWORDS: dict[str, list[str]] = {
        "coding": [
            "code", "function", "bug", "implement", "class", "method", "api",
            "debug", "refactor", "syntax", "compile", "programming", "algorithm",
            "script", "regex", "query", "sql", "endpoint",
            # common languages / runtimes — strong coding signals
            "python", "javascript", "typescript", "java", "golang", "rust",
            "c++", "bash", "shell",
            "```",
        ],
        "reasoning": [
            "why", "explain", "reason", "logic", "analyze", "think", "deduce",
            "argument", "because", "therefore", "proof",
        ],
        "creative_writing": [
            "write", "story", "poem", "creative", "fiction", "narrative",
            "character", "dialogue", "essay", "blog",
        ],
        "summarization": [
            "summarize", "summary", "tldr", "brief", "condense", "key points",
            "overview", "recap",
        ],
        "math": [
            "calculate", "equation", "solve", "math", "formula", "integral",
            "derivative", "probability", "statistics", "algebra", "factorial",
            # Operations users spell out rather than write in notation. The
            # symbolic forms are handled by the heuristics below; these catch
            # "the square root of 144", which contains no operator at all.
            "square root", "cube root", "logarithm", "arithmetic",
            "multiply", "divide", "subtract", "modulo", "remainder",
            "permutation", "combination", "quadratic", "trigonometry",
        ],
    }

    VALID_TASK_TYPES = {"coding", "reasoning", "creative_writing", "summarization", "math", "general"}

    def __init__(self, custom_keywords: dict[str, list[str]] | None = None) -> None:
        """Initialize with default keywords, optionally extended."""
        self._keywords: dict[str, list[str]] = {
            k: list(v) for k, v in self.TASK_KEYWORDS.items()
        }
        if custom_keywords:
            for task_type, keywords in custom_keywords.items():
                existing = self._keywords.get(task_type, [])
                self._keywords[task_type] = existing + keywords

    def classify(self, prompt: str) -> ClassificationResult:
        """Classify a prompt into a task type with confidence score.

        Algorithm:
        1. Normalize prompt to lowercase
        2. For each task type, count keyword matches
        3. Apply structural heuristics (code blocks, "Write a", math operators)
        4. Return highest-scoring type, or "general" if no matches
        5. Confidence = best_score / (best_score + second_best_score + epsilon)
        """
        normalized = prompt.lower()

        # Score each task type by keyword matches
        scores: dict[str, float] = {}
        matched: dict[str, list[str]] = {}

        for task_type, keywords in self._keywords.items():
            matches = [kw for kw in keywords if kw in normalized]
            scores[task_type] = len(matches)
            matched[task_type] = matches

        # Apply structural heuristics
        self._apply_heuristics(prompt, normalized, scores, matched)

        # Find best and second-best scores
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        best_type = sorted_scores[0][0] if sorted_scores else "general"
        best_score = sorted_scores[0][1] if sorted_scores else 0.0
        second_best_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

        # If no keywords matched at all, return "general"
        if best_score == 0.0:
            return ClassificationResult(
                task_type="general",
                confidence=0.0,
                matched_keywords=[],
            )

        # Compute confidence
        epsilon = 1e-6
        confidence = best_score / (best_score + second_best_score + epsilon)
        # Clamp to [0.0, 1.0]
        confidence = max(0.0, min(1.0, confidence))

        return ClassificationResult(
            task_type=best_type,
            confidence=confidence,
            matched_keywords=matched.get(best_type, []),
        )

    def _apply_heuristics(
        self,
        original: str,
        normalized: str,
        scores: dict[str, float],
        matched: dict[str, list[str]],
    ) -> None:
        """Apply structural heuristics to boost scores."""
        # Triple backticks → boost coding
        if "```" in original:
            scores.setdefault("coding", 0.0)
            scores["coding"] += 2.0
            if "```" not in matched.get("coding", []):
                matched.setdefault("coding", []).append("```")

        # Starts with "Write a" / "Create a": ambiguous — "write a poem" is
        # creative, "write a function" is coding. Route the boost to coding when
        # the prompt already has any coding signal; otherwise creative_writing.
        stripped = original.strip()
        if stripped.lower().startswith("write a") or stripped.lower().startswith("create a"):
            if scores.get("coding", 0.0) > 0:
                scores["coding"] += 2.0
                matched.setdefault("coding", []).append("write_a_code_heuristic")
            else:
                scores.setdefault("creative_writing", 0.0)
                scores["creative_writing"] += 2.0
                matched.setdefault("creative_writing", []).append("write_a_heuristic")

        # Contains a genuine arithmetic expression (number-operator-number) →
        # boost math. Guards against prose that merely contains digits and
        # punctuation (percentages, dates, ranges, hyphenated words).
        if _ARITHMETIC_RE.search(original):
            scores.setdefault("math", 0.0)
            scores["math"] += 1.5
            matched.setdefault("math", []).append("math_operators_heuristic")

        # Notation that is not infix binary, and so invisible to the rule above:
        # postfix factorial ("4!"), function calls ("sqrt(16)"), and a percentage
        # applied to a quantity ("15% of 200"). Each carries the same weight as
        # an arithmetic expression — they are equally unambiguous once matched.
        for pattern, label in (
            (_FACTORIAL_RE, "factorial_notation_heuristic"),
            (_MATH_FUNC_RE, "math_function_heuristic"),
            (_PERCENT_OF_RE, "percent_of_heuristic"),
        ):
            if pattern.search(normalized):
                scores.setdefault("math", 0.0)
                scores["math"] += 1.5
                matched.setdefault("math", []).append(label)
