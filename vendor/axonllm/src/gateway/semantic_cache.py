"""Semantic response cache — serve a cached answer to a *reworded* question.

:mod:`cache_manager` keys on a SHA-256 of the request, so "What is our refund
policy?" and "what's the refund policy?" are different keys and both hit the
provider. This module adds a second lookup, tried only after that exact key
misses: embed the prompt, compare against the embeddings of recent cached
prompts, and reuse the response when one is close enough.

The risk this carries and the exact cache does not
-------------------------------------------------
An exact cache can only ever return the answer to the question that was asked.
A semantic cache can return the answer to a *different* question, and the
failure is silent — the caller gets a confident, well-formed, wrong answer with
no indication it was substituted. "What is 17 * 23?" and "What is 17 * 24?" are
one character apart and near-identical to any embedding model; so are "revenue
in Q1" and "revenue in Q2", or "how do I enable X" and "how do I disable X".

So the design is deliberately reluctant:

* **Off unless asked for.** Per-project, defaulting to disabled.
* **A high threshold.** 0.90 cosine. Chosen for its distance from the
  highest-scoring *different*-question pair (0.7476 on the calibration set), not
  for a target hit rate — the cost of a false hit (a wrong answer) is much worse
  than a false miss (a normal API call). See DEFAULT_SIMILARITY_THRESHOLD for
  the measurements.
* **Literal tokens must agree.** Numbers, dates, quoted strings and code
  identifiers are compared exactly, whatever the embedding says. This is what
  stops 17*23 vs 17*24 — the semantic distance is tiny, but the numbers differ,
  so it is never a hit.
* **Polar opposites must not disagree.** Negations and antonym pairs are
  compared by *axis* — "enable" against "disable", "this week" against "next
  week" — rather than by which polar words each prompt happens to contain. A
  word appearing in one phrasing and not the other ("turn **on** logging" vs
  "**enable** logging") is not evidence of a different question, and treating it
  as such rejected almost every real paraphrase. See ``_POLAR_AXES``.
* **Whole classes of request are skipped.** Non-zero temperature (the caller
  asked for variety), tools (the answer is a side effect, not text), and
  streaming (a replayed stream is a different shape).

A cache that occasionally lies is worse than no cache, and the number of
requests it saves is not a defence.
"""

from __future__ import annotations

import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)


# Cosine similarity above which two prompts are treated as the same question.
#
# Calibrated against amazon.titan-embed-text-v2:0 on 16 hand-built pairs (8
# paraphrases of one question, 8 pairs of genuinely different questions), not
# taken from the ~0.85 usually quoted for "similar text". The measurement is
# worth recording because it contradicts the intuition:
#
#     paraphrases            0.47 - 0.98
#     different questions    0.09 - 0.60
#
# **The two ranges overlap.** "What are the office hours?" / "when is the office
# open?" scores 0.4734 — the same question, and lower than "Write a haiku about
# the ocean" / "...about the desert" at 0.6022, which are different questions
# with different answers. So no threshold separates them, and any choice trades
# hit rate against wrong answers rather than finding a clean line.
#
# Hits on the paraphrase set at each threshold, with the literal guard applied,
# and false hits on a 14-pair different-question set:
#
#     0.80  ->  4/8 hits, 0 false      0.95  ->  0/8 hits, 0 false
#     0.90  ->  2/8 hits, 0 false      0.98  ->  0/8 hits, 0 false
#
# 0.90 is chosen. It buys the two clearest paraphrases in the set —
#
#     0.9258  "What does our SLA guarantee for uptime?" / "What uptime does the
#             SLA promise?"
#     0.9185  "Explain how photosynthesis works" / "How does photosynthesis
#             work?"
#
# — which 0.95 rejected, and it clears the highest different-question pair the
# literal guard admits (0.7476) by 0.15. That margin matters more than the
# threshold: a low hit rate is a missed saving, a false hit is a confident wrong
# answer nobody is watching for.
#
# 0.80 doubles the hits with no false hits *on this sample*, and is still not
# taken: its extra two pairs score 0.8119 and 0.8468, close enough to the 0.7476
# ceiling that a single unseen phrasing could land between them. 14 pairs is not
# enough evidence to spend a 0.15 margin. Override per project if the workload
# is genuinely tolerant.
#
# One caveat this measurement earned the hard way: at 0.95 the guard's coverage
# was untested, because nothing scored high enough to reach it. Lowering to 0.90
# exposed "Who is the on-call engineer this week?" / "...next week?" at 0.9385 —
# a wrong answer that passed the guard, since this/next are not numbers and no
# axis covered them. Lowering a threshold makes the literal guard load-bearing
# where it previously was not, in both directions: the same change also revealed
# that the guard was rejecting every paraphrase it saw. See _POLAR_AXES.
DEFAULT_SIMILARITY_THRESHOLD = 0.90

