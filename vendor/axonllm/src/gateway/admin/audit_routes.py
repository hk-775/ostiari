"""Admin API routes for audit trail query and integrity verification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from src.gateway.export_jobs import (
    ExportJobError,
    ExportJobNotFound,
    ExportJobNotReady,
    ExportJobService,
    ExportKind,
    export_job_public,
    redact_audit_value,
    request_export_identity,
    serialize_audit_export_row,
)
from src.gateway.security.audit_trail import (
    LEGACY_TENANT_ID,
    AuditEventType,
    AuditRecord,
    AuditStoreUnavailable,
)

if TYPE_CHECKING:
    from src.gateway.security.audit_trail import AuditTrail


_READ_ONLY_ROLES = frozenset({"tenant_member", "tenant_auditor"})
_ADMIN_ROLES = frozenset({"admin", "tenant_admin", "platform_admin"})
_CANONICAL_ROLES = frozenset(
    {
        "tenant_admin",
        "tenant_member",
        "tenant_auditor",
        "platform_admin",
    }
)
class _TenantScopeError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _normalize_tenant_id(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise _TenantScopeError("tenant_id must be a non-empty string")
    return value


def _request_context(request: Request) -> object | None:
    state = getattr(request, "state", None)
    return getattr(state, "context", None)


def _context_roles(context: object | None) -> set[str]:
    raw_roles = getattr(context, "roles", None)
    if not isinstance(raw_roles, (list, tuple, set, frozenset)):
        return set()
    return {role for role in raw_roles if isinstance(role, str)}


def _requires_canonical_tenant(context: object | None) -> bool:
    return bool(
        context is not None
        and (
            getattr(context, "principal_id", None) is not None
            or _context_roles(context) & _CANONICAL_ROLES
        )
    )


def _request_tenant_id(request: Request) -> str:
    """Use canonical tenant scope and reject query-string overrides."""
    supplied = _normalize_tenant_id(request.query_params.get("tenant_id"))
    context = _request_context(request)
    authenticated = _normalize_tenant_id(getattr(context, "tenant_id", None))
    if authenticated is not None:
        if supplied is not None and supplied != authenticated:
            raise _TenantScopeError(
                "tenant_id does not match the authenticated tenant",
                status_code=403,
            )
        return authenticated
    if _requires_canonical_tenant(context):
        raise _TenantScopeError(
            "authenticated tenant context is missing tenant_id",
        )
    return supplied or LEGACY_TENANT_ID


def _scope_error_response(exc: _TenantScopeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "type": "invalid_tenant_scope",
                "message": str(exc),
            }
        },
    )


def _is_restricted_reader(request: Request) -> bool:
    context = _request_context(request)
    if context is None:
        return False
    roles = _context_roles(context)
    return bool(roles & _READ_ONLY_ROLES) and not bool(roles & _ADMIN_ROLES)


def _serialize_record(record: AuditRecord, *, restricted: bool) -> dict:
    return {
        "record_id": record.record_id,
        "event_type": record.event_type.value,
        "timestamp": record.timestamp.isoformat(),
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "project_id": record.project_id,
        "request_id": record.request_id,
        "data": redact_audit_value(record.data, restricted=restricted),
    }


def _serialize_export_row(row: dict, *, restricted: bool) -> dict:
    return serialize_audit_export_row(
        row,
        restricted=restricted,
    )


class AuditAPI:
    """Query and verify audit trail records."""

    def __init__(
        self,
        audit_trail: AuditTrail,
        *,
        export_jobs: ExportJobService | None = None,
    ) -> None:
        self.audit_trail = audit_trail
        self._export_jobs = export_jobs

    async def query_records(self, request: Request) -> JSONResponse:
        """GET /admin/audit/records?tenant_id=&project_id=&event_type=&limit="""
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        project_id = request.query_params.get("project_id")
        event_type_str = request.query_params.get("event_type")
        try:
            limit = min(max(int(request.query_params.get("limit", "100")), 1), 1000)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "limit must be an integer"},
            )

        event_type = None
        if event_type_str:
            try:
                event_type = AuditEventType(event_type_str)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": f"Invalid event_type: {event_type_str}",
                        "valid_types": [e.value for e in AuditEventType],
                    },
                )

        records = self.audit_trail.query_recent(
            project_id=project_id,
            event_type=event_type,
            limit=limit,
            tenant_id=tenant_id,
        )
        restricted = _is_restricted_reader(request)

        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "count": len(records),
                "records": [_serialize_record(record, restricted=restricted) for record in records],
            }
        )

    async def verify_integrity(self, request: Request) -> JSONResponse:
        """GET /admin/audit/verify[?durable=true][&project_id=]

        Default verifies the in-memory buffer. ``durable=true`` verifies the
        persisted chain (detects tampering with stored rows / cross-restart gaps).
        """
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        durable = request.query_params.get("durable", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if durable:
            result = await self.audit_trail.verify_persisted_chain(
                project_id=request.query_params.get("project_id"),
                tenant_id=tenant_id,
            )
            if not result.get("available", True):
                return JSONResponse(
                    status_code=503,
                    content={
                        "tenant_id": tenant_id,
                        "scope": "durable",
                        "chain_valid": False,
                        "checked": result.get("checked", 0),
                        "status": "unavailable",
                        "reason": result.get(
                            "reason",
                            "durable audit store unavailable",
                        ),
                    },
                )
            return JSONResponse(
                content={
                    "tenant_id": tenant_id,
                    "scope": "durable",
                    "chain_valid": result["valid"],
                    "checked": result.get("checked", 0),
                    "broken_at": result.get("broken_at"),
                    "reason": result.get("reason"),
                    "status": "intact" if result["valid"] else "TAMPERED",
                }
            )
        is_valid = self.audit_trail.verify_chain(tenant_id=tenant_id)
        buffer = self.audit_trail.buffered_records(tenant_id)
        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "scope": "buffer",
                "chain_valid": is_valid,
                "total_records_in_buffer": len(buffer),
                "status": "intact" if is_valid else "TAMPERED",
            }
        )

    async def export_records(self, request: Request) -> JSONResponse:
        """GET /admin/audit/export[?project_id=]

        Full audit history (durable store when enabled, else buffer) as JSON with
        chain hashes, for SIEM / S3 / offline verification.
        """
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        if self._export_jobs is not None:
            try:
                job = await self._export_jobs.create_audit(
                    tenant_id=tenant_id,
                    requested_by=request_export_identity(request),
                    project_id=request.query_params.get("project_id"),
                    restricted=_is_restricted_reader(request),
                )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "tenant_id": tenant_id,
                        "error": {
                            "type": "invalid_request",
                            "message": "Audit export filters are invalid.",
                        },
                    },
                )
            except ExportJobError:
                return JSONResponse(
                    status_code=503,
                    content={
                        "tenant_id": tenant_id,
                        "error": {
                            "type": "export_unavailable",
                            "message": "Audit export could not be queued.",
                        },
                    },
                )
            body = export_job_public(job)
            body["statusUrl"] = f"/admin/audit/exports/{job.job_id}"
            return JSONResponse(
                body,
                status_code=202,
                headers={"Retry-After": "2"},
            )
        try:
            records = await self.audit_trail.export_records(
                project_id=request.query_params.get("project_id"),
                tenant_id=tenant_id,
            )
        except AuditStoreUnavailable:
            return JSONResponse(
                status_code=503,
                content={
                    "tenant_id": tenant_id,
                    "error": {
                        "type": "audit_store_unavailable",
                        "message": "Durable audit export is temporarily unavailable.",
                    },
                },
            )
        restricted = _is_restricted_reader(request)
        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "count": len(records),
                "records": [_serialize_export_row(row, restricted=restricted) for row in records],
            }
        )

    async def export_status(self, request: Request) -> JSONResponse:
        """Return one requester-owned asynchronous audit export."""

        if self._export_jobs is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export jobs are not enabled."}},
                status_code=404,
            )
        try:
            tenant_id = _request_tenant_id(request)
            job = await self._export_jobs.get(
                tenant_id=tenant_id,
                requested_by=request_export_identity(request),
                job_id=request.path_params["job_id"],
                kind=ExportKind.AUDIT,
            )
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        except ExportJobNotFound:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export job was not found."}},
                status_code=404,
            )
        except ExportJobError:
            return JSONResponse(
                {
                    "error": {
                        "type": "export_unavailable",
                        "message": "Audit export status is temporarily unavailable.",
                    }
                },
                status_code=503,
            )
        body = export_job_public(job)
        if job.status.value == "complete":
            body["downloadUrl"] = (
                f"/admin/audit/exports/{job.job_id}/download"
            )
        return JSONResponse(body)

    async def export_download(self, request: Request):
        """Redirect an authorized requester to a short-lived S3 URL."""

        if self._export_jobs is None:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export jobs are not enabled."}},
                status_code=404,
            )
        try:
            tenant_id = _request_tenant_id(request)
            url = await self._export_jobs.download_url(
                tenant_id=tenant_id,
                requested_by=request_export_identity(request),
                job_id=request.path_params["job_id"],
                kind=ExportKind.AUDIT,
            )
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        except ExportJobNotFound:
            return JSONResponse(
                {"error": {"type": "not_found", "message": "Export job was not found."}},
                status_code=404,
            )
        except ExportJobNotReady:
            return JSONResponse(
                {
                    "error": {
                        "type": "export_not_ready",
                        "message": "Export job is not complete.",
                    }
                },
                status_code=409,
                headers={"Retry-After": "2"},
            )
        except ExportJobError:
            return JSONResponse(
                {
                    "error": {
                        "type": "export_unavailable",
                        "message": "Audit export download is temporarily unavailable.",
                    }
                },
                status_code=503,
            )
        return RedirectResponse(url, status_code=303)

    async def get_stats(self, request: Request) -> JSONResponse:
        """GET /admin/audit/stats"""
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        buffer = self.audit_trail.buffered_records(tenant_id)
        if not buffer:
            return JSONResponse(
                content={
                    "tenant_id": tenant_id,
                    "total": 0,
                    "by_type": {},
                    "by_project": {},
                }
            )

        by_type: dict[str, int] = {}
        by_project: dict[str, int] = {}
        for r in buffer:
            by_type[r.event_type.value] = by_type.get(r.event_type.value, 0) + 1
            by_project[r.project_id] = by_project.get(r.project_id, 0) + 1

        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "total": len(buffer),
                "by_type": by_type,
                "by_project": by_project,
                "oldest": buffer[0].timestamp.isoformat(),
                "newest": buffer[-1].timestamp.isoformat(),
            }
        )

    async def get_security_events(self, request: Request) -> JSONResponse:
        """GET /admin/audit/security?project_id=&limit="""
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        project_id = request.query_params.get("project_id")
        try:
            limit = min(max(int(request.query_params.get("limit", "50")), 1), 1000)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "limit must be an integer"},
            )

        security_types = {
            AuditEventType.INJECTION_DETECTED,
            AuditEventType.INJECTION_BLOCKED,
            AuditEventType.PII_REDACTION,
            AuditEventType.AUTH_FAILURE,
            AuditEventType.POLICY_DENY,
        }

        records = self.audit_trail.query_recent(
            project_id=project_id,
            limit=limit,
            tenant_id=tenant_id,
        )
        records = [r for r in records if r.event_type in security_types]
        records = records[-limit:]
        restricted = _is_restricted_reader(request)

        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "count": len(records),
                "records": [_serialize_record(record, restricted=restricted) for record in records],
            }
        )

    async def preview_pii_redaction(self, request: Request) -> JSONResponse:
        """POST /admin/pii/preview — show what redaction does to a given string.

        The audit trail records that redaction happened and how many items it
        replaced, which is the compliance question. It cannot show *what the
        provider actually received*, because storing that would mean storing the
        PII the feature exists to keep out of storage.

        So this recomputes it on demand: text in, redacted text plus the
        re-injected round-trip out, nothing persisted. It runs the real
        PIIRedactor rather than a canned example, so the panel cannot drift away
        from the engine's actual behaviour — a mocked-up screenshot would still
        look right after a pattern stopped matching.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {"type": "invalid_request", "message": "Invalid JSON body"},
                },
            )

        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                status_code=400,
                content={
                    "error": {"type": "invalid_request", "message": "text is required"},
                },
            )
        # Bounded so the panel cannot be used to run regexes over arbitrarily
        # large input. Generous next to any realistic demo prompt.
        if len(text) > 4000:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {"type": "invalid_request", "message": "text exceeds 4000 characters"},
                },
            )

        requested = body.get("types")
        if requested is not None and (
            not isinstance(requested, list) or not all(isinstance(t, str) for t in requested)
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {"type": "invalid_request", "message": "types must be a list of strings"},
                },
            )

        from src.gateway.models import ResolvedPolicy
        from src.gateway.security.pii_ner import NER_TYPE_MAP, build_entity_detector
        from src.gateway.security.pii_redactor import PII_PATTERNS, PIIRedactor

        unknown = [t for t in (requested or []) if t not in PII_PATTERNS]
        if unknown:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {"type": "invalid_request", "message": f"unknown PII types: {', '.join(sorted(unknown))}"},
                    "supported_types": sorted(PII_PATTERNS),
                },
            )

        policy = ResolvedPolicy()
        policy.pii_redaction_enabled = True
        policy.pii_redact_types = list(requested) if requested else list(PII_PATTERNS)

        redactor = PIIRedactor()
        redacted_messages, mapping = redactor.redact_messages([{"role": "user", "content": text}], policy)
        redacted = redacted_messages[0]["content"]

        # Echoing the redacted text back is what a provider that quotes the
        # prompt would do, and it is the case where re-injection is visible:
        # the caller reads their own value, the provider never held it.
        reinjected = redactor.reinject_response(redacted, mapping)

        result = {
            "original": text,
            "redacted": redacted,
            "reinjected": reinjected,
            "redacted_count": mapping.redacted_count,
            "types_found": sorted(mapping._counters.keys()),
            "types_checked": sorted(policy.pii_redact_types),
            # The round trip is lossless when every token maps back. Reported
            # rather than asserted so a pattern whose match cannot be restored
            # shows up in the panel instead of silently mangling the answer.
            "round_trip_exact": reinjected == text,
            "supported_types": sorted(PII_PATTERNS),
            "supported_ner_types": sorted(set(NER_TYPE_MAP.values())),
        }

        # Second column: the same text with named-entity detection added. Run
        # only on request, because it costs money per call — the panel makes the
        # regex-vs-NER difference visible, which is the whole point, but a
        # dashboard poll should not bill anyone by accident.
        if body.get("ner"):
            detector = build_entity_detector()
            if detector is None:
                result["ner"] = {"available": False, "reason": "boto3 unavailable in this deploy"}
            else:
                ner_policy = ResolvedPolicy()
                ner_policy.pii_redaction_enabled = True
                ner_policy.pii_redact_types = list(policy.pii_redact_types)
                ner_policy.pii_ner_enabled = True
                ner_types = body.get("ner_types")
                if isinstance(ner_types, list) and all(isinstance(t, str) for t in ner_types):
                    ner_policy.pii_ner_types = [t for t in ner_types if t in set(NER_TYPE_MAP.values())]
                try:
                    ner_redactor = PIIRedactor(entity_detector=detector)
                    ner_messages, ner_mapping = await ner_redactor.redact_messages_async(
                        [{"role": "user", "content": text}], ner_policy
                    )
                    ner_redacted = ner_messages[0]["content"]
                    if ner_mapping.ner_error:
                        # Redaction fails open, so the call above returned a
                        # normal-looking result with the shapeless types
                        # unchecked. Reporting available=False is the honest
                        # answer: two identical columns and no explanation would
                        # read as "entity detection found nothing".
                        result["ner"] = {"available": False, "reason": ner_mapping.ner_error}
                        return JSONResponse(content=result)
                    result["ner"] = {
                        "available": True,
                        "redacted": ner_redacted,
                        "reinjected": ner_redactor.reinject_response(ner_redacted, ner_mapping),
                        "redacted_count": ner_mapping.redacted_count,
                        "types_found": sorted(ner_mapping._counters.keys()),
                        # What NER added over regex alone. The reason the panel
                        # has two columns: this is the answer to "why wasn't the
                        # name redacted", stated as a number.
                        "additional_count": (ner_mapping.redacted_count - mapping.redacted_count),
                    }
                except Exception as exc:  # pragma: no cover - network dependent
                    # Surfaced rather than 500'd: a detector outage is a fact
                    # about the panel's second column, not a failure of the
                    # first, and the request path fails open the same way.
                    result["ner"] = {"available": False, "reason": str(exc)[:200]}

        return JSONResponse(content=result)


def create_audit_routes(audit_api: AuditAPI) -> list[Route]:
    """Create Starlette routes for audit trail admin API."""
    return [
        Route("/admin/audit/records", audit_api.query_records, methods=["GET"]),
        Route("/admin/audit/verify", audit_api.verify_integrity, methods=["GET"]),
        Route("/admin/audit/export", audit_api.export_records, methods=["GET"]),
        Route(
            "/admin/audit/exports/{job_id}",
            audit_api.export_status,
            methods=["GET"],
        ),
        Route(
            "/admin/audit/exports/{job_id}/download",
            audit_api.export_download,
            methods=["GET"],
        ),
        Route("/admin/audit/stats", audit_api.get_stats, methods=["GET"]),
        Route("/admin/audit/security", audit_api.get_security_events, methods=["GET"]),
        Route("/admin/pii/preview", audit_api.preview_pii_redaction, methods=["POST"]),
    ]
