"""SQLAlchemy database models."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Tenant identifier used everywhere a row/record has no explicit org yet.
# Single-org (dev/demo) deployments run entirely under this org, so behavior is
# unchanged; multi-tenant deployments assign real org ids via the auth token.
DEFAULT_ORG = "default"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Gateway(Base):
    __tablename__ = "gateways"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    endpoint: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default="registered")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    tools: Mapped[list["Tool"]] = relationship(back_populates="gateway", cascade="all, delete-orphan")


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    endpoint: Mapped[str] = mapped_column(String(512))
    method: Mapped[str] = mapped_column(String(10), default="POST")
    description: Mapped[str] = mapped_column(Text, default="")
    timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0)
    schema_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # REST param placement for tools imported from OpenAPI (nullable = older rows
    # / hand-registered tools send everything as a JSON body).
    path_params: Mapped[list | None] = mapped_column(JSON, nullable=True)
    query_params: Mapped[list | None] = mapped_column(JSON, nullable=True)
    gateway_id: Mapped[str] = mapped_column(String(64), ForeignKey("gateways.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    gateway: Mapped["Gateway"] = relationship(back_populates="tools")


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    # NOTE: name is globally unique for now — two orgs can't reuse a policy name.
    # Composite (org_id, name) uniqueness is deferred (see multi-tenancy plan).
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    gateway_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("gateways.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(20))  # embedded | remote | stdio
    # For embedded mode
    package: Mapped[str] = mapped_column(String(256), default="")
    module: Mapped[str] = mapped_column(String(256), default="")
    # For remote mode
    url: Mapped[str] = mapped_column(String(512), default="")
    # For stdio mode
    command: Mapped[list] = mapped_column(JSON, default=list)
    # Shared config
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # Tool filtering
    allowed_tools: Mapped[list | None] = mapped_column(JSON, nullable=True)
    blocked_tools: Mapped[list] = mapped_column(JSON, default=list)
    prefix: Mapped[str] = mapped_column(String(64), default="")
    # Association
    gateway_id: Mapped[str] = mapped_column(String(64), ForeignKey("gateways.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint(
            "gateway_id",
            "event_id",
            name="uq_usage_records_gateway_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    gateway_id: Mapped[str] = mapped_column(String(64), ForeignKey("gateways.id"))
    # Stable gateway-generated identity. Retries of one unconfirmed batch reuse
    # this value, so usage, pool consumption, and billing are applied once.
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(128), default="unknown")
    model: Mapped[str] = mapped_column(String(128))
    # Assignment made by Ostiari's A/B router. These stay attached to the usage
    # event even when provider fallback serves a model other than the variant's
    # configured model, so results use intention-to-treat cohorts.
    experiment_name: Mapped[str] = mapped_column(String(128), default="")
    experiment_variant: Mapped[str] = mapped_column(String(8), default="")
    # Actual provider that served the model (canonical broker pool name). Older
    # clients leave this empty and ingestion derives it from the model.
    provider: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    action: Mapped[str] = mapped_column(String(128), default="")
    broker_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    broker_charge_usd: Mapped[float] = mapped_column(Float, default=0.0)
    billing_status: Mapped[str] = mapped_column(
        String(20), default="not_applicable"
    )  # not_applicable | pending | collected | failed
    billing_ref: Mapped[str] = mapped_column(String(128), default="")
    billing_error: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class A2AAgentRecord(Base):
    """A remote A2A agent registered on a gateway, persisted so it survives a
    gateway restart. The control plane holds the record; the gateway (re)connects
    to the agent's URL and discovers its skills on startup from this.
    """

    __tablename__ = "a2a_agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128))          # display name
    agent_key: Mapped[str] = mapped_column(String(128))     # a2a.<agent_key>
    url: Mapped[str] = mapped_column(String(512))
    auth_token: Mapped[str] = mapped_column(String(512), default="")
    gateway_id: Mapped[str] = mapped_column(String(64), ForeignKey("gateways.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class TokenPool(Base):
    """Purchased token inventory for the broker, per provider.

    A pilot broker buys tokens in bulk (committed-use / prepaid) and draws down
    the pool as customer traffic consumes them. We track tokens and the dollars
    those tokens represent at our bulk cost, plus the total consumed, so the
    remaining balance and burn are auditable and depletion can halt routing.
    """

    __tablename__ = "token_pools"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)  # anthropic | openai | ...
    # Part of the primary key, not just an indexed column like every other
    # tenant table: two orgs must each be able to hold an "anthropic" pool. With
    # provider alone as the PK the second tenant to fund one would collide with
    # the first — and `db.get(TokenPool, provider)` would hand a tenant whichever
    # row happened to exist, then draw *their* traffic down against it.
    # Not nullable, for the same reason: a NULL in a PK column is not a row you
    # can look up, and pool identity has to be exact.
    org_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=DEFAULT_ORG, index=True, nullable=False
    )
    purchased_tokens: Mapped[int] = mapped_column(Integer, default=0)
    purchased_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)  # our bulk cost for them
    consumed_tokens: Mapped[int] = mapped_column(Integer, default=0)
    consumed_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)   # our-cost of consumption
    low_threshold_tokens: Mapped[int] = mapped_column(Integer, default=0)  # alert/halt below this
    status: Mapped[str] = mapped_column(String(20), default="active")      # active | depleted
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ReconciliationRecord(Base):
    """A period reconciliation: our computed pool cost vs the provider's invoice.

    The operator enters the provider's actual billed amount for a period; we
    compare it to what our consumption tracking computed, and record the delta
    (drift). Persistent audit trail — this is where a broker catches the pool
    math diverging from the real bill.
    """

    __tablename__ = "reconciliation_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    provider: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    computed_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)   # what we tracked
    invoiced_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)   # provider's actual bill
    consumed_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Wallet(Base):
    """Per-agent USDC wallet for the x402 payment gate.

    The control plane is the source of truth for balances/limits and pushes
    them to gateways. `spent_today_usdc` drives the daily-cap auto-pause. The
    address is empty in simulated mode and holds the agent's Base address when
    live; private keys never live here (KMS/secrets manager).
    """

    __tablename__ = "wallets"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    address: Mapped[str] = mapped_column(String(128), default="")
    balance_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    daily_limit_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    per_call_limit_usdc: Mapped[float | None] = mapped_column(Float, nullable=True)
    spent_today_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | paused
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class PaymentRecord(Base):
    """Ledger of x402 charges — the billing/audit trail and dashboard source."""

    __tablename__ = "payment_records"
    __table_args__ = (
        UniqueConstraint(
            "gateway_id",
            "event_id",
            name="uq_payment_records_gateway_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    # Generated by the gateway once and retained across reporter retries.
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(128), default="unknown")
    gateway_id: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(128), default="")
    amount_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    settled: Mapped[bool] = mapped_column(Boolean, default=True)
    wallet_debited: Mapped[bool] = mapped_column(Boolean, default=False)
    tx_hash: Mapped[str] = mapped_column(String(128), default="")
    mode: Mapped[str] = mapped_column(String(20), default="simulated")  # simulated | live
    source: Mapped[str] = mapped_column(String(20), default="policy")  # policy | tool_402
    reason: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(64), default=DEFAULT_ORG, index=True, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # Tamper-evidence: each row hashes its own canonical content together with
    # the previous row's entry_hash, forming a chain. Altering or removing any
    # row breaks every subsequent hash. Nullable for pre-existing rows.
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
