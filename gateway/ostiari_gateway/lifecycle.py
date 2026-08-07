"""Gateway lifecycle management — registration + heartbeat loop with the Control Plane."""

import asyncio
import contextlib
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("ostiari.lifecycle")


class LifecycleManager:
    """Manages gateway registration and heartbeat with the control plane."""

    def __init__(
        self, gateway_id: str, control_plane_url: str, callback_url: str = ""
    ) -> None:
        self._gateway_id = gateway_id
        self._cp_url = control_plane_url.rstrip("/")
        self._callback_url = callback_url
        self._heartbeat_task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=10.0)
        self._config_callback: Any = None

    @staticmethod
    def _headers() -> dict[str, str]:
        token = os.environ.get("OSTIARI_SERVICE_TOKEN", "").strip()
        return {"X-Ostiari-Service-Key": token} if token else {}

    @property
    def gateway_id(self) -> str:
        return self._gateway_id

    def set_config_callback(self, callback: Any) -> None:
        """Set a callback function(bundle) to apply config updates."""
        self._config_callback = callback

    async def register(self) -> dict[str, Any]:
        """POST to /api/gateways/{id}/register. Returns config bundle."""
        url = f"{self._cp_url}/api/gateways/{self._gateway_id}/register"
        # Advertise our callback URL so the control plane can push config back.
        body = {"callback_url": self._callback_url} if self._callback_url else {}
        try:
            resp = await self._client.post(url, json=body, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            log.info(f"Registered with control plane: {self._cp_url}")
            # Apply config bundle if callback is set
            if self._config_callback and "config" in data:
                self.apply_config(data["config"])
            # Then drain any partial updates queued while we were down. These are
            # applied AFTER the full bundle so a queued change wins over the
            # baseline it was meant to amend — same ordering as the heartbeat path.
            if self._config_callback:
                for update in data.get("config_updates") or []:
                    self.apply_config(update)
            return data
        except httpx.HTTPStatusError as e:
            log.error(f"Registration failed: HTTP {e.response.status_code}")
            raise
        except httpx.ConnectError as e:
            log.error(f"Cannot reach control plane at {self._cp_url}: {e}")
            raise

    async def start_heartbeat(self, interval: int = 30) -> None:
        """Start background heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))
        log.info(f"Heartbeat started (interval={interval}s)")

    async def _heartbeat_loop(self, interval: int) -> None:
        """Send heartbeat every `interval` seconds."""
        url = f"{self._cp_url}/api/gateways/{self._gateway_id}/heartbeat"
        while True:
            await asyncio.sleep(interval)
            try:
                resp = await self._client.post(url, headers=self._headers())
                if resp.status_code == 200:
                    data = resp.json()
                    # Apply config if the CP sent updates (reconnect or queued)
                    if "config" in data and self._config_callback:
                        self.apply_config(data["config"])
                    if "config_updates" in data and self._config_callback:
                        for update in data["config_updates"]:
                            self.apply_config(update)
                else:
                    log.warning(f"Heartbeat got HTTP {resp.status_code}")
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                log.warning(f"Heartbeat failed: {e}")
            except Exception as e:
                log.error(f"Heartbeat error: {e}")

    async def pull_config(self) -> dict[str, Any]:
        """GET /api/gateways/{id}/config-bundle — fetch full config."""
        url = f"{self._cp_url}/api/gateways/{self._gateway_id}/config-bundle"
        resp = await self._client.get(url, headers=self._headers())
        resp.raise_for_status()
        bundle = resp.json()
        if self._config_callback:
            self.apply_config(bundle)
        return bundle

    def apply_config(self, bundle: dict[str, Any]) -> None:
        """Apply a config bundle to the gateway via the registered callback."""
        if self._config_callback:
            try:
                self._config_callback(bundle)
                log.info("Config bundle applied")
            except Exception as e:
                log.error(f"Failed to apply config bundle: {e}")
        else:
            log.warning("No config callback registered — config not applied")

    async def stop(self) -> None:
        """Cancel heartbeat task and close HTTP client."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            # We just cancelled it, so CancelledError is the expected outcome,
            # not a failure to report.
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        await self._client.aclose()
        log.info("Lifecycle manager stopped")
