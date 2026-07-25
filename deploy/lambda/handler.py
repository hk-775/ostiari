"""AWS Lambda handler for the Ostiari gateway using Mangum.

Builds the FastAPI app from environment-driven config (OSTIARI_GATEWAY_ID /
OSTIARI_CONTROL_PLANE_URL / OSTIARI_PORT) — the same knobs used by the container
deployments.

NOTE: Lambda suits stateless request/response validation. The gateway's
heartbeat/config-push loop is a long-lived background task and does NOT run
under Lambda (lifespan is off). For a gateway that stays registered and
receives pushed config, use the ECS/Kubernetes deployments instead; use Lambda
only for on-demand, pull-based validation.
"""

import os

from mangum import Mangum

from ostiari_gateway.models import SidecarConfig
from ostiari_gateway.server import create_app

_config = SidecarConfig(
    sidecar_id=os.environ.get("OSTIARI_GATEWAY_ID", "gateway-lambda-1"),
    control_plane_url=os.environ.get("OSTIARI_CONTROL_PLANE_URL", ""),
)

app = create_app(initial_config=_config)

handler = Mangum(app, lifespan="off")
