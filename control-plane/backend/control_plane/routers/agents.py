"""Agent registry API — tracks agents across frameworks."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentConfig(BaseModel):
    name: str
    framework: str
    gateway_id: str = ""
    tools: list[str] = Field(default_factory=list)
    description: str = ""
    status: str = "registered"
    model: str = ""


# Live agent registry. Empty by default — populated at runtime as agents
# register (POST /api/agents) or, in demo mode, by seed_demo_agents() which
# loads DEMO_AGENTS below. A clean/no-demo install starts with no agents.
_agents: dict[str, AgentConfig] = {}


# Demo agents — loaded ONLY by the demo seeder (gated by OSTIARI_NO_DEMO), so a
# clean install isn't pre-populated with sample data.
DEMO_AGENTS: dict[str, AgentConfig] = {
    "research-agent": AgentConfig(
        name="research-agent", framework="openai", gateway_id="crm-agent",
        tools=["web_search", "file_read", "file_write", "execute_code"],
        description="OpenAI-powered research agent — web search, file I/O, code execution",
        model="gpt-4o",
    ),
    "ops-agent": AgentConfig(
        name="ops-agent", framework="strands", gateway_id="ops-agent",
        tools=["db_query", "db_delete", "file_delete", "send_email"],
        description="Strands operations agent — database, file management, notifications",
        model="claude-sonnet-4-6",
    ),
    "claude-agent": AgentConfig(
        name="claude-agent", framework="anthropic", gateway_id="crm-agent",
        tools=["web_search", "file_read", "file_write", "send_email", "execute_code"],
        description="Claude agent — research, writing, email, code execution",
        model="claude-sonnet-4-6",
    ),
    "bedrock-agent": AgentConfig(
        name="bedrock-agent", framework="bedrock", gateway_id="crm-agent",
        tools=["web_search", "file_read", "db_query", "send_email"],
        description="AWS Bedrock agent — Converse API with tool use",
        model="bedrock/anthropic.claude-3-5-sonnet",
    ),
    "agentcore-agent": AgentConfig(
        name="agentcore-agent", framework="agentcore", gateway_id="crm-agent",
        tools=["web_search", "file_read", "file_write", "db_query", "send_email"],
        description="AgentCore runtime pattern — hosted agent with tool invocations",
        model="bedrock/anthropic.claude-3-5-sonnet",
    ),
    "crewai-agent": AgentConfig(
        name="crewai-agent", framework="crewai", gateway_id="crm-agent",
        tools=["web_search", "file_read", "file_write", "execute_code"],
        description="CrewAI collaborative agent — multi-agent task execution",
        model="gpt-4o",
    ),
    "langgraph-agent": AgentConfig(
        name="langgraph-agent", framework="langgraph", gateway_id="crm-agent",
        tools=["web_search", "file_read", "file_write", "db_query", "execute_code"],
        description="LangGraph stateful agent — graph-based workflow with checkpoints",
        model="gpt-4o",
    ),
    "planner-bot": AgentConfig(
        name="planner-bot", framework="gateway-invoke", gateway_id="crm-agent",
        tools=["db_query", "send_email", "github.search_code", "github.create_issue", "drawio.create_diagram", "drawio.add_shape"],
        description="Multi-step planner — research, document, notify via gateway /invoke",
        model="claude-sonnet-4-6",
    ),
    "smart-router-bot": AgentConfig(
        name="smart-router-bot", framework="gateway-invoke", gateway_id="crm-agent",
        tools=["db_query", "send_email", "github.search_code", "github.list_repos", "drawio.create_diagram"],
        description="Smart routing demo — different tasks routed to different models",
        model="auto (routed)",
    ),
}


@router.get("")
async def list_agents() -> list[AgentConfig]:
    return list(_agents.values())


@router.get("/{name}")
async def get_agent(name: str) -> AgentConfig:
    if name not in _agents:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    return _agents[name]


@router.post("")
async def register_agent(body: AgentConfig) -> AgentConfig:
    _agents[body.name] = body
    return body


@router.delete("/{name}")
async def delete_agent(name: str):
    if name not in _agents:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    del _agents[name]
    return {"deleted": name}
