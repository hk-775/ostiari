"""Bedrock AgentCore HTTP bridge into an Ostiari-governed gateway."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Ostiari AgentCore Bridge")


@dataclass
class _Token:
    value: str
    expires_at: float


_token: _Token | None = None
_secret: str | None = None


def _client_secret() -> str:
    global _secret
    if _secret is not None:
        return _secret
    arn = os.environ.get("OSTIARI_AGENT_CLIENT_SECRET_ARN", "").strip()
    if not arn:
        return ""
    try:
        response = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError("Agent OAuth secret is unavailable") from exc
    value = response.get("SecretString", "")
    if not value:
        raise RuntimeError("Agent OAuth secret has no SecretString")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        value = str(
            parsed.get("client_secret") or parsed.get("value") or parsed.get("secret") or ""
        )
    if not value:
        raise RuntimeError("Agent OAuth secret is empty")
    _secret = value
    return value


async def _access_token() -> str:
    global _token
    token_url = os.environ.get("OSTIARI_AGENT_TOKEN_URL", "").strip()
    client_id = os.environ.get("OSTIARI_AGENT_CLIENT_ID", "").strip()
    if not token_url and not client_id:
        return ""
    if not token_url or not client_id:
        raise RuntimeError("Agent OAuth token URL and client id must be set together")
    now = time.monotonic()
    if _token and _token.expires_at - 30 > now:
        return _token.value

    data = {"grant_type": "client_credentials"}
    audience = os.environ.get("OSTIARI_AGENT_AUDIENCE", "").strip()
    if audience:
        data["audience"] = audience
    scope = os.environ.get("OSTIARI_AGENT_SCOPE", "").strip()
    if scope:
        data["scope"] = scope
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            token_url,
            data=data,
            auth=(client_id, _client_secret()),
        )
        response.raise_for_status()
        payload = response.json()
    value = str(payload.get("access_token", ""))
    if not value:
        raise RuntimeError("Agent OAuth response has no access_token")
    expires_in = max(60, int(payload.get("expires_in", 300)))
    _token = _Token(value=value, expires_at=now + expires_in)
    return value


def _validation_payload(body: dict[str, Any]) -> dict[str, Any]:
    candidate = body.get("input") if isinstance(body.get("input"), dict) else body
    action = str(candidate.get("action", "")).strip()
    params = candidate.get("params", {})
    if not action:
        raise ValueError("action is required")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return {"action": action, "params": params}


@app.get("/health")
@app.get("/ping")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "ostiari-agentcore-bridge"}


@app.post("/invocations")
async def invoke(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        payload = _validation_payload(body)
        token = await _access_token()
        headers = {
            "X-Agent-Id": os.environ.get("OSTIARI_AGENT_ID", "agentcore-runtime"),
            "X-Framework": os.environ.get("OSTIARI_FRAMEWORK", "bedrock-agentcore"),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        gateway = os.environ["OSTIARI_GATEWAY_URL"].rstrip("/")
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{gateway}/validate",
                json=payload,
                headers=headers,
            )
        return JSONResponse(
            status_code=response.status_code,
            content=response.json(),
        )
    except (KeyError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except (httpx.HTTPError, RuntimeError) as exc:
        return JSONResponse(
            status_code=502,
            content={"error": "Ostiari gateway validation unavailable", "detail": str(exc)},
        )
