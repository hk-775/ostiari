"""Entrypoint for the control plane server."""

import uvicorn
from control_plane.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8400)
