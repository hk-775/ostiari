"""Detect and report drift between models.yaml and pricing.yaml.

The two files are edited independently and nothing checks them against each
other, so a model added to ``models.yaml`` without a matching entry in
``config/pricing.yaml`` has two consequences:

* **Production excludes it from routing.** Development still permits the
  mapping and ``CostTracker`` records it at $0.00, but the production profile
  removes it from model listing, direct routing, smart routing and ensembles.
  A fully unpriced model is therefore unavailable until a real rate exists.
* **Smart routing has to estimate.** The cost half of ``cost_quality_tradeoff``
  scores an unpriced candidate at the mean of the known costs, so the ranking is
  a guess for that model rather than a measurement.

Neither failure raises anything, which is why this is a page rather than a log
line: the report is the only place the gap is visible.

The lookup here is deliberately the same one ``CostTracker.calculate_cost``
performs — provider name, then *provider-side* ``model_id`` — so a mapping this
module calls priced is exactly one the biller can find.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from src.gateway.model_registry import ModelRegistry
from src.gateway.models import TokenPricing
from .page_style import (
    BASE_STYLE, BORDER, EMBED_STYLE, FAVICON, TEXT_DIM, TEXT_HEADING, ribbon,
)


def _family(model_id: str) -> str:
    """Strip version and date components, leaving the model family.

    ``mistral.mistral-large-2407-v1:0`` and ``mistral.mistral-large-2402-v1:0``
    both reduce to ``mistral.mistral-large-v``, so a version bump is
    recognizable as one. ``gpt-4.1`` and ``gpt-4`` do not collide, because a
    trailing number *is* the family for OpenAI: dropping it would make every
    ``gpt-N`` the same model.

    Deliberately conservative. A suggestion here is a rename hint that ends in
    someone reusing a price, so a false one bills wrong — silently, which is the
    failure this module exists to surface. Suggesting nothing costs an operator
    one lookup; suggesting ``claude-opus`` for a haiku mapping overcharges by an
    order of magnitude and looks deliberate.
    """
    # Drop 4-digit dates (2407, 20250514) and version suffixes (-v1:0, :0).
    stripped = re.sub(r"\d{4,}", "", model_id)
    stripped = re.sub(r"[-_.]v\d*(?::\d+)?$", "-v", stripped)
    stripped = re.sub(r":\d+$", "", stripped)
    # Collapse the separators the removals leave behind.
    stripped = re.sub(r"[-_.]{2,}", "-", stripped).strip("-_.")
    return stripped


def _suggest_rename(model_id: str, candidates: list[str]) -> str | None:
    """Find an orphan that looks like a version bump of *model_id*, if any."""
    target = _family(model_id)
    # A family of one or two characters is what is left of an id that was
    # nothing but digits; matching on it would pair unrelated models.
    if len(target) < 4:
        return None
    matches = [c for c in candidates if _family(c) == target]
    # Ambiguity is not a suggestion: two orphans in the same family give no
    # basis for choosing, and picking either is a coin flip on the rate.
    if len(matches) == 1:
        return matches[0]
    return None


@dataclass(frozen=True)
class UnpricedMapping:
    """A provider mapping in models.yaml that no pricing entry covers."""

    model: str
    provider: str
    model_id: str
    # Closest orphaned pricing key under the same provider, when there is one.
    # A rename (a pinned model id bumped to a newer snapshot) leaves an unpriced
    # mapping and an orphan entry that differ by a few characters, and naming
    # the pair turns "add a price" into "fix this key".
    suggestion: str | None = None


@dataclass(frozen=True)
class OrphanPricingEntry:
    """A pricing entry that matches no provider mapping — nothing reads it."""

    provider: str
    model_id: str


@dataclass
class PricingDriftReport:
    """The diff between the model registry and the pricing table."""

    total_mappings: int = 0
    priced_mappings: int = 0
    unpriced: list[UnpricedMapping] = field(default_factory=list)
    orphans: list[OrphanPricingEntry] = field(default_factory=list)
    # Providers used by the registry with no section in pricing.yaml at all.
    # Called out separately from the individual mappings because the fix is
    # different: add a whole block, not a single key.
    providers_missing_section: list[str] = field(default_factory=list)
    # Models where *no* provider is priced. Production makes these models
    # unavailable; development can still route and account them at $0.00.
    models_fully_unpriced: list[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        """True when either half of the diff is non-empty."""
        return bool(self.unpriced or self.orphans)

    @property
    def has_billing_gap(self) -> bool:
        """True when some mapping has no usable rate — the consequential half.

        Kept distinct from :attr:`has_drift` because the two findings differ in
        severity and in who has to act. Production blocks an unpriced mapping;
        development under-charges it. An unused pricing entry affects no
        traffic and is at worst a stale line in a config file.
        """
        return bool(self.unpriced)

    @property
    def coverage_pct(self) -> float:
        if self.total_mappings == 0:
            return 100.0
        return 100.0 * self.priced_mappings / self.total_mappings


def audit_pricing(
    model_registry: ModelRegistry,
    pricing_config: dict[str, dict[str, TokenPricing]],
) -> PricingDriftReport:
    """Diff every provider mapping in the registry against the pricing table."""
    report = PricingDriftReport()

    # Every (provider, model_id) the registry actually asks to be priced.
    referenced: set[tuple[str, str]] = set()
    registry_providers: set[str] = set()

    for model_name in sorted(model_registry.models):
        model_config = model_registry.models[model_name]
        priced_here = 0
        for mapping in model_config.providers:
            report.total_mappings += 1
            registry_providers.add(mapping.provider)
            referenced.add((mapping.provider, mapping.model_id))

            # An inline pricing: block in models.yaml counts as priced — it is
            # the more specific declaration and smart routing prefers it.
            has_inline = (
                mapping.pricing is not None
                and mapping.pricing.is_billable
            )
            table_pricing = pricing_config.get(mapping.provider, {}).get(
                mapping.model_id
            )
            in_table = (
                table_pricing is not None
                and table_pricing.is_billable
            )
            if has_inline or in_table:
                report.priced_mappings += 1
                priced_here += 1
                continue

            entry = UnpricedMapping(
                model=model_name,
                provider=mapping.provider,
                model_id=mapping.model_id,
            )
            report.unpriced.append(entry)

        if model_config.providers and priced_here == 0:
            report.models_fully_unpriced.append(model_name)

    # Pricing entries nothing refers to. Usually the other half of a rename, so
    # they are a hint rather than dead weight — but either way nothing reads
    # them, so a price entered here has no effect on a bill.
    for provider in sorted(pricing_config):
        for model_id in sorted(pricing_config[provider]):
            if (provider, model_id) not in referenced:
                report.orphans.append(OrphanPricingEntry(provider, model_id))

    report.providers_missing_section = sorted(
        p for p in registry_providers if not pricing_config.get(p)
    )

    # Pair each unpriced mapping with an orphan in the same family under the
    # same provider. Cross-provider matches are excluded: the same model id
    # appears under several providers legitimately, and suggesting one
    # provider's key for another's mapping would produce a wrong price rather
    # than no price.
    orphans_by_provider: dict[str, list[str]] = {}
    for orphan in report.orphans:
        orphans_by_provider.setdefault(orphan.provider, []).append(orphan.model_id)

    for i, entry in enumerate(report.unpriced):
        pool = orphans_by_provider.get(entry.provider)
        if not pool:
            continue
        match = _suggest_rename(entry.model_id, pool)
        if match:
            report.unpriced[i] = UnpricedMapping(
                model=entry.model,
                provider=entry.provider,
                model_id=entry.model_id,
                suggestion=match,
            )

    return report


def build_yaml_skeleton(report: PricingDriftReport) -> str:
    """Render the missing entries as a paste-ready pricing.yaml fragment.

    Costs are left as ``0.0`` with a TODO rather than guessed: a wrong price is
    worse than a missing one, because a missing one shows up on this page and a
    wrong one bills silently. Values are per 1,000 tokens, matching the file.
    """
    if not report.unpriced:
        return ""

    by_provider: dict[str, list[UnpricedMapping]] = {}
    for entry in report.unpriced:
        by_provider.setdefault(entry.provider, []).append(entry)

    lines = ["providers:"]
    for provider in sorted(by_provider):
        lines.append(f"  {provider}:")
        seen: set[str] = set()
        for entry in sorted(by_provider[provider], key=lambda e: e.model_id):
            if entry.model_id in seen:
                continue
            seen.add(entry.model_id)
            lines.append(f"    {entry.model_id}:")
            lines.append("      prompt_token_cost: 0.0      # TODO per 1K tokens")
            lines.append("      completion_token_cost: 0.0  # TODO per 1K tokens")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

_STYLE = BASE_STYLE + (
    # Only what this page adds to the shared sheet: a dark code block for the
    # YAML to paste, and the pricing-file caption.
    f"pre{{background:{TEXT_HEADING};color:{BORDER};padding:16px;border-radius:12px;"
    "overflow:auto;font-family:'SF Mono','Fira Code',ui-monospace,monospace;"
    "font-size:12px;line-height:1.6}"
    f".file{{font-size:13px;color:{TEXT_DIM};margin:0 0 10px}}"
)


def _esc(value: object) -> str:
    return html.escape(str(value))


def render_drift_page(
    report: PricingDriftReport,
    pricing_path: str,
    *,
    embed: bool = False,
    unpriced_mappings_blocked: bool = False,
) -> str:
    """Render the drift report as a self-contained HTML page.

    With ``embed``, drop the ribbon and the page framing: the dashboard shell
    supplies both, and two stacked toolbars is the tell that a page was bolted
    on rather than built in.
    """
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        "<title>AxonLLM — Pricing Coverage</title>",
        FAVICON,
        f"<style>{_STYLE}{EMBED_STYLE if embed else ''}</style></head><body>",
    ]
    if not embed:
        parts.append(
            ribbon("Pricing Coverage", ("/admin/production-checklist", "Readiness"))
        )
    parts.append('<div class="wrap">')

    unpriced_n = len(report.unpriced)
    if not report.has_billing_gap:
        # Every mapping bills at a real rate. Leftover entries may still exist,
        # but they charge nobody anything, so this is the healthy state — say so
        # and mention the leftovers as housekeeping rather than as a warning.
        extra = ""
        if report.orphans:
            extra = (
                f" {len(report.orphans)} pricing entr"
                f"{'ies' if len(report.orphans) != 1 else 'y'} below "
                f"{'match' if len(report.orphans) != 1 else 'matches'} no model and "
                f"{'are' if len(report.orphans) != 1 else 'is'} never read — safe to "
                "leave, safe to delete."
            )
        parts.append(
            '<div class="banner ok"><h1>Every provider mapping has a price.</h1>'
            f"<p>All {report.total_mappings} mappings in the model registry resolve to an "
            "entry in the pricing table, so usage is billed at a real rate and smart "
            f"routing is scoring on measured cost.{extra}</p></div>"
        )
    else:
        fully = len(report.models_fully_unpriced)
        detail = ""
        if fully:
            if unpriced_mappings_blocked:
                detail = (
                    f" <b>{fully}</b> configured model"
                    f"{'s are' if fully != 1 else ' is'} unavailable because "
                    f"{'none of their' if fully != 1 else 'its'} provider "
                    f"mapping{'s have' if fully != 1 else ' has'} a usable rate."
                )
            else:
                detail = (
                    f" <b>{fully}</b> model{'s' if fully != 1 else ''} "
                    f"{'have' if fully != 1 else 'has'} no priced provider at all, so every "
                    "request to "
                    f"{'them' if fully != 1 else 'it'} is free as far as the gateway knows."
                )
        if unpriced_mappings_blocked:
            consequence = (
                "<p>Production routing excludes these mappings from model listing, "
                "direct requests, smart routing, streaming, and ensembles. "
                f"{detail}</p>"
            )
        else:
            consequence = (
                "<p>Development requests routed to these are recorded at "
                "<b>$0.00</b>, so project spend, budget blocks and quota alerts "
                f"under-count.{detail}</p>"
            )
        parts.append(
            '<div class="banner warn"><h1>'
            f"{unpriced_n} of {report.total_mappings} provider mappings have no price."
            "</h1>"
            f"{consequence}"
            "<p>Smart routing has no measured cost for these mappings.</p>"
            f'<p>Fill the entries below with verified rates in '
            f"<code>{_esc(pricing_path)}</code>. A <code>0.0</code> TODO remains "
            "unpriced and cannot bypass production enforcement.</p></div>"
        )

    parts.append(
        '<div class="stats">'
        f'<div class="stat"><b>{report.total_mappings}</b><small>provider mappings</small></div>'
        f'<div class="stat"><b>{report.priced_mappings}</b><small>priced</small></div>'
        f'<div class="stat"><b>{unpriced_n}</b><small>unpriced</small></div>'
        f'<div class="stat"><b>{report.coverage_pct:.0f}%</b><small>coverage</small></div>'
        f'<div class="stat"><b>{len(report.orphans)}</b><small>unused entries</small></div>'
        "</div>"
    )

    if report.providers_missing_section:
        names = ", ".join(f"<code>{_esc(p)}</code>" for p in report.providers_missing_section)
        consequence = (
            "Every model on these providers is unavailable in production"
            if unpriced_mappings_blocked
            else "Every model on these providers is accounted at $0.00"
        )
        parts.append(
            f'<h2>Providers with no pricing section <span class="count">'
            f"({len(report.providers_missing_section)})</span></h2>"
            f'<p class="hint">{consequence}: {names}. The provider has no '
            f"block in <code>{_esc(pricing_path)}</code> at all, so there is nothing to "
            "look a model id up in.</p>"
        )

    if report.unpriced:
        parts.append(
            f'<h2>Unpriced mappings <span class="count">({unpriced_n})</span></h2>'
            '<p class="hint">Keyed by provider and <b>provider-side model id</b> — the id '
            "sent to the provider, not the gateway's model name. That is the key "
            "<code>CostTracker</code> bills on, so the pricing entry has to match it "
            "exactly.</p>"
            "<table><tr><th>Model</th><th>Provider</th><th>Provider model id</th>"
            "<th>Suggestion</th></tr>"
        )
        for entry in report.unpriced:
            if entry.suggestion:
                fix = (
                    '<span class="fix">rename <code>'
                    f"{_esc(entry.suggestion)}</code> in pricing.yaml</span>"
                )
            else:
                fix = "<small>add a new entry</small>"
            parts.append(
                f"<tr><td>{_esc(entry.model)}</td><td><code>{_esc(entry.provider)}</code></td>"
                f"<td><code>{_esc(entry.model_id)}</code></td><td>{fix}</td></tr>"
            )
        parts.append("</table>")

    if report.orphans:
        parts.append(
            f'<h2>Pricing entries nothing uses <span class="count">'
            f"({len(report.orphans)})</span></h2>"
            '<p class="hint">These are priced but no model maps to them, which usually '
            "means a pinned model id moved on in <code>models.yaml</code> and the price "
            "stayed behind. A rate entered here is never charged.</p>"
            "<table><tr><th>Provider</th><th>Model id in pricing.yaml</th></tr>"
        )
        for orphan in report.orphans:
            parts.append(
                f"<tr><td><code>{_esc(orphan.provider)}</code></td>"
                f"<td><code>{_esc(orphan.model_id)}</code></td></tr>"
            )
        parts.append("</table>")

    skeleton = build_yaml_skeleton(report)
    if skeleton:
        parts.append(
            "<h2>Paste-ready fragment</h2>"
            f'<p class="file">Merge into <code>{_esc(pricing_path)}</code>. Costs are per '
            "1,000 tokens and left at <code>0.0</code> deliberately — a guessed rate bills "
            "silently, a missing one shows up here.</p>"
            f"<pre>{_esc(skeleton)}</pre>"
        )

    parts.append("</div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Startup notice
# ---------------------------------------------------------------------------


def format_startup_notice(report: PricingDriftReport, url: str) -> str | None:
    """One-paragraph startup banner pointing at the page, or None if clean.

    Returns ``None`` rather than a "no drift" message so a healthy deployment's
    startup output stays quiet — a banner printed every time is one nobody
    reads, which is the failure mode this whole page exists to avoid.
    """
    # Gated on the billing gap, not on drift generally: leftover pricing entries
    # charge nobody anything, and a banner that outlives the fix is one nobody
    # reads. They are still listed on the page.
    if not report.has_billing_gap:
        return None

    rule = "  " + "─" * 68
    lines = [
        rule,
        f"  ⚠  PRICING GAP: {len(report.unpriced)} of {report.total_mappings} "
        f"provider mappings have no price.",
        "",
        "     Production excludes these mappings from listing and routing.",
        "     Development accounts them at $0.00 and smart routing has to",
        "     estimate rather than use a measured cost.",
    ]
    if report.providers_missing_section:
        names = ", ".join(report.providers_missing_section)
        lines += ["", f"     No pricing section at all for: {names}"]
    lines += ["", f"     Review and fix:  {url}", rule]
    return "\n".join(lines)
