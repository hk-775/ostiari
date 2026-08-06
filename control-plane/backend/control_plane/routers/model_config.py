"""Model configuration API — registry of available models and their routing config."""

import os
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import DEFAULT_ORG
from control_plane.services.audit_service import actor_of, audit

router = APIRouter(prefix="/api/models", tags=["models"])


class ProviderMapping(BaseModel):
    provider: str
    model_id: str
    weight: float = 1.0
    fallback_order: int = 0


class ModelConfig(BaseModel):
    name: str
    description: str = ""
    routing_strategy: str = "round-robin"
    providers: list[ProviderMapping] = Field(default_factory=list)
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    max_tokens: int = 4096
    supports_tools: bool = True
    supports_vision: bool = False
    category: str = "general"


# In-memory model registry, scoped per org (tenant): org -> name -> config.
# Single-org dev/demo uses only the "default" org.
_models: dict[str, dict[str, ModelConfig]] = defaultdict(dict)


@router.get("")
async def list_models(org: str = Depends(get_current_org)) -> list[ModelConfig]:
    return list(_models[org].values())


@router.get("/{name}")
async def get_model(name: str, org: str = Depends(get_current_org)) -> ModelConfig:
    if name not in _models[org]:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    return _models[org][name]


def _audit_details(m: ModelConfig) -> dict:
    """The fields of a model worth recording: prices (they drive budget
    enforcement) and provider mapping (it decides where the data goes)."""
    return {
        "input_cost_per_1k": m.input_cost_per_1k,
        "output_cost_per_1k": m.output_cost_per_1k,
        "routing_strategy": m.routing_strategy,
        "max_tokens": m.max_tokens,
        "providers": [f"{p.provider}:{p.model_id}" for p in m.providers],
    }


