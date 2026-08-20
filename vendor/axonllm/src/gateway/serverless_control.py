"""AWS Lambda entry point for the request-driven control API."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import json
import os
import re
from typing import Any, Callable

from src.gateway.bootstrap import (
    build_control_api,
    build_control_components,
)
from src.gateway.config_loader import load_app_config


_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def _trusted_public_host(event: dict) -> str:
    headers = event.get("headers")
    if not isinstance(headers, dict):
        raise RuntimeError("CloudFront public-host header is missing")
    value = next(
        (
            header_value
            for header_name, header_value in headers.items()
            if isinstance(header_name, str)
            and header_name.lower() == "x-axon-public-host"
        ),
        None,
    )
    if (
        not isinstance(value, str)
        or _HOSTNAME.fullmatch(value.strip().lower()) is None
    ):
        raise RuntimeError("CloudFront public-host header is invalid")
    return value.strip().lower()


@lru_cache(maxsize=1)
def _browser_client_id() -> str:
    pool_id = os.environ.get("AXON_COGNITO_USER_POOL_ID", "").strip()
    client_name = os.environ.get(
        "AXON_COGNITO_BROWSER_CLIENT_NAME",
        "",
    ).strip()
    if not pool_id or not client_name:
        raise RuntimeError("Cognito browser-client discovery is incomplete")
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "boto3 is required by the serverless-control host"
        ) from exc

    client = boto3.client(
        "cognito-idp",
        region_name=(
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        ),
    )
    matches: list[str] = []
    token: str | None = None
    try:
        while True:
            request: dict[str, object] = {
                "UserPoolId": pool_id,
                "MaxResults": 60,
            }
            if token is not None:
                request["NextToken"] = token
            response = client.list_user_pool_clients(**request)
            for item in response.get("UserPoolClients", []):
                if (
                    item.get("ClientName") == client_name
                    and isinstance(item.get("ClientId"), str)
                ):
                    matches.append(item["ClientId"])
            token = response.get("NextToken")
            if not isinstance(token, str) or not token:
                break
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            "Cognito browser-client discovery failed"
        ) from exc
    if len(matches) != 1:
        raise RuntimeError(
            "Cognito browser-client discovery requires exactly one match"
        )
    return matches[0]


@lru_cache(maxsize=1)
def _scim_tenant_configuration() -> str | None:
    secret_arn = os.environ.get(
        "AXON_SCIM_TENANTS_SECRET_ARN",
        "",
    ).strip()
    if not secret_arn:
        return None
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "boto3 is required by the serverless-control host"
        ) from exc
    client = boto3.client(
        "secretsmanager",
        region_name=(
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        ),
    )
    try:
        response = client.get_secret_value(SecretId=secret_arn)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            "SCIM tenant configuration could not be loaded"
        ) from exc
    value = response.get("SecretString")
    if not isinstance(value, str) or not value:
        raise RuntimeError(
            "SCIM tenant configuration must be a non-empty SecretString"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "SCIM tenant configuration is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError(
            "SCIM tenant configuration must be a non-empty object"
        )
    return value


def _prepare_runtime_environment(event: dict) -> None:
    public_url = f"https://{_trusted_public_host(event)}"
    configured_url = os.environ.get("AXON_CONTROL_PLANE_URL", "").strip()
    if configured_url and configured_url != public_url:
        raise RuntimeError(
            "CloudFront public host changed within one Lambda environment"
        )
    client_id = _browser_client_id()
    configured_client = os.environ.get("AXON_OIDC_AUDIENCE", "").strip()
    if configured_client and configured_client != client_id:
        raise RuntimeError(
            "Cognito browser client changed within one Lambda environment"
        )
    os.environ["AXON_CONTROL_PLANE_URL"] = public_url
    os.environ["AXON_OIDC_AUDIENCE"] = client_id
    os.environ["AXON_BROWSER_AUTH_CLIENT_ID"] = client_id
    scim_tenants = _scim_tenant_configuration()
    if scim_tenants is not None:
        os.environ["AXON_SCIM_TENANTS"] = scim_tenants


@lru_cache(maxsize=1)
def build_lambda_application():
    """Build and cache the control-only ASGI application."""

    config = load_app_config()
    if not config.control_plane_only:
        raise RuntimeError(
            "serverless control API requires AXON_CONTROL_PLANE_ONLY=true"
        )
    components = build_control_components(config)
    export_environment = (
        os.environ.get("AXON_EXPORT_BUCKET_NAME", "").strip(),
        os.environ.get("AXON_EXPORT_QUEUE_URL", "").strip(),
    )
    if any(export_environment):
        if not all(export_environment):
            raise RuntimeError(
                "serverless export configuration is incomplete"
            )
        from src.gateway.export_jobs import build_export_job_service

        components = replace(
            components,
            export_jobs=build_export_job_service(),
        )
    return build_control_api(config, components)


@lru_cache(maxsize=1)
def _lambda_adapter() -> Callable[[dict, Any], dict]:
    """Create the ASGI adapter once per warm Lambda environment."""

    try:
        from mangum import Mangum
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "Mangum is required by the serverless-control host"
        ) from exc
    return Mangum(
        build_lambda_application(),
        lifespan="off",
    )


def lambda_handler(event: dict, context: Any) -> dict:
    """Handle a Lambda Function URL event through the control-only app."""

    _prepare_runtime_environment(event)
    return _lambda_adapter()(event, context)


__all__ = ["build_lambda_application", "lambda_handler"]
