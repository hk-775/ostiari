"""One stylesheet for the standalone admin report pages.

`pricing_drift`, `production_checklist` and the architecture viewer each carried
their own near-identical copy of a stylesheet, and all three were the old AWS
Console palette (`#232F3E` navy, `#FF9900` orange, `#5f6b7a` slate) while the
dashboard they link back to is stone-and-violet. Three copies is why: nobody
recolors the same CSS three times, so the pages drifted away from the product.

The values here mirror the dashboard's `--awsui-color-*` variables in
`admin/static/index.html`, which are also the landing page's palette, so the
whole surface is one identity.

Status tones deliberately differ from the dashboard's:

    dashboard          here          on its tint
    #16a34a success ->  #15803d      3.15 -> 4.79
    #dc2626 error   ->  #b91c1c      4.41 -> 5.91
    #d97706 warning ->  #b45309      3.07 -> 4.84
    #2563eb info    ->  #1d4ed8      4.85 -> 6.29

The dashboard shows status as large numerals and dot indicators, where its -600
shades are fine. These pages set status as 11-13px table text and 10px pills --
body text, which needs 4.5 rather than 3.0. Same hue families one step darker,
so it reads as the same palette while staying legible.
"""

from __future__ import annotations

# Mirrors --awsui-color-* in admin/static/index.html.
BG = "#fafaf9"           # stone-50
SURFACE = "#ffffff"
BORDER = "#e7e5e4"       # stone-200
BORDER_SOFT = "#f5f5f4"  # stone-100
TEXT = "#1c1917"         # stone-900
TEXT_HEADING = "#0c0a09" # stone-950
TEXT_DIM = "#78716c"     # stone-500
# stone-600, for the one place stone-500 is not legible: 11-12px uppercase
# table headers sit on stone-100, where stone-500 measures 4.40. Uppercase
# at that size is the hardest text on the page to read, not the easiest.
TEXT_MUTED = "#57534e"   # stone-600
PRIMARY = "#7c3aed"      # violet-600 — the dashboard's --awsui-color-primary
PRIMARY_HOVER = "#6d28d9"  # violet-700

# Status: the dashboard's tint backgrounds with one-step-darker text (see above).
OK = "#15803d"           # green-700
OK_BG = "#f0fdf4"        # --awsui-color-background-status-success
ERR = "#b91c1c"          # red-700
ERR_BG = "#fef2f2"       # --awsui-color-background-status-error
WARN = "#b45309"         # amber-700
WARN_BG = "#fffbeb"      # --awsui-color-background-status-warning
INFO = "#1d4ed8"         # blue-700
INFO_BG = "#f0f9ff"      # --awsui-color-background-status-info
UNKNOWN = "#78716c"      # stone-500 — not a status color; absence of one

_FONT = (
    "'Inter','Open Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif"
)
_MONO = "'SF Mono','Fira Code',ui-monospace,SFMono-Regular,Menlo,monospace"

# Shared by every report page. Page-specific rules go in the caller's own
# stylesheet, appended after this one.
BASE_STYLE = (
    f"body{{margin:0;background:{BG};font-family:{_FONT};color:{TEXT}}}"
    # The dashboard sidebar is white with violet accents, so a white toolbar with
    # a violet action matches it. The old navy bar matched nothing on the site.
    # The ribbon. Geometry copied from the dashboard's .sidebar-logo -- 64px
    # tall, 0.6rem gap, stone-100 divider -- so the mark sits identically
    # whether you arrive from the dashboard or land here directly.
    f".toolbar{{background:{SURFACE};height:64px;padding:0 24px;display:flex;"
    f"align-items:center;gap:14px;border-bottom:1px solid {BORDER};"
    "position:sticky;top:0;z-index:50}"
    f".toolbar .brand{{display:flex;align-items:center;gap:0.6rem;"
    f"text-decoration:none;color:{TEXT};padding-right:14px;"
    f"border-right:1px solid {BORDER_SOFT}}}"
    ".toolbar .brand svg{height:28px;width:28px;flex-shrink:0}"
    f".toolbar .brand b{{font-size:17px;font-weight:700;color:{TEXT};"
    "letter-spacing:-0.3px}"
    # The page title, not the product name -- the brand block already says that.
    f".toolbar .title{{color:{TEXT_HEADING};font-size:15px;font-weight:600;flex:1;"
    "letter-spacing:-0.2px}"
    f".toolbar a.action{{color:{SURFACE};text-decoration:none;font-size:13px;"
    f"padding:7px 15px;border-radius:8px;background:{PRIMARY};font-weight:600;"
    "white-space:nowrap}"
    f".toolbar a.action:hover{{background:{PRIMARY_HOVER}}}"
    # Secondary ribbon links: outlined, so there is one filled action per page.
    f".toolbar a.quiet{{color:{TEXT};text-decoration:none;font-size:13px;"
    f"padding:7px 15px;border-radius:8px;background:{SURFACE};font-weight:600;"
    f"border:1px solid {BORDER};white-space:nowrap}}"
    f".toolbar a.quiet:hover{{background:{BORDER_SOFT}}}"
    ".wrap{max-width:1000px;margin:0 auto;padding:24px 20px 60px}"
    # rounded-2xl and the dashboard's card shadow, not a flat 1px box.
    f".banner{{border-radius:16px;padding:18px 20px;margin-bottom:24px;"
    "box-shadow:0 0 0 1px rgba(214,211,209,0.3),0 1px 3px rgba(0,0,0,0.04)}"
    f".banner.ok{{background:{OK_BG};border:1px solid #bbf7d0;"
    f"border-left:5px solid {OK}}}"
    f".banner.warn{{background:{WARN_BG};border:1px solid #fde68a;"
    f"border-left:5px solid {WARN}}}"
    f".banner.fail{{background:{ERR_BG};border:1px solid #fecaca;"
    f"border-left:5px solid {ERR}}}"
    f".banner h1{{margin:0 0 8px;font-size:19px;color:{TEXT_HEADING};"
    "letter-spacing:-0.3px}"
    ".banner p{margin:6px 0;font-size:14px;line-height:1.55}"
    ".stats{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 24px}"
    f".stat{{background:{SURFACE};border:1px solid {BORDER};border-radius:16px;"
    "padding:14px 18px;min-width:112px;"
    "box-shadow:0 0 0 1px rgba(214,211,209,0.3),0 1px 3px rgba(0,0,0,0.04)}"
    f".stat b{{display:block;font-size:22px;line-height:1.2;color:{TEXT_HEADING}}}"
    f".stat small{{color:{TEXT_DIM};font-size:12px}}"
    f"h2{{font-size:16px;margin:28px 0 6px;color:{TEXT_HEADING};"
    "letter-spacing:-0.2px}"
    f"h2 .count{{color:{TEXT_DIM};font-weight:400}}"
    f".hint{{color:{TEXT_DIM};font-size:13px;line-height:1.55;margin:0 0 12px}}"
    f"table{{width:100%;border-collapse:collapse;background:{SURFACE};"
    f"border:1px solid {BORDER};border-radius:12px;overflow:hidden;font-size:13px}}"
    f"th{{text-align:left;background:{BORDER_SOFT};padding:9px 12px;font-size:12px;"
    f"text-transform:uppercase;letter-spacing:.04em;color:{TEXT_MUTED}}}"
    f"td{{padding:8px 12px;border-top:1px solid {BORDER_SOFT};vertical-align:top}}"
    f"code{{font-family:{_MONO};font-size:12px;background:{BORDER_SOFT};"
    "padding:1px 5px;border-radius:4px}"
    f".fix{{color:{INFO}}}"
    # These pages are all links; the browser default ring is near-invisible on
    # near-white. :focus-visible so a mouse click leaves nothing behind.
    f"a:focus-visible{{outline:2px solid {PRIMARY};outline-offset:2px;"
    "border-radius:8px}"
)


