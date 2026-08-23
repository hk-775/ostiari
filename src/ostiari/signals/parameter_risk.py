"""Parameter-aware risk signal.

The policy engine scores by *action name* (`db_delete` → +N). But the real risk
lives in the *arguments*: `db_delete WHERE id=5` vs. `db_delete WHERE 1=1` are
the same action with wildly different blast radius. This SignalProvider inspects
the params and contributes risk based on what the call actually does:

  - blast radius   — unbounded scope, wildcards, "all", huge counts
  - target sensitivity — production, external recipients, privileged targets
  - boundary crossing  — process calls that request host/sandbox escape semantics
  - control-plane pivot — service calls aimed at management/control infrastructure
  - destructiveness    — verbs that destroy/exfiltrate

It returns a single RiskSignal whose contribution is the sum of matched
heuristics (capped), with a description listing why. Heuristics are
conservative and explainable — this is a signal that *raises* risk on dangerous
parameters, never lowers it.
"""

from __future__ import annotations

import re
from typing import Any

from ostiari.models import EvalContext, RiskSignal

# ── Heuristics: (points, why) ────────────────────────────────────────────────

# Unbounded / mass-scope markers in SQL-ish or bulk params.
_UNBOUNDED_SQL = re.compile(r"\bwhere\s+1\s*=\s*1\b|\bdelete\b(?![\s\S]*\bwhere\b)|\btruncate\b|\bdrop\s+table\b", re.I)
_WILDCARD = re.compile(r"[\"']?\*[\"']?|/\*|;\s*--")
_MASS_WORDS = re.compile(r"\ball\b|\beverything\b|\bevery\b|\bentire\b|\b\*\b", re.I)

# Sensitive targets.
_PROD = re.compile(r"\bprod(uction)?\b|\blive\b", re.I)
_PRIVILEGED = re.compile(r"\broot\b|\badmin\b|\bsuperuser\b|\bsecret\b|\bcredential\b|\bpassword\b|\bprivate[_-]?key\b", re.I)
_CONTROL_PLANE = re.compile(
    r"\b(?:control[-_\s]?plane|management[-_\s]?plane|orchestrator|"
    r"package[-_\s]?control|admin(?:istration)?[-_\s]?service)\b",
    re.I,
)

# Strong semantic markers for process and service boundary changes. These
# inspect declared fields only; Ostiari never executes or probes the target.
_PROCESS_ACTION = re.compile(
    r"(?:^|[._-])(?:exec(?:ute)?|run|shell|process|command)(?:$|[._-])",
    re.I,
)
_SERVICE_ACTION = re.compile(
    r"(?:^|[._-])(?:service|api|http|request|invoke|call)(?:$|[._-])",
    re.I,
)
_BOUNDARY_OVERRIDE_FIELDS = {
    "escape_sequence",
    "sandbox_escape",
    "container_escape",
    "namespace_escape",
    "host_boundary",
    "host_mount",
    "host_path",
}
_OUTSIDE_BOUNDARY = re.compile(
    r"\b(?:host|outside|parent|privileged|unconfined)\b",
    re.I,
)

# Destructive verbs (belt-and-suspenders with policy; params may carry the verb).
_DESTRUCTIVE = re.compile(r"\bdelete\b|\bdrop\b|\bdestroy\b|\bpurge\b|\bwipe\b|\bterminate\b|\brm\s+-rf\b", re.I)

# Fields that commonly carry recipients / counts / targets.
_COUNT_FIELDS = ("count", "limit", "batch_size", "quantity", "n", "rows")
_RECIPIENT_FIELDS = ("to", "recipient", "recipients", "cc", "bcc", "email")
_TARGET_FIELDS = ("table", "target", "path", "bucket", "repo", "database", "db", "resource", "environment", "env")

_INTERNAL_DOMAINS_DEFAULT = ("example.com", "internal", "localhost", "corp")

