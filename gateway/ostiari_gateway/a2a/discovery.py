"""A2A agent discovery — fetches agent cards from /.well-known/agent.json."""

import logging

import httpx

from ostiari_gateway.a2a.models import AgentCard

log = logging.getLogger("ostiari.gateway.a2a.discovery")

WELL_KNOWN_PATH = "/.well-known/agent.json"


async def fetch_agent_card(
    url: str,
    timeout: float = 10.0,
    auth_token: str = "",
) -> AgentCard:
    """Fetch and parse an A2A agent card from the well-known endpoint.

    Args:
        url: Base URL of the agent (e.g. "https://agent.example.com").
             If it already ends with the well-known path, it's used as-is.
        timeout: Request timeout in seconds.
        auth_token: Optional bearer token for authenticated discovery.
    """
    base = url.rstrip("/")
    discovery_url = base if base.endswith(WELL_KNOWN_PATH) else base + WELL_KNOWN_PATH

    # SSRF guard: the agent URL is request-supplied (and A2A cards can even
    # redeclare their own url), so validate before fetching and disable redirects.
    from ostiari.net_guard import validate_public_url
    validate_public_url(discovery_url)

    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.get(discovery_url, headers=headers)
        response.raise_for_status()

    card = AgentCard(**response.json())

    if not card.url:
        card.url = base
    # The discovery document is untrusted and may advertise a different task
    # endpoint. Validate that second URL too so a public discovery service
    # cannot pivot the gateway to cloud metadata or an internal service.
    validate_public_url(card.url)

    log.info(
        "Discovered A2A agent '%s' at %s (%d skills)",
        card.name, card.url, len(card.skills),
    )
    return card
