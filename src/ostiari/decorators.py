"""@protect decorator and module-level Guard singleton management."""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from typing import Any

from ostiari.guard import Guard
from ostiari.models import OstiariConfig

_guard_lock = threading.Lock()
_guard_instance: Guard | None = None


def _get_or_create_guard() -> Guard:
    global _guard_instance
    if _guard_instance is not None:
        return _guard_instance
    with _guard_lock:
        if _guard_instance is None:
            instance = Guard()
            instance.start()
            _guard_instance = instance
    return _guard_instance


def init(config: OstiariConfig | None = None, **kwargs: Any) -> Guard:
    global _guard_instance
    with _guard_lock:
        if _guard_instance is not None:
            _guard_instance.shutdown()
        instance = Guard(config=config, **kwargs)
        instance.start()
        _guard_instance = instance
    return _guard_instance


def get_guard() -> Guard | None:
    return _guard_instance


def reset_guard() -> None:
    global _guard_instance
    with _guard_lock:
        if _guard_instance is not None:
            _guard_instance.shutdown()
            _guard_instance = None


def protect(
    risk: str | None = None,
    confirm: bool = False,
    policy: str | None = None,
) -> Any:
    def decorator(fn: Any) -> Any:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                guard = _get_or_create_guard()
                params = _build_params(fn, args, kwargs)
                context = _build_context(risk, confirm, policy)
                await guard.avalidate(action=fn.__name__, params=params, context=context)
                return await fn(*args, **kwargs)

            return async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                guard = _get_or_create_guard()
                params = _build_params(fn, args, kwargs)
                context = _build_context(risk, confirm, policy)
                guard.validate(action=fn.__name__, params=params, context=context)
                return fn(*args, **kwargs)

            return sync_wrapper

    return decorator


def _build_params(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return {"args": list(args), "kwargs": kwargs}


def _build_context(risk: str | None, confirm: bool, policy: str | None) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    if risk is not None:
        ctx["risk_hint"] = risk
    if confirm:
        ctx["force_intervene"] = True
    if policy is not None:
        ctx["policy_name"] = policy
    return ctx
