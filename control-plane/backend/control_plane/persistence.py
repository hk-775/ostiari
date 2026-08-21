"""Database-backed restoration with one-time state.json compatibility import."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.env import data_dir
from control_plane.models.database import DEFAULT_ORG, RuntimeStateRecord
from control_plane.services.runtime_state import (
    ensure_runtime_sequence,
    load_all_runtime_state,
    load_runtime_namespace,
    load_runtime_revisions,
    put_runtime_state,
)

log = logging.getLogger("control_plane.persistence")

# Retained only to migrate installations that predate runtime_state_records.
STATE_FILE = data_dir() / "state.json"
_MIGRATION_NAMESPACE = "_migration"
_LEGACY_IMPORT_KEY = "legacy_state_import"
_loaded_runtime_revisions: dict[tuple[str, str], int] = {}
_runtime_sync_task: asyncio.Task[None] | None = None
_runtime_sync_error = ""


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


async def _replace_runtime_namespace(
    db: AsyncSession,
    org: str,
    namespace: str,
    records: list[RuntimeStateRecord],
) -> None:
    """Atomically replace one process-local namespace from authoritative SQL."""
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

    try:
        if namespace == "agents":
            agent_replacement = {
                agent.name: agent
                for row in records
                if (agent := AgentConfig(**row.value))
            }
            _agents[org].clear()
            _agents[org].update(agent_replacement)
        elif namespace == "experiments":
            experiment_replacement = {
                experiment.name: experiment
                for row in records
                if (experiment := ExperimentResponse(**row.value))
            }
            _experiments[org].clear()
            _experiments[org].update(experiment_replacement)
        elif namespace == "models":
            model_replacement = {
                model.name: model
                for row in records
                if (model := ModelConfig(**row.value))
            }
            _models[org].clear()
            _models[org].update(model_replacement)
        elif namespace == "agent_routing":
            routing_replacement: dict[tuple[str, str, str], RoutingPolicy] = {}
            for row in records:
                policy = RoutingPolicy(**row.value)
                routing_replacement[(org, policy.gateway_id, policy.agent_id)] = policy
            for key in [key for key in _policies if key[0] == org]:
                del _policies[key]
            _policies.update(routing_replacement)
        elif namespace == "providers":
            provider_replacement = {
                provider.name: provider
                for row in records
                if (provider := _ProviderRecord(**row.value))
            }
            _providers[org].clear()
            _providers[org].update(provider_replacement)
        elif namespace == "quotas":
            quota_replacement: dict[int, QuotaResponse] = {}
            for row in records:
                quota = QuotaResponse(**row.value)
                quota_replacement[quota.id] = quota
            _quotas[org].clear()
            _quotas[org].update(quota_replacement)
            next_value = max(_quotas[org], default=0) + 1
            _next_id[org] = next_value
            await ensure_runtime_sequence(db, org, "quotas", next_value)
        elif namespace == "budget_alerts":
            alert_replacement = [
                BudgetAlert(**row.value)
                for row in sorted(
                    records,
                    key=lambda item: (
                        float((item.value or {}).get("timestamp") or 0.0),
                        item.item_key,
                    ),
                )
            ]
            _alerts[org].clear()
            _alerts[org].extend(alert_replacement)
        elif namespace == "roi_cost_model":
            _cost_model[org].clear()
            _cost_model[org].update(
                records[-1].value
                if records
                else {"entries": None, "fallback": 1000.0}
            )
        elif namespace == "token_broker_config":
            from control_plane import token_broker

            broker_config[org].clear()
            broker_config[org].update(
                records[-1].value
                if records
                else {
                    "bulk_discount": token_broker.DEFAULT_BULK_DISCOUNT,
                    "markup": token_broker.DEFAULT_MARKUP,
                }
            )
        elif namespace == "payment_pricing":
            pricing_replacement: dict[str, dict[str, Any]] = {}
            for row in records:
                pricing = PricingConfig(**row.value)
                pricing_replacement[row.item_key] = pricing.model_dump()
            _pricing[org].clear()
            _pricing[org].update(pricing_replacement)
        elif namespace == "trust_enforced":
            trust_replacement = {
                row.item_key: bool(row.value.get("enforced", False))
                for row in records
            }
            _enforced[org].clear()
            _enforced[org].update(trust_replacement)
        elif namespace == "gateway_config_queue":
            # Drained directly from SQL by registration/heartbeat.
            return
    except Exception as exc:
        item_key = records[0].item_key if records else "<empty>"
        raise RuntimeError(
            f"Invalid durable runtime state {org}/{namespace}/{item_key}: {exc}"
        ) from exc


async def refresh_runtime_namespace(
    db: AsyncSession,
    org: str,
    namespace: str,
) -> None:
    values = await load_runtime_namespace(db, org, namespace)
    records = [
        RuntimeStateRecord(
            org_id=org,
            namespace=namespace,
            item_key=item_key,
            value=value,
        )
        for item_key, value in values.items()
    ]
    await _replace_runtime_namespace(db, org, namespace, records)


async def sync_runtime_caches(db: AsyncSession) -> int:
    """Apply every committed namespace revision this replica has not seen."""
    revisions = await load_runtime_revisions(db)
    changed = [
        (org, namespace, revision)
        for (org, namespace), revision in revisions.items()
        if _loaded_runtime_revisions.get((org, namespace)) != revision
    ]
    for org, namespace, revision in changed:
        await refresh_runtime_namespace(db, org, namespace)
        _loaded_runtime_revisions[(org, namespace)] = revision
    await db.commit()
    return len(changed)


async def load_runtime_caches(db: AsyncSession) -> None:
    """Validate and rebuild hot caches from durable runtime records."""
    rows = await load_all_runtime_state(db)
    grouped: dict[tuple[str, str], list[RuntimeStateRecord]] = defaultdict(list)
    for row in rows:
        grouped[(row.org_id, row.namespace)].append(row)
    revisions = await load_runtime_revisions(db)
    for org, namespace in set(grouped) | set(revisions):
        await _replace_runtime_namespace(
            db,
            org,
            namespace,
            grouped.get((org, namespace), []),
        )
    _loaded_runtime_revisions.clear()
    _loaded_runtime_revisions.update(revisions)
    await db.commit()


async def _runtime_cache_sync_loop() -> None:
    global _runtime_sync_error
    interval = max(
        0.25,
        float(os.environ.get("OSTIARI_RUNTIME_SYNC_INTERVAL_SECONDS", "1")),
    )
    while True:
        try:
            from control_plane.database import async_session
            from control_plane.routers.traces import load_recent_trace_cache

            async with async_session() as db:
                await sync_runtime_caches(db)
                await load_recent_trace_cache(db)
            _runtime_sync_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - readiness exposes sync failure
            _runtime_sync_error = str(exc)
            log.exception("Runtime cache synchronization failed")
        await asyncio.sleep(interval)


def start_runtime_cache_sync() -> None:
    global _runtime_sync_task
    if _runtime_sync_task is None or _runtime_sync_task.done():
        _runtime_sync_task = asyncio.create_task(_runtime_cache_sync_loop())


async def stop_runtime_cache_sync() -> None:
    global _runtime_sync_task
    if _runtime_sync_task is None:
        return
    _runtime_sync_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _runtime_sync_task
    _runtime_sync_task = None


def runtime_cache_sync_error() -> str:
    return _runtime_sync_error