# Appended when a page is rendered inside the dashboard shell (?embed=1).
#
# The shell supplies the chrome the standalone page supplies itself: the sidebar
# navigates, the topbar carries the brand, and the React page header carries the
# title. So the ribbon is suppressed by the caller and this strips the framing
# the wrap adds -- the dashboard's .main is already padded, and a centered
# 1000px column inside an already-centered pane reads as a misaligned card.
#
# Transparent rather than BG so the page inherits whatever the shell's
# background is, instead of painting its own identical-today copy of it.
EMBED_STYLE = (
    "body{background:transparent}"
    ".wrap{max-width:none;margin:0;padding:0}"
    # First-child margin would push the content away from its own page header.
    ".wrap>:first-child{margin-top:0}"
)

# The AxonLLM mark, from the dashboard's sidebar-logo. Inlined rather than
# fetched from /admin/static so these pages render standalone -- the pricing and
# readiness pages are the ones an operator opens when something is already
# wrong, and they should not depend on another route answering.
_MARK = (
    '<svg viewBox="0 0 32 32" fill="none" aria-hidden="true">'
    '<rect width="32" height="32" rx="8" fill="url(#axon-grad)"/>'
    '<path d="M8 22L16 10L24 22" stroke="white" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="16" cy="10" r="2.5" fill="white"/>'
    '<circle cx="8" cy="22" r="2" fill="white" opacity="0.7"/>'
    '<circle cx="24" cy="22" r="2" fill="white" opacity="0.7"/>'
    '<line x1="12" y1="16" x2="20" y2="16" stroke="white" stroke-width="1.5" '
    'opacity="0.5"/>'
    '<defs><linearGradient id="axon-grad" x1="0" y1="0" x2="32" y2="32">'
    '<stop stop-color="#8b5cf6"/><stop offset="1" stop-color="#6d28d9"/>'
    '</linearGradient></defs></svg>'
)

# Favicon: the same mark, flattened to one violet. A data URI so there is no
# second request and nothing to 404.
FAVICON = (
    '<link rel="icon" href="data:image/svg+xml,'
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='8' fill='%237c3aed'/%3E"
    "%3Cpath d='M8 22L16 10L24 22' stroke='white' stroke-width='2.5' "
    "stroke-linecap='round' stroke-linejoin='round' fill='none'/%3E"
    "%3Ccircle cx='16' cy='10' r='2.5' fill='white'/%3E%3C/svg%3E\">"
)


def ribbon(title: str, *extra_links: tuple[str, str]) -> str:
    """The shared top ribbon: AxonLLM mark, page title, then actions.

    The mark links to the landing page and "Dashboard" is always the last,
    filled action, so every report page has the same way back. `extra_links`
    are rendered before it as outlined secondary links.
    """
    parts = [
        '<div class="toolbar">',
        f'<a class="brand" href="/">{_MARK}<b>AxonLLM</b></a>',
        f'<span class="title">{title}</span>',
    ]
    for href, label in extra_links:
        parts.append(f'<a class="quiet" href="{href}">{label}</a>')
    parts.append('<a class="action" href="/admin/dashboard">Dashboard &rarr;</a>')
    parts.append("</div>")
    return "".join(parts)
