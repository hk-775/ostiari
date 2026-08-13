"""Database-backed restoration with one-time state.json compatibility import."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.env import data_dir
from control_plane.models.database import DEFAULT_ORG, RuntimeStateRecord
from control_plane.services.runtime_state import (
    ensure_runtime_sequence,
    load_all_runtime_state,
    put_runtime_state,
)

log = logging.getLogger("control_plane.persistence")

# Retained only to migrate installations that predate runtime_state_records.
STATE_FILE = data_dir() / "state.json"
_MIGRATION_NAMESPACE = "_migration"
_LEGACY_IMPORT_KEY = "legacy_state_import"


def save_state(state: dict[str, Any]) -> None:
    """Legacy helper retained for old tooling; the application no longer calls it."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    log.warning("Wrote legacy state file %s", STATE_FILE)


def load_state() -> dict[str, Any]:
    """Read legacy state for a one-time database import."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read legacy state file {STATE_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Legacy state file {STATE_FILE} must contain a JSON object")
    return data


def _org_of(value: dict[str, Any]) -> str:
    return str(value.get("_org") or DEFAULT_ORG)


def _without_org(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "_org"}


async def import_legacy_state(
    db: AsyncSession,
    state: dict[str, Any],
) -> bool:
    """Import state.json only when the durable runtime store is empty."""
    if not state:
        return False
    count = (
        await db.execute(select(func.count()).select_from(RuntimeStateRecord))
    ).scalar_one()
    if count:
        log.info("Ignoring legacy state file because durable runtime state exists")
        return False

    list_namespaces = {
        "quotas": ("quotas", "id"),
        "budget_alerts": ("budget_alerts", None),
        "experiments": ("experiments", "name"),
        "models": ("models", "name"),
        "agents": ("agents", "name"),
        "providers": ("providers", "name"),
    }
    for legacy_name, (namespace, key_field) in list_namespaces.items():
        values = state.get(legacy_name, [])
        if not isinstance(values, list):
            raise RuntimeError(f"Legacy state field '{legacy_name}' must be a list")
        for index, raw in enumerate(values):
            if not isinstance(raw, dict):
                raise RuntimeError(
                    f"Legacy state field '{legacy_name}' contains a non-object"
                )
            org = _org_of(raw)
            value = _without_org(raw)
            if key_field is None:
                timestamp = float(value.get("timestamp") or 0.0)
                item_key = f"{timestamp:020.6f}:{index:08d}"
            else:
                if key_field not in value:
                    raise RuntimeError(
                        f"Legacy state field '{legacy_name}' is missing '{key_field}'"
                    )
                item_key = str(value[key_field])
            await put_runtime_state(db, org, namespace, item_key, value)

    routing = state.get("agent_routing", [])
    if not isinstance(routing, list):
        raise RuntimeError("Legacy state field 'agent_routing' must be a list")
    for raw in routing:
        if not isinstance(raw, dict):
            raise RuntimeError("Legacy agent_routing contains a non-object")
        org = _org_of(raw)
        value = _without_org(raw)
        item_key = f"{value['gateway_id']}:{value['agent_id']}"
        await put_runtime_state(db, org, "agent_routing", item_key, value)

    await _import_org_config(
        db,
        state.get("roi_cost_model"),
        "roi_cost_model",
    )
    await _import_org_config(
        db,
        state.get("token_broker_config"),
        "token_broker_config",
    )
    await put_runtime_state(
        db,
        DEFAULT_ORG,
        _MIGRATION_NAMESPACE,
        _LEGACY_IMPORT_KEY,
        {"source": STATE_FILE.name},
    )
    await db.commit()
    log.info("Imported legacy runtime state from %s", STATE_FILE)
    return True


async def _import_org_config(
    db: AsyncSession,
    raw: Any,
    namespace: str,
) -> None:
    if not raw:
        return
    if not isinstance(raw, dict):
        raise RuntimeError(f"Legacy state field '{namespace}' must be an object")
    singleton_fields = {
        "roi_cost_model": {"entries", "fallback"},
        "token_broker_config": {"bulk_discount", "markup", "_customized"},
    }[namespace]
    if singleton_fields.intersection(raw):
        await put_runtime_state(db, DEFAULT_ORG, namespace, "config", raw)
        return
    for org, value in raw.items():
        if not isinstance(value, dict):
            raise RuntimeError(f"Legacy state field '{namespace}.{org}' must be an object")
        await put_runtime_state(db, str(org), namespace, "config", value)


async def load_runtime_caches(db: AsyncSession) -> None:
    """Validate and rebuild hot caches from durable runtime records."""
    rows = await load_all_runtime_state(db)
    grouped: dict[tuple[str, str], list[RuntimeStateRecord]] = defaultdict(list)
    for row in rows:
        grouped[(row.org_id, row.namespace)].append(row)

    from control_plane.routers.agent_routing import RoutingPolicy, _policies
    from control_plane.routers.agents import AgentConfig, _agents
    from control_plane.routers.experiments import ExperimentResponse, _experiments
    from control_plane.routers.model_config import ModelConfig, _models
    from control_plane.routers.payments import PricingConfig, _pricing
    from control_plane.routers.providers import _ProviderRecord, _providers
    from control_plane.routers.quotas import BudgetAlert, QuotaResponse, _alerts, _next_id, _quotas
    from control_plane.routers.roi import _cost_model
    from control_plane.routers.token_broker import _config as broker_config
    from control_plane.routers.trust import _enforced

    for (org, namespace), records in grouped.items():
        try:
            if namespace == "agents":
                _agents[org].clear()
                for row in records:
                    agent = AgentConfig(**row.value)
                    _agents[org][agent.name] = agent
            elif namespace == "experiments":
                _experiments[org].clear()
                for row in records:
                    experiment = ExperimentResponse(**row.value)
                    _experiments[org][experiment.name] = experiment
            elif namespace == "models":
                _models[org].clear()
                for row in records:
                    model = ModelConfig(**row.value)
                    _models[org][model.name] = model
            elif namespace == "agent_routing":
                for key in [key for key in _policies if key[0] == org]:
                    del _policies[key]
                for row in records:
                    policy = RoutingPolicy(**row.value)
                    _policies[(org, policy.gateway_id, policy.agent_id)] = policy
            elif namespace == "providers":
                _providers[org].clear()
                for row in records:
                    provider = _ProviderRecord(**row.value)
                    _providers[org][provider.name] = provider
            elif namespace == "quotas":
                _quotas[org].clear()
                for row in records:
                    quota = QuotaResponse(**row.value)
                    _quotas[org][quota.id] = quota
                next_value = max(_quotas[org], default=0) + 1
                _next_id[org] = next_value
                await ensure_runtime_sequence(db, org, "quotas", next_value)
            elif namespace == "budget_alerts":
                _alerts[org].clear()
                for row in records:
                    _alerts[org].append(BudgetAlert(**row.value))
            elif namespace == "roi_cost_model":
                _cost_model[org].update(records[-1].value)
            elif namespace == "token_broker_config":
                broker_config[org].update(records[-1].value)
            elif namespace == "payment_pricing":
                _pricing[org].clear()
                for row in records:
                    pricing = PricingConfig(**row.value)
                    _pricing[org][row.item_key] = pricing.model_dump()
            elif namespace == "trust_enforced":
                _enforced[org].clear()
                for row in records:
                    _enforced[org][row.item_key] = bool(
                        row.value.get("enforced", False)
                    )
            elif namespace == "gateway_config_queue":
                # Drained directly from SQL by registration/heartbeat.
                continue
        except Exception as exc:
            raise RuntimeError(
                "Invalid durable runtime state "
                f"{org}/{namespace}/{records[0].item_key}: {exc}"
            ) from exc
    await db.commit()
