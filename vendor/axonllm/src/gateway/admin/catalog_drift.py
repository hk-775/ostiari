"""Detect and report drift between the routing table, the catalog, and traffic.

``models.yaml`` decides what the router can dispatch to. ``catalog.yaml``
describes what those models *are* — display name, capabilities, which provider
serves them. The two files are edited independently and nothing checks them
against each other, so they drift, and the drift is invisible because neither
file is wrong on its own terms.

Three consequences, none of which raises anything:

* **The catalog answers for models nobody can reach.** ``/admin/catalog`` is the
  provider/model picker the dashboard and playground read. An entry for a model
  no mapping routes to is offered to an operator who then cannot select it.
* **Routed mappings have no description.** ``/admin/models`` returns
  ``capabilities`` per model; unpopulated it returns ``[]``, so a caller asking
  "does this model do vision" is told no rather than unknown. A silent no is
  worse than a gap, because it reads as an answer.
* **Traffic can name a model the registry does not list.** A usage record
  carries the *resolved* provider, so a request served by a mapping absent from
  ``models.yaml`` is recorded rather than rejected — an unsanctioned path,
  visible only by comparing the two.

That third one is the reason this is a page and not a lint rule. A static check
of two files catches the first two; only the join against recorded usage catches
a model that is being called but was never declared, and only usage tells you
which declared models are dormant. Dormant is the useful half in practice: it is
the difference between "populate metadata for 48 models" and "for the 9 that
carry traffic".

The lookups here mirror the ones the serving code performs — ``list_models``
matches a usage record by logical name *or* any provider ``model_id``, and the
catalog is keyed by provider then provider-side ``model_id`` — so a mapping this
module calls described is one the dashboard can actually describe.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.gateway.model_registry import ModelRegistry

from .page_style import (
    BASE_STYLE,
    BORDER,
    EMBED_STYLE,
    FAVICON,
    TEXT_DIM,
    TEXT_HEADING,
    ribbon,
)


class _UsageLike(Protocol):
    """The two fields of a usage record this module reads.

    Structural rather than importing ``UsageRecord``: the audit only needs a
    model name and a resolved provider, and typing it this way keeps the report
    testable from tuples without constructing full records.
    """

    model: str
    provider: str


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UndescribedMapping:
    """A routed provider mapping the catalog carries no metadata for."""

    model: str
    provider: str
    model_id: str
    # True when the provider has no section in catalog.yaml at all, as opposed
    # to a section that simply omits this model. The fix differs: add a whole
    # provider block, or add one model to an existing list.
    provider_section_missing: bool = False


@dataclass(frozen=True)
class UnroutableCatalogEntry:
    """A catalog entry no provider mapping routes to — offered but unreachable."""

    provider: str
    model_id: str
    display_name: str = ""


@dataclass(frozen=True)
class DormantModel:
    """A model in the registry that has served no recorded request.

    Not a defect. Carried because it scopes the others: metadata, pricing and
    review effort belong first to the models actually carrying traffic, and a
    long dormant list is also the surface nobody is watching.
    """

    model: str
    providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class UndeclaredTraffic:
    """Recorded usage naming a model or provider the registry does not list.

    The consequential finding. Every other entry on this page is a
    documentation gap; this one is a request that was served by a path nobody
    declared, which is what shadow AI looks like from inside the gateway.
    """

    model: str
    provider: str
    requests: int = 0


@dataclass
class CatalogDriftReport:
    """The three-way diff between routing, catalog metadata, and traffic."""

    total_mappings: int = 0
    described_mappings: int = 0
    total_models: int = 0
    undescribed: list[UndescribedMapping] = field(default_factory=list)
    unroutable: list[UnroutableCatalogEntry] = field(default_factory=list)
    dormant: list[DormantModel] = field(default_factory=list)
    undeclared: list[UndeclaredTraffic] = field(default_factory=list)
    # Providers the router dispatches to with no catalog section at all. Called
    # out separately from the individual mappings for the same reason
    # pricing_drift does it: the fix is a whole block, not one key.
    providers_missing_section: list[str] = field(default_factory=list)
    # Models declaring no capabilities. Tracked on the logical model rather than
    # the mapping because that is the field /admin/models returns, and it is the
    # one a caller reads to decide whether a model can do what they need.
    models_without_capabilities: list[str] = field(default_factory=list)
    # None when no usage was supplied, distinguishing "nothing has run yet" from
    # "everything is dormant" — the two look identical in the counts and mean
    # opposite things.
    observed_models: int | None = None

    @property
    def has_drift(self) -> bool:
        """True when any half of the diff is non-empty."""
        return bool(self.undescribed or self.unroutable or self.undeclared)

    @property
    def has_undeclared_traffic(self) -> bool:
        """True when traffic used a path the registry does not declare.

        Kept distinct from :attr:`has_drift` for the reason pricing_drift keeps
        ``has_billing_gap`` distinct: severity and audience differ. An
        undescribed mapping is a docs chore for whoever owns the catalog. Traffic
        on an undeclared model is a governance finding, and escalating both the
        same way means the alarm still fires after the chore is done, which
        teaches people to ignore it.
        """
        return bool(self.undeclared)

    @property
    def coverage_pct(self) -> float:
        """Share of routed mappings the catalog can describe."""
        if self.total_mappings == 0:
            return 100.0
        return 100.0 * self.described_mappings / self.total_mappings

    @property
    def trafficked_models(self) -> int:
        """Registry models that served at least one recorded request."""
        return self.total_models - len(self.dormant)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _catalog_index(
    catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Map provider -> {model_id: display_name} from the catalog structure.

    Tolerant of a missing or malformed ``models`` list: the catalog is loaded
    from YAML an operator edits, and a report that raises on a typo is a report
    that stops being run. An entry with no ``model_id`` is skipped rather than
    keyed on the empty string, which would otherwise "describe" every mapping
    whose id is also empty.
    """
    index: dict[str, dict[str, str]] = {}
    for provider, block in (catalog or {}).items():
        entries = (block or {}).get("models") or []
        by_id: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("model_id")
            if not model_id:
                continue
            by_id[str(model_id)] = str(entry.get("name") or "")
        index[provider] = by_id
    return index


