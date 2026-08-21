"""Multi-strategy authentication middleware for AxonLLM.

Priority chain:
1. Application browser session cookie (CloudFront mode)
2. X-Amzn-Oidc-Data header (ALB OIDC JWT)
3. Authorization: Bearer <token> (OIDC JWT or API key if prefixed axon_)
4. X-Api-Key header
5. Anonymous -> 401
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from src.gateway.auth.browser_session import (
    BROWSER_AUTH_PATHS,
    CONFIG_PATH as BROWSER_AUTH_CONFIG_PATH,
    SESSION_COOKIE_NAME,
    BrowserSessionUnavailable,
    browser_session_cookie_values,
    valid_session_token,
)
from src.gateway.config_sync import RegionTopologyUnavailable
from src.gateway.models import AuthMethod, RequestContext

if TYPE_CHECKING:
    from src.gateway.auth.api_key_service import APIKeyService
    from src.gateway.auth.browser_session import BrowserSessionService
    from src.gateway.auth.oidc_service import OIDCService
    from src.gateway.auth.principal import PrincipalResolver

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({
    "/health",
    "/ready",
    # The landing page. Anonymous by definition — gating it behind auth would
    # mean only signed-in users could read the pitch.
    "/",
    "/admin/dashboard",
    "/chat",
    "/playground",
    "/routing",
})

SAML_HANDOFF_PATHS = frozenset(
    {
        "/saml/login",
        "/saml/acs",
        "/saml/metadata",
    }
)

APP_AUTHENTICATED_UI_PATHS = frozenset(
    {
        "/admin/dashboard",
        "/chat",
        "/playground",
        "/routing",
    }
)


def _is_browser_navigation(request: Request) -> bool:
    """Distinguish a document navigation from API and fetch/XHR requests."""
    if request.method.upper() != "GET":
        return False
    path = request.url.path
    if (
        path.startswith("/api/")
        or path.startswith("/v1/")
        or path.startswith("/scim/")
        or request.headers.get("x-requested-with", "").lower()
        == "xmlhttprequest"
    ):
        return False
    fetch_mode = request.headers.get("sec-fetch-mode")
    fetch_dest = request.headers.get("sec-fetch-dest")
    if fetch_mode is not None and fetch_mode.lower() != "navigate":
        return False
    if fetch_dest is not None and fetch_dest.lower() != "document":
        return False
    accepted = ",".join(request.headers.getlist("accept")).lower()
    return "text/html" in accepted


def _is_site_asset(path: str) -> bool:
    """True for the marketing site's pages and the assets they fetch.

    The landing page at "/" is public, and its nav links to architecture.html,
    which in turn fetches three SVGs plus the narration audio and its transcript
    from site/narration/. Gating those behind auth would serve the pitch to
    anonymous readers and then 401 the page it links to.

    Delegates the decision to ``_is_servable_site_path``, the same predicate the
    route handler applies, so "publicly routable" and "anonymous" cannot drift
    into a page that renders 200 with a 401 on the audio it plays. A path this
    admits but the handler rejects just 404s, so the coupling can only ever be
    too narrow, never too permissive.
    """
    from pathlib import PurePosixPath

    from src.gateway.admin.routes import _is_servable_site_path

    if not path.startswith("/"):
        return False
    return _is_servable_site_path(PurePosixPath(path.lstrip("/")))


class PolicyService(Protocol):
    """Interface for policy evaluation."""

    async def evaluate(self, context: RequestContext, action: str, resource: str) -> str:
        ...


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticates requests via OIDC JWT, API key, or rejects as anonymous."""

    def __init__(
        self,
        app,
        oidc_service: OIDCService | None = None,
        api_key_service: APIKeyService | None = None,
        policy_service: PolicyService | None = None,
        mode: str = "ENFORCE",
        public_paths: frozenset[str] | None = None,
        config_sync: object | None = None,
        principal_resolver: PrincipalResolver | None = None,
        require_canonical_principal: bool = False,
        browser_session_service: BrowserSessionService | None = None,
    ):
        super().__init__(app)
        self.oidc_service = oidc_service
        self.api_key_service = api_key_service
        self.policy_service = policy_service
        self.mode = mode
        self.public_paths = public_paths or PUBLIC_PATHS
        # Optional so every existing caller constructs unchanged and never polls.
        # Refreshed here rather than in GatewayAgent because the project and user
        # config gate more than chat — the admin reads and /api/users need the
        # same converged view, and one refresh per request serves all of them.
        self.config_sync = config_sync
        self.principal_resolver = principal_resolver
        self.require_canonical_principal = require_canonical_principal
        self.browser_session_service = browser_session_service

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip auth for public paths and static assets.
        # SCIM carries tenant-bound bearer authentication.  The three exact
        # SAML paths are either a managed-Cognito handoff or direct-SP
        # tombstones.  Do not exempt the whole /saml/* namespace: a future
        # endpoint must be authenticated unless it is explicitly reviewed.
        path = request.url.path
        app_authenticated_ui = bool(
            self.browser_session_service is not None
            and path in APP_AUTHENTICATED_UI_PATHS
        )
        if (
            (path in self.public_paths and not app_authenticated_ui)
            or path.startswith("/admin/static")
            or path.startswith("/chat/static")
            or path.startswith("/scim/")
            or path in SAML_HANDOFF_PATHS
            or path == BROWSER_AUTH_CONFIG_PATH
            or (
                self.browser_session_service is not None
                and path in BROWSER_AUTH_PATHS
            )
            or _is_site_asset(path)
        ):
            request.state.context = RequestContext(
                user_id="anonymous",
                project_id="",
                roles=[],
                scopes=[],
                auth_method=AuthMethod.ANONYMOUS,
            )
            request.state.principal = None
            return await call_next(request)

        context = None
        request.state.browser_session_authenticated = False

        # Credential families are mutually exclusive. Presence is
        # authoritative: malformed browser/ALB material cannot fall through to
        # a bearer token or API key supplied alongside it.
        browser_tokens = browser_session_cookie_values(
            request.headers.getlist("cookie")
        )
        browser_auth_attempted = bool(browser_tokens)
        # ambiguous ALB credential must not fall through to another auth method.
        alb_tokens = request.headers.getlist("x-amzn-oidc-data")
        alb_identities = request.headers.getlist("x-amzn-oidc-identity")
        alb_auth_attempted = bool(alb_tokens or alb_identities)
        authorization_headers = request.headers.getlist("authorization")
        api_key_headers = request.headers.getlist("x-api-key")
        header_auth_attempted = bool(
            authorization_headers or api_key_headers
        )
        competing_credentials = (
            sum(
                (
                    bool(browser_auth_attempted),
                    bool(alb_auth_attempted),
                    bool(authorization_headers),
                    bool(api_key_headers),
                )
            )
            > 1
            or (authorization_headers and api_key_headers)
            or len(authorization_headers) > 1
            or len(api_key_headers) > 1
            or len(browser_tokens) > 1
        )

        # 1. CloudFront application browser session.
        if (
            browser_auth_attempted
            and not competing_credentials
            and len(browser_tokens) == 1
            and valid_session_token(browser_tokens[0])
            and self.browser_session_service is not None
        ):
            try:
                context = await self.browser_session_service.authenticate(
                    browser_tokens[0]
                )
            except BrowserSessionUnavailable:
                logger.exception("Browser session authority is unavailable")
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "service_unavailable",
                            "code": "browser_session_unavailable",
                            "message": (
                                "Browser authentication is temporarily "
                                "unavailable."
                            ),
                        }
                    },
                )
            request.state.browser_session_authenticated = context is not None

        # 2. ALB OIDC headers.
        if (
            context is None
            and not browser_auth_attempted
            and alb_auth_attempted
            and not competing_credentials
            and self.oidc_service
            and len(alb_tokens) == 1
            and len(alb_identities) == 1
            and alb_tokens[0]
            and alb_identities[0]
        ):
            context = await self.oidc_service.validate_alb_jwt(
                alb_tokens[0],
                expected_subject=alb_identities[0],
            )

        # 3. Authorization: Bearer <token>
        if (
            context is None
            and not browser_auth_attempted
            and not alb_auth_attempted
            and not competing_credentials
            and len(authorization_headers) == 1
        ):
            auth_header = authorization_headers[0]
            if (
                auth_header.startswith("Bearer ")
                and auth_header[7:]
                and auth_header == auth_header.strip()
            ):
                token = auth_header[7:]
                if token.startswith("axon_"):
                    context = await self._authenticate_api_key(token)
                elif self.oidc_service:
                    context = await self.oidc_service.validate_oidc_jwt(token)

        # 4. X-Api-Key header
        if (
            context is None
            and not browser_auth_attempted
            and not alb_auth_attempted
            and not competing_credentials
            and not authorization_headers
            and len(api_key_headers) == 1
        ):
            api_key_header = api_key_headers[0]
            if api_key_header and api_key_header == api_key_header.strip():
                context = await self._authenticate_api_key(api_key_header)

        # 5. No credentials - redirect browser document navigations in app
        # session mode, otherwise preserve JSON 401 semantics for APIs/XHR.
        if context is None:
            if self.mode == "ENFORCE":
                if (
                    self.browser_session_service is not None
                    and not alb_auth_attempted
                    and not header_auth_attempted
                    and not competing_credentials
                    and _is_browser_navigation(request)
                ):
                    return_to = path
                    if request.url.query:
                        return_to = f"{return_to}?{request.url.query}"
                    try:
                        location = (
                            self.browser_session_service.login_url(return_to)
                        )
                    except ValueError:
                        location = (
                            self.browser_session_service.login_url()
                        )
                    response = RedirectResponse(location, status_code=302)
                    if browser_auth_attempted:
                        response.delete_cookie(
                            SESSION_COOKIE_NAME,
                            path="/",
                            secure=True,
                            httponly=True,
                            samesite="lax",
                        )
                    response.headers["cache-control"] = "no-store"
                    response.headers["pragma"] = "no-cache"
                    return response
                error: dict[str, str] = {
                    "type": "authentication_error",
                    "message": (
                        "Missing or invalid credentials. Provide a Bearer "
                        "token or X-Api-Key header."
                    ),
                }
                if self.browser_session_service is not None:
                    try:
                        login_url = (
                            self.browser_session_service.login_url(path)
                        )
                    except ValueError:
                        login_url = (
                            self.browser_session_service.login_url()
                        )
                    error.update(
                        {
                            "code": "browser_login_required",
                            "login_url": login_url,
                        }
                    )
                return JSONResponse(
                    status_code=401,
                    content={"error": error},
                )
            else:
                context = RequestContext(
                    user_id="anonymous",
                    project_id="",
                    roles=[],
                    scopes=[],
                    auth_method=AuthMethod.ANONYMOUS,
                )

        principal = None
        if context.auth_method is not AuthMethod.ANONYMOUS:
            if self.principal_resolver is not None:
                try:
                    principal = await self.principal_resolver.resolve(context)
                except Exception:
                    logger.exception(
                        "Canonical principal resolution is unavailable"
                    )
                    if self.mode == "ENFORCE":
                        return JSONResponse(
                            status_code=503,
                            content={
                                "error": {
                                    "type": "authorization_error",
                                    "message": (
                                        "Canonical principal resolution is "
                                        "temporarily unavailable."
                                    ),
                                    "code": "principal_resolver_unavailable",
                                }
                            },
                        )
                if principal is None:
                    if self.mode == "ENFORCE":
                        return JSONResponse(
                            status_code=403,
                            content={
                                "error": {
                                    "type": "authorization_error",
                                    "message": (
                                        "No active tenant membership exists for "
                                        "this credential."
                                    ),
                                    "code": "tenant_membership_required",
                                }
                            },
                        )
                    logger.warning(
                        "Canonical principal resolution failed user=%s tenant_hint=%s",
                        context.user_id,
                        context.tenant_id,
                    )
                else:
                    from src.gateway.auth.principal import canonical_request_context

                    context = canonical_request_context(context, principal)
            elif self.require_canonical_principal:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "configuration_error",
                            "message": "Canonical principal resolution is unavailable.",
                            "code": "principal_resolver_unavailable",
                        }
                    },
                )

        request.state.context = context
        request.state.principal = principal

        # Adopt any project or user config another instance wrote, before the
        # handler reads either. Both gate requests rather than decorate them — an
        # unresolved project means no budget limit, no allowed-models list and no
        # rate limit, and a missing user config means no per-user model
        # restriction — so a write that reached only one task made enforcement a
        # function of which task the balancer picked. Rate-limited to one counter
        # read per CONFIG_SYNC_TTL_SECONDS and a no-op without persistence.
        if self.config_sync is not None:
            try:
                await self.config_sync.refresh_if_stale()
            except RegionTopologyUnavailable:
                logger.error(
                    "Authoritative region topology refresh failed",
                    exc_info=True,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "service_unavailable",
                            "message": (
                                "Region routing configuration is temporarily "
                                "unavailable."
                            ),
                            "code": "region_topology_unavailable",
                        }
                    },
                )
            except Exception:
                # Project/user refresh retains the last loaded enforcement state.
                # Topology failures are handled above because stale residency and
                # failover rules cannot safely authorize routing.
                logger.warning("Config refresh failed", exc_info=True)

        # Policy evaluation
        if self.policy_service:
            # Adopt any policy another instance wrote before deciding this
            # request. Statements are compiled once, so a policy written through
            # POST /admin/policies previously took effect only on the task that
            # served the write — behind desired_count=2 an operator's forbid was
            # enforced by one task and ignored by the other, per request, decided
            # by the load balancer. Rate-limited to one counter read per
            # POLICY_SYNC_TTL_SECONDS and a no-op without persistence.
            refresh = getattr(self.policy_service, "refresh_if_stale", None)
            if refresh is not None:
                try:
                    await refresh()
                except Exception:
                    # Never fail a request because the refresh failed; the
                    # already-compiled set still decides it.
                    logger.warning("Policy refresh failed", exc_info=True)
            action = request.method.lower()
            resource = path
            try:
                decision = await self.policy_service.evaluate(
                    context,
                    action,
                    resource,
                )
            except Exception:
                logger.error(
                    "Policy evaluation unavailable user=%s tenant=%s resource=%s",
                    context.user_id,
                    context.tenant_id,
                    resource,
                    exc_info=True,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "service_unavailable",
                            "message": "Authorization is temporarily unavailable.",
                            "code": "policy_evaluation_unavailable",
                        }
                    },
                )

            if decision == "DENY":
                if self.mode == "ENFORCE":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "type": "authorization_error",
                                "message": "Access denied by policy.",
                            }
                        },
                    )
                else:
                    logger.warning(
                        "Policy DENY (LOG_ONLY) user=%s project=%s action=%s resource=%s",
                        context.user_id,
                        context.project_id,
                        action,
                        resource,
                    )

        return await call_next(request)

    async def _authenticate_api_key(self, raw_key: str) -> RequestContext | None:
        """Validate API key and return context."""
        if not self.api_key_service:
            return None

        key_record = await self.api_key_service.validate_key(raw_key)
        if key_record is None:
            return None

        return RequestContext(
            user_id=f"apikey:{key_record.key_id}",
            project_id=key_record.project_id,
            roles=["service"],
            scopes=key_record.scopes,
            auth_method=AuthMethod.API_KEY,
            tenant_id=key_record.tenant_id,
            api_key_id=key_record.key_id,
            issuer="urn:axonllm:api-key",
            subject=key_record.key_id,
        )
