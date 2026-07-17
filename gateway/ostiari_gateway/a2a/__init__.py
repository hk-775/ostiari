"""A2A integration — connects to remote A2A agents and exposes this gateway as one."""

from ostiari_gateway.a2a.client import A2AClient
from ostiari_gateway.a2a.manager import A2AManager
from ostiari_gateway.a2a.models import AgentCard as A2AAgentCard

__all__ = ["A2AManager", "A2AClient", "A2AAgentCard"]