# Per project. Small on purpose: every entry is compared on every lookup (a
# linear scan — see _best_match), so this bounds lookup work as well as memory.
DEFAULT_MAX_ENTRIES_PER_PROJECT = 500

# Prompts shorter than this are not cached semantically. Embeddings of very
# short strings are dominated by the few tokens present, so "yes" and "no" sit
# closer together than their meanings do.
MIN_PROMPT_CHARS = 16


# Tokens that must match exactly between two prompts, whatever their embeddings
# say. Each is a case where a tiny textual difference flips the correct answer.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`")
# Identifier-ish: has an underscore, a dot between letters, or internal caps.
# Deliberately not "any word" — that would make the whole check an exact match.
_IDENT_RE = re.compile(r"\b(?:\w+_\w+|\w+\.\w+|[a-z]+[A-Z]\w*)\b")
# Polar opposites, grouped into axes: (side A, side B).
#
# Two prompts are treated as different questions when they land on *opposite
# sides of the same axis* — "enable" against "disable", "this week" against
# "next week". A word appearing on one side with nothing opposing it on the
# other says nothing about whether the questions differ, so it does not block.
#
# This replaced a single flat set of polar words compared for equality, which
# conflated two different situations and was measurably wrong about the second:
#
#     "how do I disable X"        vs "how do I enable X"        -> different
#     "how do I turn on logging"  vs "how do I enable logging"  -> the SAME
#
# The flat set blocked both, because {on} != {enable}. On a 45-pair labelled
# corpus it allowed only 6 of 19 real paraphrases; measured against live Titan,
# all three paraphrases that scored above the 0.90 threshold — and so were the
# only ones the guard ever saw — were rejected. That silently cancelled out the
# hits that lowering the threshold to 0.90 had just bought.
#
# It also *missed* "what is included in the plan" / "what is excluded from the
# plan", because only the uninflected include/exclude were listed. Axes make the
# inflections part of the data rather than something to remember: a missing form
# now weakens one axis instead of leaving a lone word to compare unequal.
#
# Adding to this table is the safe direction. Adding a word to only one side is
# not: it makes that word block on presence alone for the axes below that treat
# a one-sided appearance as decisive.
_POLAR_AXES: tuple[tuple[str, frozenset[str], frozenset[str]], ...] = (
    # Negation is presence-vs-absence, not two sides: there is no word that
    # means "affirmatively yes" the way "not" means no, so any asymmetry counts.
    (
        "negation",
        frozenset({
            "not", "no", "never", "none", "without", "cannot", "cant", "dont",
            "doesnt", "isnt", "arent", "wasnt", "werent", "shouldnt",
            "wouldnt", "wont", "havent", "hasnt", "nothing", "neither", "nor",
        }),
        frozenset(),
    ),
    (
        "on_off",
        frozenset({"enable", "enabled", "enabling", "on", "allow", "allowed",
                   "true", "yes", "include", "included", "including",
                   "permit", "permitted", "grant", "granted"}),
        frozenset({"disable", "disabled", "disabling", "off", "deny", "denied",
                   "false", "exclude", "excluded", "excluding",
                   "forbid", "forbidden", "revoke", "revoked"}),
    ),
    (
        "lifecycle",
        frozenset({"add", "added", "create", "created", "creating", "start",
                   "started", "starting", "increase", "increased", "install",
                   "installed", "enroll", "subscribe"}),
        frozenset({"remove", "removed", "delete", "deleted", "deleting",
                   "destroy", "destroyed", "stop", "stopped", "stopping",
                   "decrease", "decreased", "uninstall", "cancel", "cancelled",
                   "unsubscribe"}),
    ),
    # Which one, in time. A one-sided appearance is decisive here: "the current
    # quota" and "the quota" can differ, where "start the job" and "start the
    # job now" do not — so `now` sits with the words that need an opposite.
    (
        "time_rel",
        frozenset({"this", "current", "currently", "today", "latest", "newest",
                   "recent"}),
        frozenset({"next", "last", "previous", "prior", "upcoming", "tomorrow",
                   "yesterday", "oldest", "earliest", "former"}),
    ),
    (
        "order",
        frozenset({"before", "preceding", "first", "initial"}),
        frozenset({"after", "following", "final", "subsequent"}),
    ),
    (
        "extremum",
        frozenset({"min", "minimum", "lowest", "least", "fewest", "smallest"}),
        frozenset({"max", "maximum", "highest", "most", "largest", "greatest"}),
    ),
    (
        "optionality",
        frozenset({"required", "mandatory", "compulsory"}),
        frozenset({"optional", "voluntary"}),
    ),
)

