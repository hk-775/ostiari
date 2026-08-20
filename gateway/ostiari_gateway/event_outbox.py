"""Crash-safe gateway event queues backed by the shared Redis store."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol


def scoped_stream(name: str, owner: str) -> str:
    """Return a secret-free stream name isolated to one gateway identity."""
    if not name or ":" in name:
        raise ValueError("outbox stream must be a non-empty name without ':'")
    identity = owner.strip() or "unconfigured"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{name}.{digest}"


@dataclass(frozen=True)
class PendingEvent:
    """One event awaiting a confirmed control-plane commit."""

    receipt: str
    event_id: str
    payload: dict[str, Any]
    durable: bool


class OutboxStore(Protocol):
    """Minimum shared-store contract required by the durable outbox."""

    @property
    def required(self) -> bool: ...

    def outbox_enqueue(
        self,
        stream: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> bool: ...

    def outbox_read(
        self,
        stream: str,
        *,
        count: int = 100,
    ) -> list[tuple[str, str, dict[str, Any]]] | None: ...

    def outbox_ack(self, stream: str, receipts: list[str]) -> bool: ...

    def outbox_depth(self, stream: str) -> int | None: ...


class EventOutbox:
    """Queue events durably when Redis is available, in memory otherwise.

    Production requires Redis through ``shared_store_required()``. The memory
    queue remains for local development and as a best-effort buffer while a
    configured Redis endpoint is temporarily unavailable. A failed durable
    operation marks the shared store unhealthy, which makes production
    readiness fail closed.
    """

    def __init__(
        self,
        stream: str,
        *,
        id_field: str,
        memory: list[dict[str, Any]] | None = None,
    ) -> None:
        if not stream or ":" in stream:
            raise ValueError("outbox stream must be a non-empty name without ':'")
        self._stream = stream
        self._id_field = id_field
        self._memory = memory if memory is not None else []
        self._store: OutboxStore | None = None
        self._errors: dict[str, str] = {}

    def attach_store(self, store: OutboxStore) -> None:
        """Attach the shared store and migrate any pre-existing memory events."""
        self._store = store
        self._migrate_memory()

    def rebind(self, stream: str) -> None:
        """Move an empty outbox to a new immutable owner identity."""
        if stream == self._stream:
            return
        depth = self.depth()
        if depth is None:
            raise RuntimeError("cannot verify empty outbox before changing gateway identity")
        if depth:
            raise RuntimeError("cannot change gateway identity with pending events")
        self._stream = stream

    def enqueue(self, payload: dict[str, Any]) -> bool:
        """Persist an event before delivery.

        Returns True when the event reached Redis. False means it remains in the
        development/best-effort memory queue.
        """
        event_id = str(payload.get(self._id_field) or "")
        if not event_id:
            raise ValueError(f"outbox event requires {self._id_field}")
        event = dict(payload)
        if self._store is not None and self._store.outbox_enqueue(self._stream, event_id, event):
            self._recover("enqueue")
            return True
        if self._store is not None:
            self._fail("enqueue", "durable enqueue failed")
        self._memory.append(event)
        return False

    def pending(self, *, count: int = 100) -> list[PendingEvent]:
        """Return the oldest pending events without removing them."""
        if count <= 0:
            return []
        self._migrate_memory()
        if self._store is not None:
            stored = self._store.outbox_read(self._stream, count=count)
            if stored:
                self._recover("read")
                return [
                    PendingEvent(
                        receipt=receipt,
                        event_id=event_id,
                        payload=payload,
                        durable=True,
                    )
                    for receipt, event_id, payload in stored
                ]
            if stored is not None and not self._memory:
                self._recover("read")
                return []
            if stored is None:
                self._fail("read", "durable read failed")
        return [
            PendingEvent(
                receipt=str(index),
                event_id=str(payload[self._id_field]),
                payload=dict(payload),
                durable=False,
            )
            for index, payload in enumerate(self._memory[:count])
        ]

    def acknowledge(self, events: list[PendingEvent]) -> bool:
        """Remove only events whose control-plane commit was confirmed."""
        if not events:
            return True
        durable = {event.durable for event in events}
        if len(durable) != 1:
            raise ValueError("cannot acknowledge mixed durable and memory events")
        if events[0].durable:
            if self._store is None:
                return False
            acknowledged = self._store.outbox_ack(
                self._stream, [event.receipt for event in events]
            )
            if acknowledged:
                self._recover("acknowledge")
            else:
                self._fail("acknowledge", "durable acknowledgement failed")
            return acknowledged

        expected = [event.event_id for event in events]
        actual = [str(payload.get(self._id_field) or "") for payload in self._memory[: len(events)]]
        if actual != expected:
            return False
        del self._memory[: len(events)]
        return True

    def depth(self) -> int | None:
        """Return pending event count, or None when Redis could not be read."""
        self._migrate_memory()
        if self._store is not None:
            stored = self._store.outbox_depth(self._stream)
            if stored is None:
                self._fail("depth", "durable depth check failed")
                return None
            self._recover("depth")
            return stored + len(self._memory)
        return len(self._memory)

    def status(self) -> dict[str, Any]:
        depth = self.depth()
        last_error = "; ".join(self._errors.values())
        return {
            "configured": self._store is not None,
            "required": bool(self._store is not None and self._store.required),
            "healthy": not self._errors,
            "last_error": last_error,
            "pending": depth,
        }

    def _migrate_memory(self) -> None:
        if self._store is None:
            return
        while self._memory:
            payload = self._memory[0]
            event_id = str(payload.get(self._id_field) or "")
            if not event_id or not self._store.outbox_enqueue(self._stream, event_id, payload):
                self._fail("migration", "durable migration failed")
                return
            del self._memory[0]
        self._recover("enqueue")
        self._recover("migration")

    def _fail(self, operation: str, message: str) -> None:
        self._errors[operation] = message

    def _recover(self, operation: str) -> None:
        self._errors.pop(operation, None)
