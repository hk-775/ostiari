"""FastAPI dependency injection — provides shared instances to route handlers."""

from __future__ import annotations

from typing import Any

from ostiari.dashboard.cache import QueryCache
from ostiari.dashboard.intervention import InterventionBroker
from ostiari.dashboard.services.agents import AgentService
from ostiari.dashboard.services.stats import StatsService
from ostiari.dashboard.storage_async import AsyncStorageWrapper
from ostiari.health import HealthChecker
from ostiari.report import ReportGenerator

_state: dict[str, Any] = {}


def init_dependencies(
    storage: AsyncStorageWrapper,
    cache: QueryCache,
    intervention: InterventionBroker | None,
    raw_storage: Any,
) -> None:
    _state["storage"] = storage
    _state["cache"] = cache
    _state["intervention"] = intervention
    _state["stats_service"] = StatsService(storage)
    _state["agent_service"] = AgentService(storage)
    _state["health_checker"] = HealthChecker(storage=raw_storage)
    _state["report_generator"] = ReportGenerator(raw_storage)


def get_storage() -> AsyncStorageWrapper:
    return _state["storage"]


def get_cache() -> QueryCache:
    return _state["cache"]


def get_stats_service() -> StatsService:
    return _state["stats_service"]


def get_agent_service() -> AgentService:
    return _state["agent_service"]


def get_health_checker() -> HealthChecker:
    return _state["health_checker"]


def get_report_generator() -> ReportGenerator:
    return _state["report_generator"]


def get_intervention_broker() -> InterventionBroker | None:
    return _state.get("intervention")