# Axes where a word on one side with nothing opposing it still blocks.
#
# The distinction is whether the word narrows *which* facts answer the question
# or merely colours how it is asked. "the current quota" vs "the quota" may well
# have different answers, so time_rel is decisive on its own. "start the job"
# vs "start the job now" is one question, so lifecycle is not — it needs to see
# an actual opposite before it blocks.
_ONE_SIDED_BLOCKS = frozenset({"negation", "time_rel", "extremum", "optionality"})

# Every word in the table, for the cheap pre-filter in _axis_sides.
_POLAR_WORDS = frozenset().union(
    *(a | b for _, a, b in _POLAR_AXES)
)


# (request model, prompt). See the comment on the key in put().
_EntryKey = tuple[str, str]
# Tenant and project form the authorization boundary. ``tenant_id`` is optional
# only for legacy single-tenant callers.
_BucketKey = tuple[str | None, str]


@dataclass
class SemanticCacheEntry:
    """One cached response plus what is needed to match against it."""

    prompt: str
    embedding: list[float]
    response: ChatCompletionResponse
    expires_at: datetime
    # The model the caller asked for is the cache and authorization namespace.
    # Do not infer it from a provider response: legacy/custom integrations may
    # return provider ids, and multiple gateway aliases can map to the same id.
    # This is also the field the exact-match cache keys on, so both caches agree
    # on what "same model" means.
    request_model: str = ""
    # Extracted once at insert. Recomputing per comparison would make every
    # lookup O(entries x prompt length) on top of the vector maths.
    literals: PromptLiterals = field(default_factory=lambda: PromptLiterals())


@dataclass
class SemanticCacheStats:
    """Counters for the admin surface.

    Kept here rather than derived from logs because the interesting number is
    the ratio, and ``skipped`` is what explains a hit rate of zero on a project
    that has caching switched on.
    """

    lookups: int = 0
    hits: int = 0
    misses: int = 0
    # Requests never eligible: streaming, tools, temperature, too short.
    skipped: int = 0
    # A candidate cleared the similarity threshold but disagreed on a literal.
    # Worth its own counter: a high value means the threshold is admitting
    # near-misses and the literal guard is the only thing catching them.
    rejected_by_literals: int = 0
    embed_failures: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        """Hits as a fraction of lookups that were actually attempted.

        Skipped requests are excluded from the denominator: they never consulted
        the cache, so counting them would report a cache that is working as one
        that is failing.
        """
        attempted = self.hits + self.misses
        return self.hits / attempted if attempted else 0.0

    def as_dict(self) -> dict:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "misses": self.misses,
            "skipped": self.skipped,
            "rejected_by_literals": self.rejected_by_literals,
            "embed_failures": self.embed_failures,
            "evictions": self.evictions,
            "hit_rate": round(self.hit_rate, 4),
        }


_WORD_RE = re.compile(r"[a-z']+")


