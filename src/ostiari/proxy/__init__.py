"""Ostiari Proxy — sidecar that validates and executes tool calls on behalf of agents."""

from ostiari.proxy.server import create_app, run_proxy

__all__ = ["create_app", "run_proxy"]