def audit_catalog(
    model_registry: ModelRegistry,
    catalog: dict[str, dict[str, Any]],
    usage: Sequence[_UsageLike] | Iterable[_UsageLike] | None = None,
) -> CatalogDriftReport:
    """Diff routing against catalog metadata, and both against recorded usage.

    *usage* is optional: with it the report can name dormant models and
    undeclared traffic, without it the static halves still hold. Passing an
    empty sequence is not the same as passing nothing — see
    :attr:`CatalogDriftReport.observed_models`.
    """
    report = CatalogDriftReport()
    index = _catalog_index(catalog)

    # Every (provider, model_id) the router can dispatch to, and every name a
    # usage record could legitimately carry for it. list_models matches records
    # by logical name *or* provider-side id, so the declared set has to include
    # both or a record naming the provider id reads as undeclared.
    routed: set[tuple[str, str]] = set()
    declared_names: set[str] = set()
    declared_pairs: set[tuple[str, str]] = set()
    routed_providers: set[str] = set()

    for model_name in sorted(model_registry.models):
        model_config = model_registry.models[model_name]
        report.total_models += 1
        declared_names.add(model_name)

        if not model_config.capabilities:
            report.models_without_capabilities.append(model_name)

        for mapping in model_config.providers:
            report.total_mappings += 1
            routed.add((mapping.provider, mapping.model_id))
            routed_providers.add(mapping.provider)
            declared_pairs.add((model_name, mapping.provider))
            declared_pairs.add((mapping.model_id, mapping.provider))

            section = index.get(mapping.provider)
            if section is not None and mapping.model_id in section:
                report.described_mappings += 1
                continue

            report.undescribed.append(
                UndescribedMapping(
                    model=model_name,
                    provider=mapping.provider,
                    model_id=mapping.model_id,
                    provider_section_missing=section is None,
                )
            )

    # Catalog entries nothing routes to. The dashboard offers these in its model
    # picker, so an operator can select one and get a routing failure.
    for provider in sorted(index):
        for model_id in sorted(index[provider]):
            if (provider, model_id) not in routed:
                report.unroutable.append(
                    UnroutableCatalogEntry(
                        provider=provider,
                        model_id=model_id,
                        display_name=index[provider][model_id],
                    )
                )

    report.providers_missing_section = sorted(
        p for p in routed_providers if p not in index
    )

    if usage is None:
        return report

    # ── The traffic join ───────────────────────────────────────────────────
    seen_pairs: dict[tuple[str, str], int] = {}
    seen_names: set[str] = set()
    for record in usage:
        model = str(getattr(record, "model", "") or "")
        provider = str(getattr(record, "provider", "") or "")
        seen_pairs[(model, provider)] = seen_pairs.get((model, provider), 0) + 1
        seen_names.add(model)

    report.observed_models = len(seen_names)

    for pair, count in sorted(seen_pairs.items()):
        model, provider = pair
        # Declared as a (name, provider) pair, not just a known name: a model
        # served by a provider it has no mapping for is as undeclared as an
        # unknown model, and is the more likely misconfiguration of the two.
        if pair not in declared_pairs:
            report.undeclared.append(
                UndeclaredTraffic(model=model, provider=provider, requests=count)
            )

    for model_name in sorted(model_registry.models):
        model_config = model_registry.models[model_name]
        ids = {model_name} | {p.model_id for p in model_config.providers}
        if not (ids & seen_names):
            report.dormant.append(
                DormantModel(
                    model=model_name,
                    providers=tuple(p.provider for p in model_config.providers),
                )
            )

    return report