@dataclass(frozen=True)
class PromptLiterals:
    """What must agree between two prompts for them to be the same question.

    Two kinds of evidence, compared differently, which is why this is a
    structure rather than the single set it used to be:

    ``exact``
        Numbers, quoted strings and code identifiers. Compared for equality —
        "17 * 23" and "17 * 24" are different questions, full stop.
    ``axes``
        Which side of each polar axis the prompt sits on, as ``(axis, side)``
        pairs. Compared for *opposition*, not equality: sharing no polar words
        at all is normal for two phrasings of one question.
    """

    exact: frozenset[str] = frozenset()
    axes: frozenset[tuple[str, str]] = frozenset()

    def conflict_with(self, other: PromptLiterals) -> str | None:
        """Why these two prompts are different questions, or None if they agree.

        Returns a short reason rather than a bool so the rejection log says
        which check fired — "negation" and "numbers" are very different
        diagnoses when someone is working out why a cache never hits.
        """
        if self.exact != other.exact:
            return "exact-tokens"

        # Sides per axis, not one side per axis: "how do I enable and disable
        # this" sits on both, and a dict keyed by axis would silently keep
        # whichever came last and compare the wrong one.
        mine = self._by_axis()
        theirs = other._by_axis()
        for axis in set(mine) | set(theirs):
            a = mine.get(axis, frozenset())
            b = theirs.get(axis, frozenset())
            if a == b:
                continue
            if a and b:
                # Both mention the axis but not identically: enable vs disable,
                # or "enable" vs "enable and disable".
                return axis
            if axis in _ONE_SIDED_BLOCKS:
                # One prompt mentions the axis and the other does not at all,
                # and this axis narrows which facts answer the question:
                # "the latest release" vs "the release".
                return axis
        return None

    def _by_axis(self) -> dict[str, frozenset[str]]:
        grouped: dict[str, set[str]] = {}
        for axis, side in self.axes:
            grouped.setdefault(axis, set()).add(side)
        return {k: frozenset(v) for k, v in grouped.items()}


