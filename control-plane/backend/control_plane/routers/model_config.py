"""Model configuration API — registry of available models and their routing config."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


# In-memory model registry
_models: dict[str, ModelConfig] = {}


@router.get("")
async def list_models() -> list[ModelConfig]:
    return list(_models.values())


@router.get("/{name}")
async def get_model(name: str) -> ModelConfig:
    if name not in _models:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    return _models[name]


@router.post("")
async def add_model(body: ModelConfig) -> ModelConfig:
    _models[body.name] = body
    return body


@router.put("/{name}")
async def update_model(name: str, body: ModelConfig) -> ModelConfig:
    if name not in _models:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    _models[name] = body
    return body


@router.delete("/{name}")
async def delete_model(name: str):
    if name not in _models:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    del _models[name]
    return {"deleted": name}


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
    ]
    for m in models:
        _models[m.name] = m


# Auto-seed on import
seed_models()
