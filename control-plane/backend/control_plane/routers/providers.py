"""LLM Provider Configuration API — manage provider credentials and connectivity."""

import contextlib
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org, require_role
from control_plane.database import get_db
from control_plane.models.database import ProviderRouteRecord
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.runtime_state import (
    delete_runtime_state,
    put_runtime_state,
)

log = logging.getLogger("control_plane.routers.providers")

router = APIRouter(prefix="/api/providers", tags=["providers"])

# ---------------------------------------------------------------------------
# Encryption helpers (Fernet symmetric encryption for API keys at rest)
# ---------------------------------------------------------------------------

_ENCRYPTION_KEY = os.environ.get("OSTIARI_ENCRYPTION_KEY", "")

# Cached cipher so encrypt and decrypt share the same key for the process
# lifetime. Without this, an unset OSTIARI_ENCRYPTION_KEY would mint a NEW
# transient key on every call, so anything encrypted could never be decrypted
# (the key-reveal endpoint would silently return "").
_fernet = None


def _get_fernet():
    """Return a cached Fernet cipher, building it once per process.

    Uses OSTIARI_ENCRYPTION_KEY when set; otherwise falls back to a single
    transient key generated on first use (not production safe — keys won't
    survive a restart, but they stay stable within the running process).
    """
    global _fernet
    if _fernet is not None:
        return _fernet

    from cryptography.fernet import Fernet

    if not _ENCRYPTION_KEY:
        log.warning("OSTIARI_ENCRYPTION_KEY not set — using a transient key (not production safe)")
        _fernet = Fernet(Fernet.generate_key())
    else:
        key = _ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY
        _fernet = Fernet(key)
    return _fernet


def _encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProviderCreate(BaseModel):
    name: str  # anthropic, openai, bedrock, azure, vertex, cohere
    api_key: str = ""
    api_base_url: str = ""
    region: str = ""
    project_id: str = ""
    tenant_id: str = ""
    enabled: bool = True


class ProviderUpdate(BaseModel):
    api_key: str | None = None
    api_base_url: str | None = None
    region: str | None = None
    project_id: str | None = None
    tenant_id: str | None = None
    enabled: bool | None = None


class ProviderResponse(BaseModel):
    name: str
    enabled: bool
    status: str = "unchecked"  # connected, error, unchecked
    last_checked: datetime | None = None
    latency_ms: float | None = None
    models_available: list[str] = Field(default_factory=list)
    api_base_url: str = ""
    region: str = ""
    project_id: str = ""
    tenant_id: str = ""
    has_api_key: bool = False
    api_key_preview: str = ""  # masked: "....sk-1234"


class ProviderDetail(ProviderResponse):
    """Extended response that can include the decrypted key (for reveal toggle)."""
    api_key: str = ""


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class _ProviderRecord(BaseModel):
    name: str
    api_key_encrypted: str = ""
    api_base_url: str = ""
    region: str = ""
    project_id: str = ""
    tenant_id: str = ""
    enabled: bool = True
    status: str = "unchecked"
    last_checked: datetime | None = None
    latency_ms: float | None = None
    models_available: list[str] = Field(default_factory=list)


# Provider credentials, scoped per org (tenant). Nested org -> name -> record
# so one org can never read or overwrite another org's encrypted API keys by
# reusing a provider name. Single-org dev/demo uses only the "default" org.
_providers: dict[str, dict[str, "_ProviderRecord"]] = defaultdict(dict)


def _mask_key(key: str) -> str:
    """Return a masked representation of an API key."""
    if not key:
        return ""
    if len(key) <= 8:
        return "****" + key[-4:] if len(key) > 4 else "****"
    return "****" + key[-4:]


def _to_response(rec: _ProviderRecord) -> ProviderResponse:
    raw_key = ""
    with contextlib.suppress(Exception):
        raw_key = _decrypt(rec.api_key_encrypted)
    return ProviderResponse(
        name=rec.name,
        enabled=rec.enabled,
        status=rec.status,
        last_checked=rec.last_checked,
        latency_ms=rec.latency_ms,
        models_available=rec.models_available,
        api_base_url=rec.api_base_url,
        region=rec.region,
        project_id=rec.project_id,
        tenant_id=rec.tenant_id,
        has_api_key=bool(rec.api_key_encrypted),
        api_key_preview=_mask_key(raw_key),
    )


