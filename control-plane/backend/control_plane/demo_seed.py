"""Idempotent demo-data seeding for DB- and memory-backed resources.

The trace buffer is seeded in routers/traces.py. This covers metering (usage
records in the DB), A/B experiments (in-memory), and MCP server records (DB) so
those pages are populated on a fresh start. Runs from the app lifespan; only
seeds when empty, so it never duplicates or clobbers real data.

The seeded MCP servers are *real* stdio servers (draw.io + filesystem, run via
npx). The DB record is only the config; the crm-agent gateway actually spawns
the subprocess and discovers tools — at startup (from llm-gateway-config.yaml)
or when register_demo_mcp.py pushes them to a running gateway.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models.database import (
    Gateway,
    McpServer,
    PaymentRecord,
    Policy,
    Tool,
    UsageRecord,
    Wallet,
)

log = logging.getLogger("control_plane.demo_seed")

_AGENTS_VOL = {
    "coder-agent": 200, "research-agent": 150, "analytics-agent": 90,
    "ops-agent": 70, "support-agent": 55, "db-agent": 40,
    "planner-agent": 30, "payments-agent": 12,
}
_MODELS = ["claude-haiku", "gpt-4o-mini", "claude-sonnet", "gpt-4o"]
_TOOLS = ["web_search", "db_query", "send_email", "github.search_code", "file_read"]
_DEMO_GATEWAYS = (
    ("crm-agent", "CRM Agent"),
    ("ops-agent", "Operations Agent"),
    ("devops-agent", "DevOps Agent"),
    ("analytics-agent", "Analytics Agent"),
)


def _tool_schema(*required: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in required},
        "required": list(required),
    }


_DEMO_TOOL_DEFINITIONS = (
    ("web_search", "web_search", "Search the web for information.", _tool_schema("query")),
    ("db_query", "db_query", "Run a read-only SQL query.", _tool_schema("sql")),
    ("send_email", "send_email", "Send an email.", _tool_schema("to", "subject", "body")),
    ("github.list_repos", "github.list_repos", "List GitHub repositories.", _tool_schema()),
    ("github.search_code", "github.search_code", "Search source code.", _tool_schema("query")),
    ("github.create_issue", "github.create_issue", "Create a GitHub issue.",
     _tool_schema("repo", "title", "body")),
    ("drawio.list_diagrams", "drawio.list_diagrams", "List diagrams.", _tool_schema()),
    ("drawio.create_diagram", "drawio.create_diagram", "Create a diagram.",
     _tool_schema("name")),
    ("drawio.add_shape", "drawio.add_shape", "Add a shape to a diagram.",
     _tool_schema("diagram_id", "shape", "label")),
    ("db_delete", "db_delete", "Delete rows from a database table.", _tool_schema("table")),
    ("github.delete_repo", "github.delete_repo", "Delete a GitHub repository.",
     _tool_schema("repo")),
    ("drawio.delete_diagram", "drawio.delete_diagram", "Delete a diagram.",
     _tool_schema("id")),
    ("premium_search", "premium_search", "Run a paid premium search.", _tool_schema("query")),
    ("market_data.fetch", "premium_search", "Fetch paid market data.", _tool_schema("query")),
)


async def seed_demo_gateways_and_tools(db: AsyncSession) -> None:
    """Seed FK-safe gateway rows and functional HTTP demo tools."""
    from control_plane.models.database import DEFAULT_ORG

    gateway_url = os.environ.get(
        "OSTIARI_DEMO_GATEWAY_URL", "http://localhost:8421"
    ).rstrip("/")
    tools_url = os.environ.get(
        "OSTIARI_DEMO_TOOLS_URL", "http://localhost:9300"
    ).rstrip("/")

    for gateway_id, name in _DEMO_GATEWAYS:
        if await db.get(Gateway, (DEFAULT_ORG, gateway_id)) is None:
            endpoint = gateway_url if gateway_id == "crm-agent" else f"http://{gateway_id}:8421"
            db.add(Gateway(
                id=gateway_id, org_id=DEFAULT_ORG, name=name, endpoint=endpoint,
                description="Seeded Ostiari demo gateway", status="registered",
            ))
    await db.flush()

    existing_tools = set((await db.execute(
        select(Tool.name).where(
            Tool.org_id == DEFAULT_ORG, Tool.gateway_id == "crm-agent",
        )
    )).scalars())
    for name, path, description, schema in _DEMO_TOOL_DEFINITIONS:
        if name not in existing_tools:
            db.add(Tool(
                org_id=DEFAULT_ORG, gateway_id="crm-agent", name=name,
                endpoint=f"{tools_url}/{path}", method="POST",
                description=description, schema_json=schema,
            ))

    policy = (await db.execute(select(Policy).where(
        Policy.org_id == DEFAULT_ORG, Policy.name == "block-destructive",
    ))).scalar_one_or_none()
    if policy is None:
        db.add(Policy(
            org_id=DEFAULT_ORG, name="block-destructive",
            description="Block destructive calls in the demo gateway",
            content={"block": ["*delete*", "*.drop", "*.destroy", "db_delete"]},
            gateway_id="crm-agent", is_active=True,
        ))
    await db.commit()
    log.info("Seeded demo gateways and HTTP tools")


# Real MCP servers to seed on crm-agent. `command` is filled in at seed time
# from the resolved npx path (portable across machines). filesystem is sandboxed
# to a scratch dir so its tools actually execute end-to-end in the demo; draw.io
# discovers 28 real tools (calls need a browser-extension bridge).
DEMO_MCP_SANDBOX = "/tmp/ostiari-mcp-sandbox"
_MCP_SERVERS = [
    {
        "name": "drawio", "prefix": "drawio",
        "npx_args": ["-y", "drawio-mcp-server"],
    },
    {
        "name": "filesystem", "prefix": "fs",
        "npx_args": ["-y", "@modelcontextprotocol/server-filesystem", DEMO_MCP_SANDBOX],
    },
]


def demo_mcp_specs() -> list[dict]:
    """Real MCP server configs for the demo, or [] if npx isn't available.

    Shared by the DB seeder and register_demo_mcp.py so both agree on the
    exact command. Returns stdio configs with an absolute npx path.
    """
    npx = shutil.which("npx")
    if not npx:
        return []
    return [
        {
            "name": s["name"], "mode": "stdio", "prefix": s["prefix"],
            "command": [npx, *s["npx_args"]],
        }
        for s in _MCP_SERVERS
    ]


async def seed_demo_mcp(db: AsyncSession, gateway_id: str = "crm-agent") -> None:
    """Seed real MCP server records (idempotent; skips if any already exist)."""
    existing = (await db.execute(select(func.count()).select_from(McpServer))).scalar_one()
    if existing:
        return

    specs = demo_mcp_specs()
    if not specs:
        log.info("npx not found — skipping MCP server seed (install Node.js for the MCP demo)")
        return

    for spec in specs:
        db.add(McpServer(
            name=spec["name"], mode="stdio", prefix=spec["prefix"],
            command=spec["command"], gateway_id=gateway_id,
        ))
    await db.commit()
    log.info("Seeded %d real MCP server records (%s)", len(specs),
             ", ".join(s["name"] for s in specs))


async def seed_demo_db(db: AsyncSession) -> None:
    """Populate DB-backed demo data; idempotent (skips if already present)."""
    # Ensure the default org exists so org_id FKs resolve even when the admin
    # seed path (auth/router._seed_admin) hasn't run yet.
    from control_plane.models.database import DEFAULT_ORG, Organization
    if await db.get(Organization, DEFAULT_ORG) is None:
        db.add(Organization(id=DEFAULT_ORG, name="Default Organization"))
        await db.commit()

    await seed_demo_gateways_and_tools(db)
    await seed_demo_mcp(db)
    await seed_demo_payments(db)

    existing = (await db.execute(select(func.count()).select_from(UsageRecord))).scalar_one()
    if existing:
        return

    rnd = random.Random(42)
    now = datetime.now(timezone.utc)
    n = 0
    for agent, vol in _AGENTS_VOL.items():
        for _ in range(vol):
            db.add(UsageRecord(
                gateway_id=rnd.choice(["crm-agent", "ops-agent", "devops-agent", "analytics-agent"]),
                agent_id=agent, model=rnd.choice(_MODELS),
                input_tokens=rnd.randint(20, 600), output_tokens=rnd.randint(10, 600),
                total_tokens=rnd.randint(40, 1200), cost_usd=round(rnd.uniform(0.0003, 0.03), 5),
                action=rnd.choice(_TOOLS),
                timestamp=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 20)),
            ))
            n += 1
    await db.commit()
    log.info("Seeded %d usage records for metering demo", n)


# Demo wallets: varied balances so the payments page tells a story — a nearly
# drained agent (blocks), a couple capped, the rest flush.
_WALLETS = [
    {"agent_id": "research-agent", "balance_usdc": 4.80, "daily_limit_usdc": 5.0},
    {"agent_id": "coder-agent", "balance_usdc": 9.20},
    {"agent_id": "analytics-agent", "balance_usdc": 2.15, "per_call_limit_usdc": 0.05},
    {"agent_id": "ops-agent", "balance_usdc": 6.00},
    {"agent_id": "support-agent", "balance_usdc": 1.10},
    {"agent_id": "db-agent", "balance_usdc": 0.80},
    {"agent_id": "planner-agent", "balance_usdc": 3.40},
    {"agent_id": "payments-agent", "balance_usdc": 0.002},  # nearly drained — blocks
]
_PAID_TOOLS = ["premium_search", "market_data.fetch", "enrichment.lookup", "geocode.resolve"]


async def seed_demo_payments(db: AsyncSession) -> None:
    """Seed x402 wallets + a ledger history (idempotent; skips if wallets exist)."""
    existing = (await db.execute(select(func.count()).select_from(Wallet))).scalar_one()
    if existing:
        return

    now = datetime.now(timezone.utc)
    for w in _WALLETS:
        db.add(Wallet(
            agent_id=w["agent_id"], balance_usdc=w["balance_usdc"],
            daily_limit_usdc=w.get("daily_limit_usdc"),
            per_call_limit_usdc=w.get("per_call_limit_usdc"),
            spent_today_usdc=round(w["balance_usdc"] * 0.0, 4),
        ))

    # Ledger history: mostly settled micropayments, a few blocked (drained agent).
    rnd = random.Random(7)
    n = 0
    funded = [w["agent_id"] for w in _WALLETS if w["balance_usdc"] > 0.5]
    for i in range(60):
        agent = rnd.choice(funded)
        amount = rnd.choice([0.005, 0.002, 0.01, 0.005])
        db.add(PaymentRecord(
            agent_id=agent, gateway_id="crm-agent", action=rnd.choice(_PAID_TOOLS),
            amount_usdc=amount, settled=True, tx_hash=f"sim-seed-{i}",
            mode="simulated", source="tool_402",
            timestamp=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 7)),
        ))
        n += 1
    # A handful of blocked attempts from the drained agent.
    for _i in range(5):
        db.add(PaymentRecord(
            agent_id="payments-agent", gateway_id="crm-agent",
            action="premium_search", amount_usdc=0.005, settled=False,
            mode="simulated", source="tool_402",
            timestamp=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 3)),
        ))
        n += 1
    await db.commit()
    log.info("Seeded %d wallets and %d payment records", len(_WALLETS), n)


def seed_demo_pricing() -> None:
    """Set crm-agent's payment mode to passthrough (native x402) for the demo.

    Pricing lives in the payments router's in-memory policy; set it only if
    unset so a real configuration isn't overwritten.
    """
    from control_plane.models.database import DEFAULT_ORG
    from control_plane.routers.payments import _pricing

    if "crm-agent" in _pricing[DEFAULT_ORG]:
        return
    _pricing[DEFAULT_ORG]["crm-agent"] = {"mode": "passthrough", "default": 0.0, "overrides": {}}
    log.info("Seeded demo payment pricing (crm-agent: passthrough)")


# Approvals demo data. The four pending calls are the *same* four intervene-tier
# calls seeded into the trace buffer (routers/traces.py), with matching agent,
# action, params, and score — that page and this one describe one fleet, so an
# operator who clicks from a paused trace to the approval queue must find the
# call it paused, not a different set of fictional ones.
#
# Scores are not hand-picked: each is the sum of its signals below, and the
# reason string is what ostiari.explain._summarize() produces from them. That
# keeps the queue honest — every row is one the real scorer could have emitted,
# and the intervene band (allow_max 30 < score <= intervene_max 70) holds by
# construction rather than by luck.
_APPROVAL_SIGNALS: dict[str, list[tuple[str, int, str]]] = {
    "file_write": [
        ("policy", 30, "Filesystem write outside the agent's sandbox path"),
        ("anomaly:rate", 18, "12 writes in 60s — 4x this agent's baseline"),
    ],
    "execute_code": [
        ("policy", 45, "Arbitrary code execution requires review"),
        ("anomaly:new-action", 17, "First use of execute_code by this agent"),
    ],
    "send_email": [
        ("policy", 25, "Email has moderate risk"),
        ("anomaly:sequence", 19, "Outbound email directly after a bulk read"),
    ],
    "deploy": [
        ("policy", 30, "Deployment mutates running infrastructure"),
        ("parameter-risk", 25, "Parameter risk: targets production/live"),
    ],
    "db_export": [
        ("policy", 20, "Bulk export leaves the trust boundary"),
        ("parameter-risk", 25, "Parameter risk: high volume (rows=5000)"),
    ],
}

# (agent_id, gateway_id, action, params, age_minutes). Ordered oldest first; the
# queue sorts newest-first for display, so the oldest ends up at the bottom.
_PENDING_APPROVALS = [
    ("research-agent", "crm-agent", "file_write",
     {"path": "/reports/summary.md", "bytes": 4096}, 44),
    ("bedrock-agent", "crm-agent", "execute_code",
     {"lang": "python", "lines": 34}, 27),
    ("planner-bot", "crm-agent", "send_email",
     {"to": "team@example.com", "subject": "Competitor brief ready"}, 12),
    ("ops-agent", "devops-agent", "deploy",
     {"service": "auth-service", "environment": "production"}, 3),
]

# Already-decided calls, so the "Recent decisions" table on the Approvals page
# has an audit trail instead of being hidden entirely (it renders only when a
# decision exists). One denial included: a queue where every answer was "yes"
# would not show that denying is a real outcome.
# (agent_id, gateway_id, action, params, decision, decided_by, age_minutes)
_DECIDED_APPROVALS = [
    ("ops-agent", "ops-agent", "send_email",
     {"to": "oncall@example.com", "subject": "Nightly report"},
     "approved", "dana@example.com", 96),
    ("analytics-agent", "analytics-agent", "db_export",
     {"table": "customers", "rows": 5000, "destination": "s3://acme-exports/"},
     "denied", "dana@example.com", 71),
    ("coder-agent", "crm-agent", "file_write",
     {"path": "/etc/hosts", "bytes": 128},
     "denied", "sam@example.com", 58),
    ("planner-bot", "crm-agent", "execute_code",
     {"lang": "python", "lines": 12},
     "approved", "sam@example.com", 33),
]


def _approval_reason(action: str, score: int) -> str:
    """The reason string the gateway would have stored for this call.

    The gateway sends `explain(result).summary`, so build the same explanation
    from the seeded signals rather than writing prose that only looks like it.
    """
    from types import SimpleNamespace

    from ostiari.explain import explain
    from ostiari.models import RiskSignal

    signals = [
        RiskSignal(source=src, score_contribution=pts, description=desc)
        for src, pts, desc in _APPROVAL_SIGNALS[action]
    ]
    result = SimpleNamespace(
        action=action, tier="intervene", original_tier="intervene",
        score=score, signals=signals,
    )
    return explain(result).summary


def _approval_score(action: str) -> int:
    """Total risk score for a seeded action — the sum of its signals."""
    return sum(pts for _, pts, _ in _APPROVAL_SIGNALS[action])


def seed_demo_approvals() -> None:
    """Seed the HITL approval queue: pending calls plus a decision history.

    In-memory and per-process (approvals are not in the state file), so this
    runs on every start. Idempotent by the usual rule — skips entirely once the
    queue holds anything, so a real gateway's pending approvals are never
    joined by demo rows.
    """
    from control_plane.models.database import DEFAULT_ORG
    from control_plane.routers.approvals import Approval, _pending

    if _pending[DEFAULT_ORG]:
        return

    now = datetime.now(timezone.utc)

    def _stamp(minutes: int) -> str:
        return (now - timedelta(minutes=minutes)).isoformat()

    for i, (agent, gw, action, params, age) in enumerate(_PENDING_APPROVALS):
        score = _approval_score(action)
        aid = f"apr-demo{i:04d}pend"
        _pending[DEFAULT_ORG][aid] = Approval(
            id=aid, agent_id=agent, gateway_id=gw, action=action, params=params,
            score=score, reason=_approval_reason(action, score),
            status="pending", created_at=_stamp(age),
        )

    for i, (agent, gw, action, params, decision, who, age) in enumerate(_DECIDED_APPROVALS):
        score = _approval_score(action)
        aid = f"apr-demo{i:04d}done"
        # Decided a few minutes after it was raised — a review takes a moment,
        # and a decided_at equal to created_at reads like a machine, not a human.
        _pending[DEFAULT_ORG][aid] = Approval(
            id=aid, agent_id=agent, gateway_id=gw, action=action, params=params,
            score=score, reason=_approval_reason(action, score),
            status=decision, decided_by=who,
            decided_at=_stamp(max(0, age - 4)), created_at=_stamp(age),
        )

    log.info("Seeded %d pending and %d decided approvals",
             len(_PENDING_APPROVALS), len(_DECIDED_APPROVALS))


# Gateway quotas. One per seeded gateway, with budgets chosen so each lands in a
# different state the Quotas page renders differently — red bar + warning, amber
# + warning, amber alone, green. A demo where every bar is green shows the fields
# exist but not that the limit ever bites.
#
# current_spend is NOT invented: it's summed from the UsageRecord rows already
# seeded for metering, so the Quotas page and the Metering/Costs pages agree
# about what each gateway has spent. The budget is the only chosen number, and
# it's chosen to place real spend at a particular percentage.
#
# Field names match the gateway's own QuotaConfig (rate_limit_rpm,
# budget_limit_usd, max_tokens_per_request, allowed_models), so the page's
# "Push to gateway" button sends a payload the sidecar actually accepts.
#
# The budgets are tuned against the spend the *metering seed* produces, which is
# deterministic (random.Random(42) over fixed per-agent volumes). Don't tune them
# against a long-running instance: live gateway traffic adds usage rows, which
# raises spend and quietly moves a bar into a band it won't occupy on a fresh
# start. test_budgets_span_every_band_the_page_renders pins this to the seed.
# (name, gateway_id, rate_limit_rpm, budget_limit_usd, max_tokens, target_pct)
_GATEWAY_QUOTAS = [
    ("CRM production cap", "crm-agent", 120, 2.60, 8192, 95),
    ("Analytics warehouse cap", "analytics-agent", 90, 3.00, 16384, 86),
    ("Ops fleet cap", "ops-agent", 60, 3.00, 4096, 79),
    ("DevOps deploy cap", "devops-agent", 30, 10.00, 4096, 24),
]

# Only the models the demo actually meters (see _MODELS) — an allowlist naming a
# model no usage record mentions would be unenforceable in this demo.
_QUOTA_ALLOWED_MODELS = {
    "crm-agent": ["claude-sonnet", "claude-haiku", "gpt-4o"],
    "analytics-agent": ["claude-haiku", "gpt-4o-mini"],
    "ops-agent": ["claude-haiku", "gpt-4o-mini"],
    "devops-agent": [],           # unrestricted — not every quota caps models
}


async def seed_demo_quotas(db: AsyncSession) -> None:
    """Seed one gateway-scoped quota per demo gateway (idempotent).

    Spend is read from the metering rows rather than made up, so a viewer who
    cross-checks the Costs page against a budget bar finds the same number.
    Rate limits are the configured ceiling; current_rpm stays 0 because nothing
    is driving live traffic — a nonzero RPM with an idle fleet would be a lie
    the Live Traces page immediately contradicts.
    """
    from control_plane.models.database import DEFAULT_ORG
    from control_plane.routers.quotas import QuotaResponse, _next_id, _quotas

    if _quotas[DEFAULT_ORG]:
        return

    rows = (await db.execute(
        select(UsageRecord.gateway_id, func.sum(UsageRecord.cost_usd))
        .group_by(UsageRecord.gateway_id)
    )).all()
    spend_by_gw = {gw: float(total or 0.0) for gw, total in rows}

    for name, gw, rpm, budget, max_tokens, _pct in _GATEWAY_QUOTAS:
        qid = _next_id[DEFAULT_ORG]
        _quotas[DEFAULT_ORG][qid] = QuotaResponse(
            id=qid, name=name, scope="gateway", scope_id=gw,
            rate_limit_rpm=rpm, budget_limit_usd=budget,
            max_tokens_per_request=max_tokens,
            allowed_models=_QUOTA_ALLOWED_MODELS.get(gw, []),
            current_spend=round(spend_by_gw.get(gw, 0.0), 4),
            current_rpm=0,
        )
        _next_id[DEFAULT_ORG] += 1

    log.info("Seeded %d gateway quotas (spend read from %d metered gateways)",
             len(_GATEWAY_QUOTAS), len(spend_by_gw))


# Broker token pools. The Token Broker page renders the pool inventory and the
# reconciliation ledger as tables with no empty state, so with nothing seeded
# both show a header row and nothing else.
#
# Consumption is not invented: tokens and cost are aggregated from the same
# UsageRecord rows the metering seed writes, grouped by the provider each model
# draws from (broker_pilot.provider_for), and the cost is put through the same
# retail * (1 - bulk_discount) conversion draw_down() applies. So a pool's
# consumed figures are what the draw-down path would have produced had it run
# over this usage — which is the point of a pool inventory that claims to be
# auditable.
#
# Only the *purchase* is chosen, expressed as a multiple of what was consumed so
# it stays correct if the metering volumes change. One pool is deliberately sized
# to sit under its low-water mark: `status` is computed by the same rule
# draw_down uses (remaining <= low_threshold -> depleted), so the page shows the
# depleted badge and the halt condition it exists to demonstrate, rather than two
# healthy rows that never exercise it.
# (provider, purchase_multiple, low_threshold_fraction_of_purchase)
_BROKER_POOLS = [
    ("anthropic", 2.5, 0.10),   # healthy: consumed 40% of a 2.5x buy
    ("openai", 1.05, 0.10),     # depleted: only 5% headroom left, under the 10% mark
]

# Reconciliations: the provider's invoice against what we tracked. Drift is the
# whole point of the page, and >5% is styled red, so one of each — a small
# benign delta and one big enough to be flagged. Invoices are a multiplier on the
# computed cost rather than a fixed dollar figure, for the same reason as above.
# (provider, invoice_multiple_of_computed, period_days)
_BROKER_RECONCILIATIONS = [
    ("anthropic", 1.018, 30),   # +1.8% — normal rounding/timing drift
    ("openai", 1.094, 30),      # +9.4% — over the 5% threshold, rendered red
]


async def seed_demo_broker_pools(db: AsyncSession) -> None:
    """Seed broker token pools and a reconciliation history (idempotent).

    Consumption is aggregated from the metered usage rows and converted at the
    configured bulk discount, exactly as draw_down() would have done, so the
    Token Broker page agrees with Costs and Metering instead of asserting its
    own numbers.
    """
    from control_plane.broker_pilot import provider_for
    from control_plane.models.database import DEFAULT_ORG, ReconciliationRecord, TokenPool
    from control_plane.routers.token_broker import _config as _tb_config

    if (await db.execute(select(func.count()).select_from(TokenPool))).scalar_one():
        return

    # Same conversion draw_down() applies to each usage record.
    discount = float(_tb_config[DEFAULT_ORG].get("bulk_discount", 0.0))

    consumed: dict[str, tuple[int, float]] = {}
    rows = (await db.execute(
        select(UsageRecord.model, UsageRecord.total_tokens, UsageRecord.cost_usd)
    )).all()
    for model, tokens, retail in rows:
        provider = provider_for(model)
        tok, cost = consumed.get(provider, (0, 0.0))
        consumed[provider] = (
            tok + int(tokens or 0),
            cost + float(retail or 0.0) * (1 - discount),
        )

    for provider, multiple, low_frac in _BROKER_POOLS:
        used_tokens, used_cost = consumed.get(provider, (0, 0.0))
        if not used_tokens:
            continue
        purchased = int(used_tokens * multiple)
        low = int(purchased * low_frac)
        remaining = max(0, purchased - used_tokens)
        db.add(TokenPool(
            provider=provider,
            # Explicit because org_id is half this table's primary key — a column
            # default would be filled in at flush, but the pool's identity is
            # clearer stated at the point of construction.
            org_id=DEFAULT_ORG,
            purchased_tokens=purchased,
            # Bulk cost of the whole purchase, at the same per-token rate the
            # consumed portion was charged at.
            purchased_cost_usd=round(used_cost * multiple, 6),
            consumed_tokens=used_tokens,
            consumed_cost_usd=round(used_cost, 6),
            low_threshold_tokens=low,
            # draw_down()'s own rule, so the badge matches what enforcement does.
            status="depleted" if remaining <= low else "active",
        ))

    now = datetime.now(timezone.utc)
    for provider, invoice_mult, days in _BROKER_RECONCILIATIONS:
        since = now - timedelta(days=days)
        # The route computes drift over *retail* cost for the period, not the
        # discounted pool cost — mirror that so a re-run of Reconcile agrees.
        computed = sum(
            float(retail or 0.0) for model, _tokens, retail in rows
            if provider_for(model) == provider
        )
        tokens = sum(
            int(t or 0) for model, t, _r in rows if provider_for(model) == provider
        )
        if not tokens:
            continue
        db.add(ReconciliationRecord(
            provider=provider, org_id=DEFAULT_ORG, period_start=since, period_end=now,
            computed_cost_usd=round(computed, 6),
            invoiced_cost_usd=round(computed * invoice_mult, 6),
            consumed_tokens=tokens,
        ))

    await db.commit()
    log.info("Seeded %d broker token pools and %d reconciliations",
             len(_BROKER_POOLS), len(_BROKER_RECONCILIATIONS))


def seed_demo_agents() -> None:
    """Seed the in-memory agent registry with the demo agents (idempotent)."""
    from control_plane.models.database import DEFAULT_ORG
    from control_plane.routers.agents import DEMO_AGENTS, _agents

    if _agents[DEFAULT_ORG]:
        return
    _agents[DEFAULT_ORG].update(DEMO_AGENTS)
    log.info("Seeded %d demo agents", len(_agents[DEFAULT_ORG]))


def seed_demo_experiments() -> None:
    """Seed in-memory A/B experiments (persisted via the state file on shutdown)."""
    from control_plane.models.database import DEFAULT_ORG
    from control_plane.routers.experiments import ExperimentResponse, _experiments

    if _experiments[DEFAULT_ORG]:
        return
    for name, a, b, pct, gw in [
        ("haiku-vs-sonnet", "claude-haiku", "claude-sonnet", 30, "crm-agent"),
        ("gpt4o-vs-o3", "gpt-4o", "o3", 20, "crm-agent"),
        ("cost-routing-test", "gpt-4o-mini", "claude-haiku", 50, "ops-agent"),
    ]:
        _experiments[DEFAULT_ORG][name] = ExperimentResponse(
            name=name, model_a=a, model_b=b, traffic_pct_b=pct, gateway_id=gw, is_active=True,
        )
    log.info("Seeded %d demo experiments", len(_experiments[DEFAULT_ORG]))