def build_catalog_skeleton(report: CatalogDriftReport) -> str:
    """Render the undescribed mappings as a paste-ready catalog.yaml fragment.

    ``capabilities`` is left empty with a TODO rather than guessed. Guessing here
    is worse than omitting: the field is what a caller reads to decide whether a
    model supports vision or tools, so an invented ``vision`` sends a request
    that fails at the provider, and an invented omission hides a capability the
    model has. Both look like settled facts on the page that reads them.
    """
    if not report.undescribed:
        return ""

    by_provider: dict[str, list[UndescribedMapping]] = {}
    for entry in report.undescribed:
        by_provider.setdefault(entry.provider, []).append(entry)

    lines = ["providers:"]
    for provider in sorted(by_provider):
        lines.append(f"  {provider}:")
        if any(e.provider_section_missing for e in by_provider[provider]):
            lines.append(f"    display_name: {provider}   # TODO human-readable")
            lines.append("    auth_type: api_key           # TODO")
        lines.append("    models:")
        seen: set[str] = set()
        for entry in sorted(by_provider[provider], key=lambda e: e.model_id):
            if entry.model_id in seen:
                continue
            seen.add(entry.model_id)
            lines.append(f"      - model_id: {entry.model_id}")
            lines.append(f"        name: {entry.model}   # TODO display name")
            lines.append("        capabilities: []       # TODO chat/vision/tools/streaming")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

_STYLE = BASE_STYLE + (
    # Only what this page adds to the shared sheet: the dark paste block and the
    # file caption, both matching the pricing page so the two read as siblings.
    f"pre{{background:{TEXT_HEADING};color:{BORDER};padding:16px;border-radius:12px;"
    "overflow:auto;font-family:'SF Mono','Fira Code',ui-monospace,monospace;"
    "font-size:12px;line-height:1.6}"
    f".file{{font-size:13px;color:{TEXT_DIM};margin:0 0 10px}}"
)


def _esc(value: object) -> str:
    return html.escape(str(value))


def _plural(n: int, singular: str, plural: str = "") -> str:
    return singular if n == 1 else (plural or singular + "s")


