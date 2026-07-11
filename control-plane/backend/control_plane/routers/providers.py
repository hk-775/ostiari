"""LLM Provider Configuration API — manage provider credentials and connectivity."""

import os
import time
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
import httpx

from control_plane.auth.dependencies import require_role

log = logging.getLogger("control_plane.routers.providers")

router = APIRouter(prefix="/api/providers", tags=["providers"])

# ---------------------------------------------------------------------------
# Encryption helpers (Fernet symmetric encryption for API keys at rest)
# ---------------------------------------------------------------------------

_ENCRYPTION_KEY = os.environ.get("OSTIARI_ENCRYPTION_KEY", "")


def _get_fernet():
    """Lazy-load Fernet cipher using OSTIARI_ENCRYPTION_KEY env var."""
    from cryptography.fernet import Fernet

    if not _ENCRYPTION_KEY:
        # Fallback: generate a transient key (keys won't survive restart without env var)
        log.warning("OSTIARI_ENCRYPTION_KEY not set — using transient key (not production safe)")
        return Fernet(Fernet.generate_key())
    return Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)


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


_providers: dict[str, _ProviderRecord] = {}


def _mask_key(key: str) -> str:
    """Return a masked representation of an API key."""
    if not key:
        return ""
    if len(key) <= 8:
        return "****" + key[-4:] if len(key) > 4 else "****"
    return "****" + key[-4:]


def _to_response(rec: _ProviderRecord) -> ProviderResponse:
    raw_key = ""
    try:
        raw_key = _decrypt(rec.api_key_encrypted)
    except Exception:
        pass
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
}

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_admin_dep = Depends(require_role("admin"))


@router.get("", response_model=list[ProviderResponse])
async def list_providers():
    """List all configured providers (no keys exposed)."""
    return [_to_response(p) for p in _providers.values()]


@router.post("", response_model=ProviderResponse)
async def add_provider(body: ProviderCreate, _user=_admin_dep):
    """Add a new provider configuration."""
    if body.name in _providers:
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
    _providers[body.name] = rec
    return _to_response(rec)


@router.put("/{name}", response_model=ProviderResponse)
async def update_provider(name: str, body: ProviderUpdate, _user=_admin_dep):
    """Update an existing provider."""
    if name not in _providers:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[name]
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

    _providers[name] = rec
    return _to_response(rec)


@router.delete("/{name}")
async def delete_provider(name: str, _user=_admin_dep):
    """Remove a provider."""
    if name not in _providers:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")
    del _providers[name]
    return {"deleted": name}


@router.post("/{name}/test")
async def test_provider(name: str, _user=_admin_dep):
    """Test connectivity to a provider by making a minimal API call."""
    if name not in _providers:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[name]
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
                        bedrock_client = boto3.client(
                            "bedrock-runtime",
                            region_name=rec.region,
                        )
                        # Try listing foundation models as a connectivity check
                        bedrock_mgmt = boto3.client("bedrock", region_name=rec.region)
                        bedrock_mgmt.list_foundation_models(byOutputModality="TEXT")
                        success = True
                    except ImportError:
                        error_msg = "boto3 not installed"
                    except Exception as e:
                        error_msg = str(e)[:200]

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
    _providers[name] = rec

    return {
        "name": name,
        "success": success,
        "latency_ms": elapsed_ms,
        "error": error_msg if not success else None,
        "models_available": rec.models_available if success else [],
    }


@router.get("/{name}/health")
async def provider_health(name: str):
    """Check cached health status and latency for a provider."""
    if name not in _providers:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[name]
    return {
        "name": name,
        "status": rec.status,
        "enabled": rec.enabled,
        "latency_ms": rec.latency_ms,
        "last_checked": rec.last_checked,
        "models_available": rec.models_available,
    }


@router.get("/{name}/key")
async def get_provider_key(name: str, _user=_admin_dep):
    """Return the decrypted API key (admin only, for reveal toggle)."""
    if name not in _providers:
        raise HTTPException(status_code=404, detail=f"Provider '{name}' not found")

    rec = _providers[name]
    try:
        raw_key = _decrypt(rec.api_key_encrypted)
    except Exception:
        raw_key = ""

    return {"name": name, "api_key": raw_key}