# Point values (tunable).
P_UNBOUNDED = 45
P_MASS_WORD = 20
P_WILDCARD = 20
P_PROD = 25
P_PRIVILEGED = 30
P_CONTROL_PLANE = 45
P_BOUNDARY_ESCAPE = 80
P_DESTRUCTIVE = 15
P_HIGH_COUNT = 25          # count above _HIGH_COUNT_THRESHOLD
P_EXTERNAL_RECIPIENT = 20
_HIGH_COUNT_THRESHOLD = 1000
_CAP = 80                  # a single signal shouldn't monopolize the 0-100 scale


def _flatten(params: dict[str, Any]) -> str:
    """Best-effort string view of all param values for pattern matching."""
    parts: list[str] = []
    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        else:
            parts.append(str(v))
    walk(params)
    return " ".join(parts)


class ParameterRiskSignal:
    """SignalProvider that scores risk from the request's parameters."""

    name = "parameter-risk"

    def __init__(self, internal_domains: tuple[str, ...] = _INTERNAL_DOMAINS_DEFAULT) -> None:
        self._internal = internal_domains

    def evaluate(
        self, action: str, params: dict[str, Any], context: EvalContext
    ) -> RiskSignal | None:
        if not params:
            return None

        blob = _flatten(params)
        points = 0
        reasons: list[str] = []

        # ── blast radius ──
        unbounded = bool(_UNBOUNDED_SQL.search(blob))
        wildcard = bool(_WILDCARD.search(blob))
        mass_word = bool(_MASS_WORDS.search(blob))
        if unbounded:
            points += P_UNBOUNDED
            reasons.append("unbounded operation (no WHERE / matches all rows)")
        if wildcard:
            points += P_WILDCARD
            reasons.append("wildcard / mass selector in parameters")
        if mass_word:
            points += P_MASS_WORD
            reasons.append("mass-scope keyword ('all'/'every'/'entire')")

        # numeric counts
        for field in _COUNT_FIELDS:
            val = params.get(field)
            if isinstance(val, (int, float)) and val >= _HIGH_COUNT_THRESHOLD:
                points += P_HIGH_COUNT
                reasons.append(f"high volume ({field}={int(val)})")
                break

        # ── target sensitivity ──
        target_blob = " ".join(
            str(params.get(f, "")) for f in _TARGET_FIELDS if params.get(f)
        ) or blob
        if _PROD.search(target_blob):
            points += P_PROD
            reasons.append("targets production/live")
        if _PRIVILEGED.search(blob):
            points += P_PRIVILEGED
            reasons.append("touches privileged/secret target")
        if _SERVICE_ACTION.search(action) and _CONTROL_PLANE.search(target_blob):
            points += P_CONTROL_PLANE
            reasons.append("targets infrastructure control-plane service")

        boundary_override = any(
            field in params and params[field] not in (None, False, "")
            for field in _BOUNDARY_OVERRIDE_FIELDS
        )
        boundary_value = str(params.get("boundary", ""))
        if _PROCESS_ACTION.search(action) and (
            boundary_override or _OUTSIDE_BOUNDARY.search(boundary_value)
        ):
            points += P_BOUNDARY_ESCAPE
            reasons.append("requests execution outside the assigned boundary")

        # external recipients (email/exfil risk)
        for field in _RECIPIENT_FIELDS:
            val = params.get(field)
            if val:
                addrs = val if isinstance(val, (list, tuple)) else [val]
                for a in addrs:
                    s = str(a)
                    if "@" in s and not any(d in s for d in self._internal):
                        points += P_EXTERNAL_RECIPIENT
                        reasons.append("recipient is an external address")
                        break
                break

        # ── destructiveness ──
        # A destructive verb only ADDS risk when paired with a scope danger
        # (unbounded/mass/wildcard). A scoped destructive op (DELETE ... WHERE
        # id=5) is normal and must NOT be flagged — that's the whole point of
        # being parameter-aware rather than action-name-aware.
        if _DESTRUCTIVE.search(blob) and (unbounded or wildcard or mass_word):
            points += P_DESTRUCTIVE
            reasons.append("destructive verb at unbounded/mass scope")

        if points <= 0:
            return None

        points = min(points, _CAP)
        return RiskSignal(
            source=self.name,
            score_contribution=points,
            description="Parameter risk: " + "; ".join(reasons),
            metadata={"reasons": reasons, "raw_points": points},
        )