def render_catalog_drift_page(
    report: CatalogDriftReport,
    models_path: str,
    catalog_path: str,
    *,
    embed: bool = False,
) -> str:
    """Render the catalog drift report as a self-contained HTML page.

    With ``embed``, drop the ribbon and the page framing: the dashboard shell
    supplies both, and two stacked toolbars is the tell that a page was bolted
    on rather than built in.
    """
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        "<title>AxonLLM — Catalogue Coverage</title>",
        FAVICON,
        f"<style>{_STYLE}{EMBED_STYLE if embed else ''}</style></head><body>",
    ]
    if not embed:
        parts.append(
            ribbon(
                # Same words the dashboard's sidebar and subtitle use. A ribbon
                # that says something else makes the framed and standalone
                # copies read as two different pages.
                "Catalogue Coverage",
                ("/admin/pricing-drift", "Pricing"),
                ("/admin/production-checklist", "Readiness"),
            )
        )
    parts.append('<div class="wrap">')

    # ── Headline ──────────────────────────────────────────────────────────
    if report.has_undeclared_traffic:
        n = len(report.undeclared)
        parts.append(
            f'<div class="banner fail"><h1>{n} undeclared '
            f'{_plural(n, "path")} served traffic.</h1>'
            "<p>A recorded request names a model and provider the registry does not "
            "declare, so it was billed and answered by a route nobody put in "
            f"<code>{_esc(models_path)}</code>. Everything else on this page is a "
            "documentation gap; this is not.</p></div>"
        )
    elif not report.has_drift:
        extra = ""
        if report.dormant:
            n = len(report.dormant)
            extra = (
                f" {n} of {report.total_models} models have served no recorded "
                "request — reachable and described, just unused."
            )
        parts.append(
            '<div class="banner ok"><h1>The catalog describes every routed model.</h1>'
            f"<p>All {report.total_mappings} provider "
            f'{_plural(report.total_mappings, "mapping")} in the registry resolve to a '
            "catalog entry, and no catalog entry is offered that routing cannot "
            f"reach.{extra}</p></div>"
        )
    else:
        n = len(report.undescribed)
        parts.append(
            f'<div class="banner warn"><h1>{n} routed '
            f'{_plural(n, "mapping")} {_plural(n, "has", "have")} no metadata.</h1>'
            f"<p><code>/admin/models</code> returns <code>capabilities: []</code> for "
            "these, which reads as <em>no</em> rather than <em>unknown</em> to whoever "
            "asks whether the model supports vision or tools.</p></div>"
        )

    # ── Counters ──────────────────────────────────────────────────────────
    observed = (
        "—" if report.observed_models is None else str(report.trafficked_models)
    )
    parts.append(
        '<div class="stats">'
        f'<div class="stat"><b>{report.total_models}</b><small>models declared</small></div>'
        f'<div class="stat"><b>{report.total_mappings}</b><small>provider mappings</small></div>'
        f'<div class="stat"><b>{report.described_mappings}</b><small>described</small></div>'
        f'<div class="stat"><b>{report.coverage_pct:.0f}%</b><small>coverage</small></div>'
        f'<div class="stat"><b>{observed}</b><small>carrying traffic</small></div>'
        f'<div class="stat"><b>{len(report.unroutable)}</b><small>unroutable</small></div>'
        "</div>"
    )

    # ── Undeclared traffic ────────────────────────────────────────────────
    if report.undeclared:
        parts.append(
            "<h2>Traffic on undeclared paths</h2>"
            "<p>Recorded usage naming a model/provider pair with no mapping in "
            f"<code>{_esc(models_path)}</code>. Either the registry lost an entry it "
            "once had, or something is reaching a provider without going through the "
            "routing table.</p>"
            "<table><thead><tr><th>Model</th><th>Provider</th>"
            "<th>Requests</th></tr></thead><tbody>"
        )
        for entry in report.undeclared:
            parts.append(
                f"<tr><td><code>{_esc(entry.model)}</code></td>"
                f"<td>{_esc(entry.provider)}</td>"
                f"<td>{entry.requests}</td></tr>"
            )
        parts.append("</tbody></table>")

    # ── Undescribed mappings ──────────────────────────────────────────────
    if report.undescribed:
        parts.append(
            "<h2>Routed but undescribed</h2>"
            f"<p>Mappings in <code>{_esc(models_path)}</code> with no entry in "
            f"<code>{_esc(catalog_path)}</code>.</p>"
            "<table><thead><tr><th>Model</th><th>Provider</th>"
            "<th>Provider model id</th></tr></thead><tbody>"
        )
        for entry in report.undescribed:
            note = (
                ' <span class="hint">(no provider section)</span>'
                if entry.provider_section_missing
                else ""
            )
            parts.append(
                f"<tr><td>{_esc(entry.model)}</td>"
                f"<td>{_esc(entry.provider)}{note}</td>"
                f"<td><code>{_esc(entry.model_id)}</code></td></tr>"
            )
        parts.append("</tbody></table>")

        skeleton = build_catalog_skeleton(report)
        if skeleton:
            parts.append(
                f'<h2>Paste into {_esc(catalog_path)}</h2>'
                '<p class="file">Names and capabilities are left as TODO rather than '
                "guessed: an invented capability sends a request the provider rejects, "
                "and an invented omission hides one the model has.</p>"
                f"<pre>{_esc(skeleton)}</pre>"
            )

    # ── Unroutable catalog entries ────────────────────────────────────────
    if report.unroutable:
        n = len(report.unroutable)
        parts.append(
            "<h2>Described but unroutable</h2>"
            f"<p>{n} catalog {_plural(n, 'entry', 'entries')} that no mapping routes "
            "to. <code>/admin/catalog</code> is the dashboard's model picker, so these "
            "can be selected and will fail to route.</p>"
            "<table><thead><tr><th>Provider</th><th>Provider model id</th>"
            "<th>Display name</th></tr></thead><tbody>"
        )
        for entry in report.unroutable:
            parts.append(
                f"<tr><td>{_esc(entry.provider)}</td>"
                f"<td><code>{_esc(entry.model_id)}</code></td>"
                f"<td>{_esc(entry.display_name) or '&mdash;'}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    # ── Dormant models ────────────────────────────────────────────────────
    if report.dormant and report.observed_models is not None:
        n = len(report.dormant)
        parts.append(
            "<h2>Declared but dormant</h2>"
            f"<p>{n} of {report.total_models} models have served no recorded request. "
            "Not a defect — carried because it scopes the work above: metadata and "
            "pricing matter first for the "
            f"{report.trafficked_models} that carry traffic. A long list here is also "
            "surface nobody is watching.</p>"
            "<table><thead><tr><th>Model</th><th>Providers</th></tr></thead><tbody>"
        )
        for entry in report.dormant:
            provs = ", ".join(entry.providers) or "—"
            parts.append(
                f"<tr><td>{_esc(entry.model)}</td><td>{_esc(provs)}</td></tr>"
            )
        parts.append("</tbody></table>")

    # ── Capabilities gap ──────────────────────────────────────────────────
    if report.models_without_capabilities:
        n = len(report.models_without_capabilities)
        names = ", ".join(_esc(m) for m in report.models_without_capabilities)
        parts.append(
            "<h2>No capabilities declared</h2>"
            f"<p>{n} of {report.total_models} models declare no capabilities, so "
            "<code>/admin/models</code> reports <code>[]</code> for them. The field is "
            f"on the logical model in <code>{_esc(models_path)}</code>.</p>"
            f'<p class="hint">{names}</p>'
        )

    if report.observed_models is None:
        parts.append(
            '<p class="hint">No usage was supplied, so the traffic halves of this '
            "report (dormant models, undeclared paths) were not computed. That is "
            "distinct from having seen zero requests.</p>"
        )

    parts.append("</div></body></html>")
    return "".join(parts)