# ---------------------------------------------------------------------------
# Known models per provider (used for display when test succeeds)
# ---------------------------------------------------------------------------

_KNOWN_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "o3",
        "o4-mini",
    ],
    "bedrock": [
        "us.anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-sonnet-4-6",
        "amazon.nova-pro-v1:0",
        "amazon.nova-lite-v1:0",
    ],
    "bedrock-mantle": [
        "anthropic.claude-sonnet-4-6",
        "anthropic.claude-haiku-4-5",
        "amazon.nova-pro",
        "amazon.nova-lite",
        "meta.llama-4-maverick",
    ],
    "azure": [
        "gpt-4o",
        "gpt-4o-mini",
    ],
    "vertex": [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
    "cohere": [
        "command-r-plus",
        "command-r",
    ],
    "xai": [
        "grok-3",
        "grok-3-mini",
        "grok-2-vision-1212",
    ],
    "together": [
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        "deepseek-ai/DeepSeek-R1",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
        "mistralai/Mistral-Small-24B-Instruct-2501",
    ],
}

# Providers that speak the OpenAI wire format, so connectivity is one shared
# probe against /v1/chat/completions. Base URLs and probe models mirror
# AxonLLM's provider_config/adapters — the router is what ultimately calls
# these, so a divergence here would "pass" a key that can't actually route.
_OPENAI_COMPATIBLE: dict[str, dict[str, str]] = {
    "xai": {"base_url": "https://api.x.ai", "probe_model": "grok-3-mini"},
    "together": {"base_url": "https://api.together.xyz",
                 "probe_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
}

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_admin_dep = Depends(require_role("admin"))


@router.get("", response_model=list[ProviderResponse])
async def list_providers(org: str = Depends(get_current_org)):
    """List this org's configured providers (no keys exposed)."""
    return [_to_response(p) for p in _providers[org].values()]


@router.post("", response_model=ProviderResponse)
async def add_provider(
    body: ProviderCreate,
    request: Request,
    _user=_admin_dep,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Add a new provider configuration (within the caller's org)."""
    if body.name in _providers[org]:
        raise HTTPException(status_code=409, detail=f"Provider '{body.name}' already exists")

    rec = _ProviderRecord(
        name=body.name,
        api_key_encrypted=_encrypt(body.api_key),
        api_base_url=body.api_base_url,
        region=body.region,
        project_id=body.project_id,
        tenant_id=body.tenant_id,
        enabled=body.enabled,
    )
    _providers[org][body.name] = rec
    await put_runtime_state(
        db,
        org,
        "providers",
        body.name,
        rec.model_dump(mode="json"),
    )
    await audit.log(
        db,
        actor_of(request),
        "create",
        "provider",
        body.name,
        {
            "api_base_url": body.api_base_url,
            "region": body.region,
            "project_id": body.project_id,
            "tenant_id": body.tenant_id,
            "enabled": body.enabled,
            "has_api_key": bool(body.api_key),
        },
        org=org,
    )
    await db.commit()
    return _to_response(rec)


@router.put("/{name}", response_model=ProviderResponse)
async def update_provider(
    name: str,
    body: ProviderUpdate,
    request: Request,
    _user=_admin_dep,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Update an existing provider (within the caller's org)."""
    if name not in _providers[org]:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[org][name]
    if body.api_key is not None:
        rec.api_key_encrypted = _encrypt(body.api_key)
    if body.api_base_url is not None:
        rec.api_base_url = body.api_base_url
    if body.region is not None:
        rec.region = body.region
    if body.project_id is not None:
        rec.project_id = body.project_id
    if body.tenant_id is not None:
        rec.tenant_id = body.tenant_id
    if body.enabled is not None:
        rec.enabled = body.enabled

    _providers[org][name] = rec
    await put_runtime_state(
        db,
        org,
        "providers",
        name,
        rec.model_dump(mode="json"),
    )
    changes = body.model_dump(exclude_unset=True, mode="json")
    if "api_key" in changes:
        changes["api_key"] = "[updated]" if changes["api_key"] else "[cleared]"
    await audit.log(
        db,
        actor_of(request),
        "update",
        "provider",
        name,
        changes,
        org=org,
    )
    await db.commit()
    return _to_response(rec)


@router.delete("/{name}")
async def delete_provider(
    name: str,
    request: Request,
    _user=_admin_dep,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Remove a provider (within the caller's org)."""
    if name not in _providers[org]:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    runtime_name = {
        "azure": "azure_openai",
        "google": "google_ai",
        "vertex": "vertex_ai",
    }.get(name, name)
    await db.execute(
        delete(ProviderRouteRecord).where(
            ProviderRouteRecord.org_id == org,
            ProviderRouteRecord.provider == runtime_name,
        )
    )
    del _providers[org][name]
    await delete_runtime_state(db, org, "providers", name)
    await audit.log(
        db,
        actor_of(request),
        "delete",
        "provider",
        name,
        {},
        org=org,
    )
    await db.commit()
    return {"deleted": name}


@router.post("/{name}/test")
async def test_provider(
    name: str,
    _user=_admin_dep,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Test connectivity to a provider by making a minimal API call."""
    if name not in _providers[org]:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[org][name]
    try:
        api_key = _decrypt(rec.api_key_encrypted)
    except Exception:
        api_key = ""

    start = time.time()
    error_msg = ""
    success = False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if name == "anthropic":
                base = rec.api_base_url or "https://api.anthropic.com"
                resp = await client.post(
                    f"{base}/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                # 200 or 400 (validation) means the key works
                success = resp.status_code in (200, 400) or (resp.status_code < 500)
                if resp.status_code == 401:
                    success = False
                    error_msg = "Invalid API key"
                elif resp.status_code >= 500:
                    error_msg = f"Server error: {resp.status_code}"

            elif name == "openai":
                base = rec.api_base_url or "https://api.openai.com"
                resp = await client.post(
                    f"{base}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                success = resp.status_code in (200, 400) or (resp.status_code < 500)
                if resp.status_code == 401:
                    success = False
                    error_msg = "Invalid API key"
                elif resp.status_code >= 500:
                    error_msg = f"Server error: {resp.status_code}"

            elif name == "bedrock":
                # For Bedrock, we check if boto3 credentials work
                # In a real implementation, we'd use boto3 to call invoke_model
                # For now, we verify the region is set
                if not rec.region:
                    error_msg = "Region is required for Bedrock"
                else:
                    try:
                        import boto3
                        # Try listing foundation models as a connectivity check
                        bedrock_mgmt = boto3.client("bedrock", region_name=rec.region)
                        bedrock_mgmt.list_foundation_models(byOutputModality="TEXT")
                        success = True
                    except ImportError:
                        error_msg = "boto3 not installed"
                    except Exception as e:
                        error_msg = str(e)[:200]

            elif name == "bedrock-mantle":
                if not rec.region:
                    error_msg = "Region is required for Bedrock Mantle"
                elif not api_key:
                    error_msg = "API key is required for Bedrock Mantle"
                else:
                    base_url = f"https://bedrock-mantle.{rec.region}.api.aws"
                    resp = await client.post(
                        f"{base_url}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": "anthropic.claude-haiku-4-5",
                            "max_tokens": 1,
                            "messages": [{"role": "user", "content": "hi"}],
                        },
                    )
                    success = resp.status_code in (200, 400) or (resp.status_code < 500)
                    if resp.status_code == 401 or resp.status_code == 403:
                        success = False
                        error_msg = "Invalid API key or insufficient permissions"

            elif name == "azure":
                base = rec.api_base_url
                if not base:
                    error_msg = "API base URL is required for Azure OpenAI"
                else:
                    resp = await client.post(
                        f"{base}/openai/deployments/gpt-4o/chat/completions?api-version=2024-02-01",
                        headers={
                            "api-key": api_key,
                            "Content-Type": "application/json",
                        },
                        json={
                            "max_tokens": 1,
                            "messages": [{"role": "user", "content": "hi"}],
                        },
                    )
                    success = resp.status_code in (200, 400) or (resp.status_code < 500)
                    if resp.status_code == 401:
                        success = False
                        error_msg = "Invalid API key"

            elif name == "vertex":
                # Vertex AI uses Google Cloud auth
                if not rec.project_id:
                    error_msg = "Project ID is required for Vertex AI"
                elif not rec.region:
                    error_msg = "Region is required for Vertex AI"
                else:
                    base = rec.api_base_url or f"https://{rec.region}-aiplatform.googleapis.com"
                    resp = await client.post(
                        f"{base}/v1/projects/{rec.project_id}/locations/{rec.region}/publishers/google/models/gemini-2.5-flash:generateContent",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "contents": [{"parts": [{"text": "hi"}]}],
                            "generationConfig": {"maxOutputTokens": 1},
                        },
                    )
                    success = resp.status_code in (200, 400) or (resp.status_code < 500)
                    if resp.status_code == 401:
                        success = False
                        error_msg = "Invalid credentials"

            elif name == "cohere":
                base = rec.api_base_url or "https://api.cohere.ai"
                resp = await client.post(
                    f"{base}/v1/chat",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "command-r",
                        "message": "hi",
                        "max_tokens": 1,
                    },
                )
                success = resp.status_code in (200, 400) or (resp.status_code < 500)
                if resp.status_code == 401:
                    success = False
                    error_msg = "Invalid API key"

            elif name in _OPENAI_COMPATIBLE:
                # xAI and Together both speak the OpenAI wire format, so one
                # branch covers them (and any future OpenAI-compatible provider)
                # rather than two near-copies of the /v1/chat/completions probe.
                # Base URLs and probe models match AxonLLM's adapters so a key
                # that tests OK here is one the router can actually route to.
                spec = _OPENAI_COMPATIBLE[name]
                base = rec.api_base_url or spec["base_url"]
                resp = await client.post(
                    f"{base}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": spec["probe_model"],
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                success = resp.status_code < 500
                if resp.status_code in (401, 403):
                    success = False
                    error_msg = "Invalid API key"
                elif resp.status_code >= 500:
                    error_msg = f"Server error: {resp.status_code}"

            else:
                error_msg = f"Unknown provider type: {name}"

    except httpx.TimeoutException:
        error_msg = "Connection timed out"
    except Exception as e:
        error_msg = str(e)[:200]

    elapsed_ms = round((time.time() - start) * 1000, 1)

    # Update record
    rec.last_checked = datetime.now(timezone.utc)
    rec.latency_ms = elapsed_ms
    rec.status = "connected" if success else "error"
    if success:
        rec.models_available = _KNOWN_MODELS.get(name, [])
    _providers[org][name] = rec
    await put_runtime_state(
        db,
        org,
        "providers",
        name,
        rec.model_dump(mode="json"),
    )
    await db.commit()

    return {
        "name": name,
        "success": success,
        "latency_ms": elapsed_ms,
        "error": error_msg if not success else None,
        "models_available": rec.models_available if success else [],
    }


@router.get("/{name}/health")
async def provider_health(name: str, org: str = Depends(get_current_org)):
    """Check cached health status and latency for a provider."""
    if name not in _providers[org]:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[org][name]
    return {
        "name": name,
        "status": rec.status,
        "enabled": rec.enabled,
        "latency_ms": rec.latency_ms,
        "last_checked": rec.last_checked,
        "models_available": rec.models_available,
    }


@router.get("/{name}/key")
async def get_provider_key(name: str, _user=_admin_dep, org: str = Depends(get_current_org)):
    """Return the decrypted API key (admin only, for reveal toggle)."""
    if name not in _providers[org]:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[org][name]
    try:
        raw_key = _decrypt(rec.api_key_encrypted)
    except Exception:
        raw_key = ""

    return {"name": name, "api_key": raw_key}