def extract_literals(text: str) -> PromptLiterals:
    """Split a prompt into the evidence :meth:`PromptLiterals.conflict_with` uses.

    Deliberately not "every word" — that would make the guard an exact-match
    check and the embedding pointless. The point is to catch the specific cases
    where embeddings are blind, and leave everything else to the vector.
    """
    lowered = text.lower()
    exact: set[str] = set()

    exact.update(_NUMBER_RE.findall(lowered))
    for groups in _QUOTED_RE.findall(text):
        # findall over alternated groups yields a tuple with empties for the
        # branches that did not match.
        for g in groups:
            if g:
                exact.add(g.lower())
    exact.update(m.lower() for m in _IDENT_RE.findall(text))

    words = set(_WORD_RE.findall(lowered))
    axes: set[tuple[str, str]] = set()
    if words & _POLAR_WORDS:  # cheap pre-filter; most prompts have none
        for axis, side_a, side_b in _POLAR_AXES:
            if words & side_a:
                axes.add((axis, "A"))
            if words & side_b:
                axes.add((axis, "B"))

    return PromptLiterals(exact=frozenset(exact), axes=frozenset(axes))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity, or 0.0 when either vector is degenerate.

    Hand-rolled rather than numpy: numpy is not a dependency, and one function
    over a 1024-dim vector is not worth adding a compiled one for — this is fast
    enough next to the network call it avoids.
    """
    if len(a) != len(b):
        # Different embedding models, or a model change mid-process. Not
        # comparable; report no similarity rather than raising, so a config
        # change degrades to cache misses instead of failing requests.
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def is_cacheable(request: ChatCompletionRequest) -> tuple[bool, str]:
    """Whether a request may be served from, or stored in, the semantic cache.

    Returns ``(ok, reason)`` — the reason is for the skip counter and logs, so a
    project seeing no hits can find out why without a debugger.

    These are correctness limits, not optimisations. Each one is a case where
    reusing a response would return something the caller did not ask for.
    """
    if request.stream:
        # A cached response is a complete object; replaying it as a stream is
        # possible but changes timing and chunk boundaries that callers key off.
        return False, "streaming"
    if request.tools:
        # The value of a tool call is the side effect. Serving a cached one
        # would return a stale tool_use block the caller then acts on.
        return False, "tools"
    if request.system:
        # The system instruction governs the meaning and allowed shape of the
        # answer. Embedding only the user text could otherwise reuse a response
        # generated under a different system policy.
        return False, "system_instruction"
    if request.temperature is not None and request.temperature > 0:
        # A caller asking for sampling asked for variety. Returning a fixed
        # answer silently overrides that.
        return False, "temperature"
    # getattr, not request.n: ChatCompletionRequest has no ``n`` field today.
    # Guarded anyway so that adding one later cannot silently start serving one
    # cached completion to a caller who asked for several.
    n = getattr(request, "n", None)
    if n is not None and n > 1:
        return False, "multiple_completions"

    messages = request.messages or []
    if len(messages) != 1 or messages[0].get("role") != "user":
        return False, "conversation_context"

    prompt = last_user_text(request)
    if prompt is None:
        return False, "no_user_text"
    if len(prompt.strip()) < MIN_PROMPT_CHARS:
        return False, "prompt_too_short"
    return True, ""


def last_user_text(request: ChatCompletionRequest) -> str | None:
    """Plain text of the final user message, or None if there isn't any.

    Only the last user turn is embedded, not the whole conversation: the
    conversation is what makes two requests different even when the question is
    the same, and it is handled by keying the cache on the exact-match hash
    first. Multi-turn requests are excluded outright below.
    """
    messages = request.messages or []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Multimodal: concatenate the text parts, ignore images. A request
            # whose text matches but whose image differs must not hit, which is
            # why images make it uncacheable below rather than being skipped.
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            non_text = any(
                isinstance(p, dict) and p.get("type") not in ("text", None) for p in content
            )
            if non_text:
                return None
            joined = " ".join(t for t in parts if t)
            return joined or None
        return None
    return None


def conversation_depth(request: ChatCompletionRequest) -> int:
    """Number of user turns. Used to keep multi-turn requests out of the cache.

    Two conversations can end on the same question and require different
    answers, because the earlier turns changed what it refers to ("and the
    second one?"). Embedding only the final turn cannot see that, so anything
    past the first user message is left to the exact-match cache.
    """
    return sum(1 for m in (request.messages or []) if m.get("role") == "user")


class SemanticCache:
    """Per-tenant, per-project semantic response cache.

    Consulted only after :class:`CacheManager` misses, so a byte-identical
    repeat never pays for an embedding call.
    """

    def __init__(
        self,
        embedder=None,
        *,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_entries_per_project: int = DEFAULT_MAX_ENTRIES_PER_PROJECT,
    ) -> None:
        self._embedder = embedder
        self._threshold = similarity_threshold
        self._max_entries = max_entries_per_project
        # (tenant_id, project_id) -> prompt-keyed entries. Keyed by prompt so
        # the same question asked twice updates one entry instead of
        # accumulating duplicates that then compete in the scan.
        self._entries: dict[
            _BucketKey, OrderedDict[_EntryKey, SemanticCacheEntry]
        ] = {}
        self._stats_by_bucket: dict[_BucketKey, SemanticCacheStats] = {}
        # Aggregate compatibility for existing observability callers. Tenant
        # admin routes use ``stats_for_scope`` instead.
        self.stats = SemanticCacheStats()
        # One-slot memo of the most recent (prompt, embedding). A miss is
        # normally followed by a put of the same prompt, and embedding it twice
        # would double the cost of every miss — the common case.
        #
        # One slot, and reused only when the prompt matches exactly, so
        # concurrent requests can evict each other but can never pair a prompt
        # with another prompt's vector. The failure mode is a wasted re-embed,
        # not a wrong comparison.
        self._last_embedding: tuple[_BucketKey, str, list[float]] | None = None

    @property
    def enabled(self) -> bool:
        """False without an embedder, which makes every method a no-op.

        Separate from the per-project flag: this is "the gateway cannot embed"
        (no credentials, no Bedrock), where the per-project flag is "this
        project asked not to".
        """
        return self._embedder is not None

    @property
    def threshold(self) -> float:
        """The default threshold, for the admin surface to report.

        Worth exposing: a project with no override of its own is matching at
        this value, and an operator diagnosing an unexpected hit needs to know
        what it was compared against.
        """
        return self._threshold

    async def get(
        self,
        request: ChatCompletionRequest,
        project_id: str,
        threshold: float | None = None,
        tenant_id: str | None = None,
    ) -> ChatCompletionResponse | None:
        """Closest acceptable cached response, or None.

        ``threshold`` overrides the instance default for this lookup — that is
        how a project's own ``semantic_cache_threshold`` is honoured without a
        cache instance per project. None means "use the default"; 0.0 would mean
        "match everything", so the two cannot be collapsed.

        None covers every negative case — disabled, ineligible, no entries, no
        candidate above threshold, candidate rejected on literals, embedding
        failure. The caller then proceeds to the provider, which is correct for
        all of them.
        """
        bucket_key = (tenant_id, project_id)
        if not self.enabled:
            return None

        ok, reason = is_cacheable(request)
        if not ok or conversation_depth(request) > 1:
            self._increment_stat(bucket_key, "skipped")
            return None

        bucket = self._entries.get(bucket_key)
        if not bucket:
            # No entries yet: a miss, but do not spend an embedding call to
            # discover that. Counted as a miss rather than a skip because the
            # request was eligible — the cache was simply cold.
            self._increment_stat(bucket_key, "lookups")
            self._increment_stat(bucket_key, "misses")
            return None

        prompt = last_user_text(request)
        if prompt is None:  # pragma: no cover — is_cacheable already rejected it
            self._increment_stat(bucket_key, "skipped")
            return None

        self._increment_stat(bucket_key, "lookups")
        embedding = await self._embed(prompt, bucket_key)
        if embedding is None:
            self._increment_stat(bucket_key, "misses")
            return None

        self._purge_expired(project_id, tenant_id)

        match = self._best_match(
            bucket,
            prompt,
            embedding,
            request.model,
            self._threshold if threshold is None else threshold,
            bucket_key,
        )
        if match is None:
            self._increment_stat(bucket_key, "misses")
            return None

        key, entry, score = match
        bucket.move_to_end(key)
        self._increment_stat(bucket_key, "hits")
        logger.info(
            "semantic cache hit: tenant=%s project=%s similarity=%.4f "
            "cached_prompt=%r",
            tenant_id,
            project_id,
            score,
            entry.prompt[:80],
        )
        return entry.response

    async def put(
        self,
        request: ChatCompletionRequest,
        project_id: str,
        response: ChatCompletionResponse,
        ttl_seconds: int,
        tenant_id: str | None = None,
    ) -> None:
        """Store a response for future semantic lookups. Never raises.

        A failure here must not fail the request: the response has already been
        produced and is on its way to the caller, so the only thing a raise
        would achieve is turning a successful request into an error.
        """
        if not self.enabled:
            return
        ok, _ = is_cacheable(request)
        if not ok or conversation_depth(request) > 1:
            return

        prompt = last_user_text(request)
        if prompt is None:  # pragma: no cover — is_cacheable already rejected it
            return

        bucket_key = (tenant_id, project_id)
        embedding = await self._embed(prompt, bucket_key)
        if embedding is None:
            return

        bucket = self._entries.setdefault(bucket_key, OrderedDict())
        # Keyed on (model, prompt), not prompt alone: the same question asked of
        # two models is two answers, and a prompt-only key would make the second
        # overwrite the first — so a project routing between models would keep
        # losing whichever it asked less recently.
        key = (request.model or "", prompt)
        bucket[key] = SemanticCacheEntry(
            prompt=prompt,
            embedding=embedding,
            response=response,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
            literals=extract_literals(prompt),
            request_model=request.model or "",
        )
        bucket.move_to_end(key)
        while len(bucket) > self._max_entries:
            bucket.popitem(last=False)
            self._increment_stat(bucket_key, "evictions")

    def invalidate(
        self,
        project_id: str | None = None,
        tenant_id: str | None = None,
        *,
        all_tenants: bool = False,
    ) -> int:
        """Drop entries in one explicit scope.

        Exists because a semantic cache has no natural way to know its answers
        went stale — the underlying documents or prompts change with nothing
        observable in the request. An operator needs a way to clear it that
        does not involve a restart.

        Omitting ``tenant_id`` addresses only the legacy tenantless namespace;
        it is never a wildcard. Platform maintenance may opt into a cross-tenant
        operation with ``all_tenants=True``.
        """
        if all_tenants and tenant_id is not None:
            raise ValueError("tenant_id and all_tenants cannot be combined")
        if tenant_id is not None and project_id is None:
            raise ValueError("project_id is required when tenant_id is provided")

        matching = self._matching_bucket_keys(
            project_id,
            tenant_id,
            all_tenants=all_tenants,
        )
        removed = sum(len(self._entries[key]) for key in matching)
        for key in matching:
            del self._entries[key]
        return removed

    def entry_count(
        self,
        project_id: str | None = None,
        tenant_id: str | None = None,
        *,
        all_tenants: bool = False,
    ) -> int:
        """Count entries in one tenant scope, or all scopes when requested."""
        if all_tenants and tenant_id is not None:
            raise ValueError("tenant_id and all_tenants cannot be combined")
        if tenant_id is not None and project_id is None:
            raise ValueError("project_id is required when tenant_id is provided")
        return sum(
            len(self._entries[key])
            for key in self._matching_bucket_keys(
                project_id,
                tenant_id,
                all_tenants=all_tenants,
            )
        )

    def stats_for_scope(
        self,
        *,
        tenant_id: str | None,
        project_id: str | None = None,
        all_tenants: bool = False,
    ) -> SemanticCacheStats:
        """Return counters visible inside exactly one authorization scope."""
        if all_tenants and tenant_id is not None:
            raise ValueError("tenant_id and all_tenants cannot be combined")
        aggregate = SemanticCacheStats()
        for bucket_key, stats in self._stats_by_bucket.items():
            if not all_tenants and bucket_key[0] != tenant_id:
                continue
            if project_id is not None and bucket_key[1] != project_id:
                continue
            for field_name in (
                "lookups",
                "hits",
                "misses",
                "skipped",
                "rejected_by_literals",
                "embed_failures",
                "evictions",
            ):
                setattr(
                    aggregate,
                    field_name,
                    getattr(aggregate, field_name)
                    + getattr(stats, field_name),
                )
        return aggregate

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _best_match(
        self,
        bucket: OrderedDict[_EntryKey, SemanticCacheEntry],
        prompt: str,
        embedding: list[float],
        model: str | None,
        threshold: float,
        bucket_key: _BucketKey,
    ) -> tuple[str, SemanticCacheEntry, float] | None:
        """Highest-scoring entry that clears the threshold and the guards.

        A linear scan. With max_entries_per_project at 500 and 1024-dim vectors
        that is well under the latency of the provider call being avoided, and
        it keeps the whole cache dependency-free — an index would mean numpy or
        a vector store, neither of which is available (see cosine_similarity).
        """
        literals = extract_literals(prompt)
        best: tuple[str, SemanticCacheEntry, float] | None = None

        for key, entry in bucket.items():
            # A response from a different model is a different answer: models
            # differ in format, verbosity and quality, and a project routing to
            # one deliberately should not be served another's output.
            if model and entry.request_model and entry.request_model != model:
                continue

            score = cosine_similarity(embedding, entry.embedding)
            if score < threshold:
                continue

            # The guard that does the real work. Two prompts can sit at 0.99
            # and still be different questions when a number or a negation
            # differs, and no threshold short of 1.0 separates them.
            conflict = literals.conflict_with(entry.literals)
            if conflict is not None:
                self._increment_stat(
                    bucket_key,
                    "rejected_by_literals",
                )
                logger.debug(
                    "semantic cache rejected on %s: %.4f %r vs %r",
                    conflict, score, prompt[:60], entry.prompt[:60],
                )
                continue

            if best is None or score > best[2]:
                best = (key, entry, score)

        return best

    def _purge_expired(
        self,
        project_id: str,
        tenant_id: str | None = None,
    ) -> None:
        bucket = self._entries.get((tenant_id, project_id))
        if not bucket:
            return
        now = datetime.now(timezone.utc)
        for key in [k for k, e in bucket.items() if now >= e.expires_at]:
            del bucket[key]

    def _matching_bucket_keys(
        self,
        project_id: str | None,
        tenant_id: str | None,
        *,
        all_tenants: bool,
    ) -> list[_BucketKey]:
        return [
            key
            for key in self._entries
            if (all_tenants or key[0] == tenant_id) and (project_id is None or key[1] == project_id)
        ]

    async def _embed(
        self,
        text: str,
        bucket_key: _BucketKey,
    ) -> list[float] | None:
        """Embed, or None on any failure.

        Swallowing is deliberate: an embedding outage must degrade the cache to
        misses, not take the gateway down with it. Counted so the failure is
        visible on the admin surface rather than only in logs.
        """
        memo = self._last_embedding
        if memo is not None and memo[0] == bucket_key and memo[1] == text:
            return memo[2]
        try:
            vector = await self._embedder.embed(text)
        except Exception:
            self._increment_stat(bucket_key, "embed_failures")
            logger.warning("semantic cache: embedding failed", exc_info=True)
            return None
        if not vector:
            self._increment_stat(bucket_key, "embed_failures")
            return None
        self._last_embedding = (bucket_key, text, vector)
        return vector

    def _increment_stat(
        self,
        bucket_key: _BucketKey,
        field_name: str,
    ) -> None:
        """Increment both compatibility aggregate and tenant-local counters."""
        scoped = self._stats_by_bucket.setdefault(
            bucket_key,
            SemanticCacheStats(),
        )
        setattr(scoped, field_name, getattr(scoped, field_name) + 1)
        setattr(
            self.stats,
            field_name,
            getattr(self.stats, field_name) + 1,
        )