@router.post("")
async def add_model(
    body: ModelConfig,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> ModelConfig:
    _models[org][body.name] = body
    await audit.log(db, actor_of(request), "create", "model", body.name,
                    _audit_details(body), org=org)
    await db.commit()
    return body


@router.put("/{name}")
async def update_model(
    name: str,
    body: ModelConfig,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> ModelConfig:
    before = _models[org].get(name)
    if before is None:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    _models[org][name] = body
    # Record only what actually changed — a full before/after doubles the row size
    # and buries the one edited price.
    old, new = _audit_details(before), _audit_details(body)
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    await audit.log(db, actor_of(request), "update", "model", name, changes, org=org)
    await db.commit()
    return body


@router.delete("/{name}")
async def delete_model(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    model = _models[org].get(name)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    del _models[org][name]
    await audit.log(db, actor_of(request), "delete", "model", name,
                    _audit_details(model), org=org)
    await db.commit()
    return {"deleted": name}


def pricing_table(org: str) -> dict[str, dict[str, float]]:
    """This org's registry as a gateway-shaped pricing table.

    The gateway's QuotaEnforcer takes ``{model: {"input": x, "output": y}}`` with
    costs per 1k tokens — the same unit the registry stores — so this is a rename,
    not a conversion. Models priced at zero are omitted: the enforcer treats a
    missing model as "fall back to DEFAULT_PRICING", which is a better answer than
    asserting a real model is free (that would silently disable budget enforcement
    for it).
    """
    return {
        name: {"input": m.input_cost_per_1k, "output": m.output_cost_per_1k}
        for name, m in _models[org].items()
        if m.input_cost_per_1k > 0 or m.output_cost_per_1k > 0
    }


def seed_models():
    """Pre-seed with AxonLLM models."""
    models = [
        ModelConfig(name="claude-opus", description="Claude Opus 4 — most capable", routing_strategy="least-latency",
                    providers=[ProviderMapping(provider="bedrock", model_id="us.anthropic.claude-opus-4-6-v1"), ProviderMapping(provider="anthropic", model_id="claude-opus-4-8", fallback_order=1)],
                    input_cost_per_1k=0.015, output_cost_per_1k=0.075, max_tokens=32768, supports_tools=True, supports_vision=True, category="reasoning"),
        ModelConfig(name="claude-sonnet", description="Claude Sonnet 4.6 — balanced", routing_strategy="cost-optimized",
                    providers=[ProviderMapping(provider="anthropic", model_id="claude-sonnet-4-6"), ProviderMapping(provider="bedrock", model_id="us.anthropic.claude-sonnet-4-6", fallback_order=1)],
                    input_cost_per_1k=0.003, output_cost_per_1k=0.015, max_tokens=8192, supports_tools=True, supports_vision=True, category="general"),
        ModelConfig(name="claude-haiku", description="Claude Haiku 4.5 — fast & cheap", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="anthropic", model_id="claude-haiku-4-5-20251001"), ProviderMapping(provider="bedrock", model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", fallback_order=1)],
                    input_cost_per_1k=0.0008, output_cost_per_1k=0.004, max_tokens=8192, supports_tools=True, category="speed"),
        ModelConfig(name="gpt-4o", description="GPT-4o — OpenAI flagship", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="openai", model_id="gpt-4o")],
                    input_cost_per_1k=0.0025, output_cost_per_1k=0.01, max_tokens=4096, supports_tools=True, supports_vision=True, category="general"),
        ModelConfig(name="gpt-4o-mini", description="GPT-4o Mini — budget OpenAI", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="openai", model_id="gpt-4o-mini")],
                    input_cost_per_1k=0.00015, output_cost_per_1k=0.0006, max_tokens=4096, supports_tools=True, category="speed"),
        ModelConfig(name="o4-mini", description="O4 Mini — OpenAI reasoning", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="openai", model_id="o4-mini")],
                    input_cost_per_1k=0.0011, output_cost_per_1k=0.0044, max_tokens=16384, supports_tools=True, category="reasoning"),
        ModelConfig(name="o3", description="O3 — OpenAI advanced reasoning", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="openai", model_id="o3")],
                    input_cost_per_1k=0.01, output_cost_per_1k=0.04, max_tokens=32768, supports_tools=True, category="reasoning"),
        ModelConfig(name="gemini-2.5-pro", description="Gemini 2.5 Pro — Google", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="vertex", model_id="gemini-2.5-pro")],
                    input_cost_per_1k=0.00125, output_cost_per_1k=0.01, max_tokens=8192, supports_tools=True, supports_vision=True, category="general"),
        ModelConfig(name="gemini-2.5-flash", description="Gemini 2.5 Flash — fast Google", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="vertex", model_id="gemini-2.5-flash")],
                    input_cost_per_1k=0.000075, output_cost_per_1k=0.0003, max_tokens=8192, supports_tools=True, category="speed"),
        ModelConfig(name="nova-pro", description="Amazon Nova Pro", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="bedrock", model_id="amazon.nova-pro-v1:0")],
                    input_cost_per_1k=0.0008, output_cost_per_1k=0.0032, max_tokens=5120, supports_tools=True, category="general"),
        ModelConfig(name="nova-lite", description="Amazon Nova Lite", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="bedrock", model_id="amazon.nova-lite-v1:0")],
                    input_cost_per_1k=0.00006, output_cost_per_1k=0.00024, max_tokens=5120, supports_tools=True, category="speed"),
        ModelConfig(name="deepseek-r1", description="DeepSeek R1 — reasoning", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="bedrock", model_id="deepseek.deepseek-r1")],
                    input_cost_per_1k=0.00135, output_cost_per_1k=0.0054, max_tokens=8192, supports_tools=False, category="reasoning"),
        ModelConfig(name="llama-4-maverick", description="Llama 4 Maverick — Meta", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="bedrock", model_id="meta.llama4-maverick-17b-instruct-v1:0")],
                    input_cost_per_1k=0.0003, output_cost_per_1k=0.0006, max_tokens=8192, supports_tools=True, category="general"),
        ModelConfig(name="mistral-large", description="Mistral Large", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="bedrock", model_id="mistral.mistral-large-2411-v1:0")],
                    input_cost_per_1k=0.002, output_cost_per_1k=0.006, max_tokens=8192, supports_tools=True, category="general"),
        # xAI (Grok) and Together — both OpenAI-compatible, credentialed from
        # XAI_API_KEY / TOGETHER_API_KEY. Model ids match AxonLLM's adapters so
        # the router can actually dispatch these, not just display them.
        ModelConfig(name="grok-3", description="Grok 3 — xAI flagship", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="xai", model_id="grok-3")],
                    input_cost_per_1k=0.003, output_cost_per_1k=0.015, max_tokens=8192, supports_tools=True, category="general"),
        ModelConfig(name="grok-3-mini", description="Grok 3 Mini — fast, cheap", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="xai", model_id="grok-3-mini")],
                    input_cost_per_1k=0.0003, output_cost_per_1k=0.0005, max_tokens=8192, supports_tools=True, category="speed"),
        ModelConfig(name="llama-3.3-70b", description="Llama 3.3 70B Turbo — Together", routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="together", model_id="meta-llama/Llama-3.3-70B-Instruct-Turbo")],
                    input_cost_per_1k=0.00088, output_cost_per_1k=0.00088, max_tokens=8192, supports_tools=True, category="general"),
        ModelConfig(name="deepseek-r1-together", description="DeepSeek R1 — reasoning, via Together",
                    routing_strategy="round-robin",
                    providers=[ProviderMapping(provider="together", model_id="deepseek-ai/DeepSeek-R1")],
                    input_cost_per_1k=0.003, output_cost_per_1k=0.007, max_tokens=8192, supports_tools=False, category="reasoning"),
    ]
    for m in models:
        _models[DEFAULT_ORG][m.name] = m


# Auto-seed on import, unless this is a clean install. Gated because it wasn't:
# `make clean-start` and OSTIARI_NO_DEMO=1 promise an empty control plane, and the
# 18 model configs showed up anyway — this was the one seeder running at import time
# rather than from the gated block in app.py.
#
# On a clean install, register the models you use via POST /api/models, or call
# seed_models() to get the built-in set back. Tests call it explicitly, since
# conftest clears _models between them.
if os.environ.get("OSTIARI_NO_DEMO", "").lower() not in ("1", "true", "yes"):
    seed_models()
