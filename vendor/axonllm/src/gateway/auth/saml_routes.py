"""Managed Cognito SAML handoff and direct-SP tombstone endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from src.gateway.auth.saml_service import SamlService

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}
_MANAGED_MESSAGE = (
    "SAML authentication is managed by Cognito and the control-plane login. "
    "AxonLLM does not accept SAML assertions directly."
)


def _error(
    status_code: int,
    *,
    error_type: str,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "type": error_type,
                "code": code,
                "message": message,
            }
        },
        headers=_NO_STORE_HEADERS,
    )


class SamlAPI:
    """HTTP contract for managed Cognito enterprise SAML login."""

    def __init__(self, service: SamlService) -> None:
        self.service = service

    def _disabled(self) -> JSONResponse | None:
        if self.service.enabled:
            return None
        return _error(
            503,
            error_type="sso_not_configured",
            code="managed_cognito_federation_required",
            message=_MANAGED_MESSAGE,
        )

    async def login(self, request: Request) -> Response:
        """Redirect to the configured ALB or application Cognito handoff."""
        if (disabled := self._disabled()) is not None:
            return disabled

        from src.gateway.auth.saml_service import SamlError

        keys = set(request.query_params)
        return_values = request.query_params.getlist("return_to")
        if keys.difference({"return_to"}) or len(return_values) > 1:
            return _error(
                400,
                error_type="invalid_request",
                code="invalid_login_parameters",
                message=(
                    "Only one same-origin return_to parameter is supported. "
                    "Cognito owns SAML RelayState."
                ),
            )
        try:
            target = self.service.login_target(
                return_values[0] if return_values else None
            )
        except SamlError:
            return _error(
                400,
                error_type="invalid_request",
                code="invalid_return_to",
                message="return_to must identify a protected same-origin path.",
            )

        response = RedirectResponse(target, status_code=302)
        response.headers.update(_NO_STORE_HEADERS)
        return response

    async def acs(self, request: Request) -> Response:
        """Reject every direct SAML assertion without parsing the request body."""
        return _error(
            410,
            error_type="direct_saml_disabled",
            code="managed_cognito_federation_required",
            message=_MANAGED_MESSAGE,
        )

    async def metadata(self, request: Request) -> Response:
        """Reject app SP metadata; Cognito is the configured service provider."""
        return _error(
            410,
            error_type="direct_saml_disabled",
            code="use_cognito_sp_metadata",
            message=(
                "AxonLLM is not a SAML service provider. Configure the identity "
                "provider with the Cognito user pool's SP metadata."
            ),
        )


def create_saml_routes(api: SamlAPI) -> list[Route]:
    return [
        Route("/saml/login", api.login, methods=["GET"]),
        Route("/saml/acs", api.acs, methods=["POST"]),
        Route("/saml/metadata", api.metadata, methods=["GET"]),
    ]
