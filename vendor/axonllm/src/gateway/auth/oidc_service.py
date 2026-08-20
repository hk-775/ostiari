"""OIDC/JWT authentication — ALB and direct OIDC token validation."""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit

from src.gateway.models import AuthMethod, RequestContext

logger = logging.getLogger(__name__)

ALB_JWT_ALGORITHM = "ES256"
MAX_ALB_JWT_BYTES = 64 * 1024
MAX_ALB_KEY_BYTES = 16 * 1024
MAX_ALB_KEY_CACHE_ENTRIES = 16
MAX_ALB_KEY_CACHE_TTL_SECONDS = 3600
DIRECT_OIDC_ALGORITHMS = ("RS256", "ES256")
_DIRECT_OIDC_KEY_TYPES = {"RS256": "RSA", "ES256": "EC"}
MAX_DIRECT_OIDC_JWT_BYTES = 64 * 1024
MAX_DIRECT_OIDC_HEADER_BYTES = 4 * 1024
MAX_DIRECT_OIDC_KID_BYTES = 256
MAX_OIDC_URL_BYTES = 2048
MAX_OIDC_DISCOVERY_BYTES = 64 * 1024
MAX_OIDC_JWKS_BYTES = 256 * 1024
MAX_OIDC_JWKS_KEYS = 64
OIDC_HTTP_TOTAL_TIMEOUT_SECONDS = 5.0
OIDC_HTTP_CONNECT_TIMEOUT_SECONDS = 2.0
UNKNOWN_KID_REFRESH_INTERVAL_SECONDS = 30.0
MAX_JWKS_CACHE_TTL_SECONDS = 3600
_ALB_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_NUMERIC_HOST_LABEL_PATTERN = re.compile(r"^(?:0[xX][0-9A-Fa-f]+|[0-9]+)$")
_INVALID_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")
_AWS_DNS_SUFFIXES = {
    "aws": "amazonaws.com",
    "aws-cn": "amazonaws.com.cn",
    "aws-us-gov": "amazonaws.com",
}


@dataclass
class OIDCConfig:
    """Configuration for OIDC authentication."""

    issuer: str = ""
    audience: str = ""
    alb_region: str = "us-east-1"
    alb_signer_arn: str = ""
    alb_client_id: str = ""
    alb_issuer: str = ""
    alb_key_cache_ttl: int = 300
    jwks_cache_ttl: int = 3600
    claim_mappings: dict = field(
        default_factory=lambda: {
            "user_id": "sub",
            "email": "email",
            "project_id": "custom:project_id",
            "tenant_id": "custom:tenant_id",
            "business_unit": "custom:business_unit",
            "roles": "custom:roles",
        }
    )


class OIDCService:
    """Validates OIDC JWTs from ALB or direct Bearer tokens.

    Supports:
    - ALB-injected tokens (X-Amzn-Oidc-Data, ES256, regional public keys)
    - Standard OIDC Bearer tokens (RS256/ES256, JWKS discovery)
    """

    def __init__(self, config: OIDCConfig) -> None:
        self._config = config
        self._alb_key_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._jwks_cache: dict[str, Any] = {}
        self._jwks_cache_issuer: str | None = None
        self._jwks_fetched_at: float = 0
        self._jwks_generation = 0
        self._jwks_last_unknown_refresh_at = float("-inf")
        self._jwks_refresh_lock = asyncio.Lock()

    async def validate_alb_jwt(
        self,
        token: str,
        expected_subject: str,
    ) -> RequestContext | None:
        """Validate JWT from X-Amzn-Oidc-Data header (ALB-signed ES256).

        ALB trust metadata lives in the protected JWT header. It must be bound
        to one configured ALB and OIDC client before the regional key endpoint
        is contacted. The signed subject must also match X-Amzn-Oidc-Identity.
        """
        try:
            trust = self._alb_trust_config()
            identity_issuer = self._validated_oidc_issuer()
            if trust is None or identity_issuer is None:
                return None
            signer, client_id, issuer, key_base_url = trust

            if (
                not isinstance(token, str)
                or not token
                or len(token.encode("utf-8")) > MAX_ALB_JWT_BYTES
                or not isinstance(expected_subject, str)
                or not expected_subject
                or len(expected_subject.encode("utf-8")) > 2048
            ):
                return None

            header = self._decode_jwt_header(token)
            if header is None:
                return None

            kid = header.get("kid")
            if (
                header.get("alg") != ALB_JWT_ALGORITHM
                or header.get("signer") != signer
                or header.get("client") != client_id
                or header.get("iss") != issuer
                or not isinstance(kid, str)
                or _ALB_KEY_ID_PATTERN.fullmatch(kid) is None
            ):
                return None

            expires_at = header.get("exp")
            if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= int(time.time()):
                return None

            public_key = await self._get_alb_public_key(kid, key_base_url)
            if public_key is None:
                return None

            claims = self._verify_and_decode(
                token,
                public_key,
                algorithms=[ALB_JWT_ALGORITHM],
                required_claims=("sub",),
            )
            if claims is None:
                return None

            subject = claims.get("sub")
            if (
                not isinstance(subject, str)
                or not subject
                or len(subject.encode("utf-8")) > 2048
                or subject != expected_subject
            ):
                return None

            context = self._map_claims_to_context(claims)
            # The ALB key issuer proves which load balancer signed the token.
            # Canonical membership remains keyed by the upstream OIDC issuer
            # configured on that trusted ALB client.
            context.issuer = identity_issuer
            context.subject = subject
            return context

        except Exception:
            logger.debug("ALB JWT validation failed", exc_info=True)
            return None

    async def validate_oidc_jwt(self, token: str) -> RequestContext | None:
        """Validate standard OIDC Bearer JWT using JWKS discovery."""
        try:
            claims = await self._validate_direct_oidc_claims(token)
            if claims is None:
                return None

            subject = claims.get("sub")
            if not isinstance(subject, str) or not subject.strip():
                return None

            return self._map_claims_to_context(claims)

        except Exception:
            logger.debug("OIDC JWT validation failed")
            return None

    async def validate_id_token(
        self,
        token: str,
        *,
        expected_nonce: str | None = None,
    ) -> RequestContext | None:
        """Validate an authorization-code ID token and its one-time nonce."""
        try:
            if expected_nonce is not None and (
                not isinstance(expected_nonce, str)
                or not expected_nonce
                or len(expected_nonce.encode("utf-8")) > 512
            ):
                return None
            claims = await self._validate_direct_oidc_claims(token)
            if claims is None:
                return None

            # This path is for the Cognito browser client. Cognito marks ID and
            # access tokens explicitly, so absence is not accepted either.
            if claims.get("token_use") != "id":
                return None
            if expected_nonce is not None:
                nonce = claims.get("nonce")
                if (
                    not isinstance(nonce, str)
                    or not hmac.compare_digest(nonce, expected_nonce)
                ):
                    return None
            return self._map_claims_to_context(claims)
        except Exception:
            logger.debug("OIDC ID token validation failed")
            return None

    async def _validate_direct_oidc_claims(
        self,
        token: str,
    ) -> dict | None:
        """Return verified direct-OIDC claims shared by bearer and ID tokens."""
        issuer = self._validated_oidc_issuer()
        audience = self._validated_oidc_audience()
        if (
            issuer is None
            or audience is None
            or not self._valid_direct_oidc_token(token)
        ):
            return None

        header = self._decode_jwt_header(token)
        if header is None:
            return None

        kid = header.get("kid")
        alg = header.get("alg")
        if not self._valid_direct_oidc_kid(kid):
            return None
        if alg not in DIRECT_OIDC_ALGORITHMS:
            return None

        jwks = await self._get_jwks()
        if jwks is None:
            return None

        observed_generation = self._jwks_generation
        key = self._find_key(jwks, kid)
        if key is None:
            jwks = await self._refresh_jwks_for_unknown_kid(
                kid,
                issuer=issuer,
                observed_generation=observed_generation,
            )
            if jwks is None:
                return None
            key = self._find_key(jwks, kid)
            if key is None:
                return None
        if not self._jwk_supports_algorithm(key, alg):
            return None

        claims = self._verify_and_decode(
            token,
            key,
            algorithms=[alg],
            audience=audience,
            issuer=issuer,
            required_claims=("iss", "aud", "exp", "sub"),
        )
        if claims is None:
            return None
        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject.encode("utf-8")) > 2048
        ):
            return None
        return claims

    def _decode_jwt_header(self, token: str) -> dict | None:
        """Decode JWT header without verification (to get kid/alg)."""
        try:
            segments = token.split(".")
            if len(segments) != 3 or any(not segment for segment in segments):
                return None
            header_segment = segments[0]
            if len(header_segment) > MAX_DIRECT_OIDC_HEADER_BYTES:
                return None
            padding = 4 - len(header_segment) % 4
            if padding != 4:
                header_segment += "=" * padding
            header_bytes = base64.b64decode(
                header_segment,
                altchars=b"-_",
                validate=True,
            )
            if len(header_bytes) > MAX_DIRECT_OIDC_HEADER_BYTES:
                return None
            header = json.loads(
                header_bytes,
                object_pairs_hook=self._reject_duplicate_json_keys,
                parse_constant=self._reject_non_finite_json,
            )
            return header if isinstance(header, dict) else None
        except Exception:
            return None

    @staticmethod
    def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JWT header member")
            result[key] = value
        return result

    @staticmethod
    def _reject_non_finite_json(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    def _verify_and_decode(
        self,
        token: str,
        key: Any,
        algorithms: list[str],
        *,
        audience: str | tuple[str, ...] | None = None,
        issuer: str | None = None,
        required_claims: tuple[str, ...] = (),
    ) -> dict | None:
        """Verify JWT signature and decode claims.

        Requires python-jose for signature verification. Refuses to decode
        without verification to prevent authentication bypass.
        """
        try:
            from jose import jwt as jose_jwt
        except ImportError:
            logger.error(
                "python-jose is not installed — JWT signature verification unavailable. "
                "Install it with: uv sync --extra oidc"
            )
            return None

        try:
            multiple_audiences = (
                audience if isinstance(audience, tuple) else None
            )
            options = {
                "verify_aud": (
                    audience is not None
                    and multiple_audiences is None
                ),
                "verify_iss": issuer is not None,
                "verify_exp": True,
            }
            options.update(
                {
                    f"require_{claim}": True
                    for claim in required_claims
                    if not (
                        multiple_audiences is not None
                        and claim == "aud"
                    )
                }
            )
            claims = jose_jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=(
                    audience
                    if isinstance(audience, str)
                    else None
                ),
                issuer=issuer,
                options=options,
            )
            if multiple_audiences is not None:
                token_audiences = claims.get("aud")
                if isinstance(token_audiences, str):
                    token_audiences = [token_audiences]
                if (
                    not isinstance(token_audiences, list)
                    or any(
                        not isinstance(value, str)
                        for value in token_audiences
                    )
                    or set(token_audiences).isdisjoint(
                        multiple_audiences
                    )
                ):
                    return None
            return claims
        except Exception:
            return None

    def _alb_trust_config(self) -> tuple[str, str, str, str] | None:
        """Return validated ALB trust roots and the fixed AWS key endpoint."""
        signer = self._config.alb_signer_arn.strip()
        client_id = self._config.alb_client_id.strip()
        issuer = self._config.alb_issuer.strip()
        region = self._config.alb_region.strip()
        if not signer or not client_id or not issuer or not region:
            return None

        arn_parts = signer.split(":", 5)
        if len(arn_parts) != 6:
            return None
        arn, partition, service, signer_region, account_id, resource = arn_parts
        dns_suffix = _AWS_DNS_SUFFIXES.get(partition)
        if (
            arn != "arn"
            or service != "elasticloadbalancing"
            or signer_region != region
            or not account_id.isdigit()
            or len(account_id) != 12
            or not resource.startswith("loadbalancer/app/")
            or dns_suffix is None
            or re.fullmatch(r"[a-z0-9-]{3,32}", region) is None
        ):
            return None

        expected_issuer = f"https://public-keys.auth.elb.{region}.{dns_suffix}"
        if issuer != expected_issuer:
            return None

        if partition == "aws-us-gov":
            key_base_url = f"https://s3-{region}.amazonaws.com/aws-elb-public-keys-prod-{region}"
        else:
            key_base_url = expected_issuer
        return signer, client_id, issuer, key_base_url

    async def _get_alb_public_key(
        self,
        kid: str,
        key_base_url: str,
    ) -> str | None:
        """Get one ALB key from a bounded, expiring, fail-closed cache."""
        now = time.monotonic()
        ttl = self._alb_key_cache_ttl()

        for cached_kid, (_, fetched_at) in list(self._alb_key_cache.items()):
            age = now - fetched_at
            if ttl <= 0 or age < 0 or age >= ttl:
                self._alb_key_cache.pop(cached_kid, None)

        cached = self._alb_key_cache.get(kid)
        if cached is not None:
            self._alb_key_cache.move_to_end(kid)
            return cached[0]

        public_key = await self._fetch_alb_public_key(kid, key_base_url)
        if public_key is None:
            return None

        if ttl > 0:
            self._alb_key_cache[kid] = (public_key, time.monotonic())
            self._alb_key_cache.move_to_end(kid)
            while len(self._alb_key_cache) > MAX_ALB_KEY_CACHE_ENTRIES:
                self._alb_key_cache.popitem(last=False)
        return public_key

    async def _fetch_alb_public_key(
        self,
        kid: str,
        key_base_url: str,
    ) -> str | None:
        """Fetch and validate a bounded P-256 ALB public key response."""
        if _ALB_KEY_ID_PATTERN.fullmatch(kid) is None:
            return None
        url = f"{key_base_url}/{kid}"
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=5.0, connect=2.0)
            async with aiohttp.ClientSession(
                timeout=timeout,
                trust_env=False,
                auto_decompress=False,
            ) as client:
                async with client.get(
                    url,
                    headers={"Accept-Encoding": "identity"},
                    allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        return None

                    content_length = resp.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > MAX_ALB_KEY_BYTES:
                                return None
                        except ValueError:
                            return None

                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(4096):
                        body.extend(chunk)
                        if len(body) > MAX_ALB_KEY_BYTES:
                            return None

            return self._validate_alb_public_key(bytes(body))
        except ImportError:
            logger.warning("aiohttp not installed — ALB key fetch unavailable")
        except Exception:
            logger.debug("Failed to fetch ALB public key", exc_info=True)
        return None

    @staticmethod
    def _validate_alb_public_key(public_key_pem: bytes) -> str | None:
        """Require the AWS response to be exactly a usable P-256 public key."""
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec

            public_key = serialization.load_pem_public_key(public_key_pem)
            if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(public_key.curve, ec.SECP256R1):
                return None
            normalized = public_key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            return normalized.decode("ascii")
        except (ImportError, TypeError, ValueError):
            return None

    def _alb_key_cache_ttl(self) -> float:
        ttl = self._config.alb_key_cache_ttl
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            return 0
        return max(0, min(float(ttl), MAX_ALB_KEY_CACHE_TTL_SECONDS))

    async def _get_jwks(self) -> dict | None:
        """Return a fresh bounded JWKS cache, failing closed on refresh errors."""
        issuer = self._validated_oidc_issuer()
        if issuer is None:
            self._clear_jwks_cache()
            return None

        cached = self._fresh_jwks(issuer)
        if cached is not None:
            return cached

        async with self._jwks_refresh_lock:
            cached = self._fresh_jwks(issuer)
            if cached is not None:
                return cached

            # Stale key material must never become an outage fallback.
            self._clear_jwks_cache()
            jwks = await self._fetch_valid_jwks()
            if jwks is None or self._validated_oidc_issuer() != issuer:
                return None
            self._install_jwks(jwks, issuer)
            return self._jwks_cache

    async def _refresh_jwks_for_unknown_kid(
        self,
        kid: str,
        *,
        issuer: str,
        observed_generation: int,
    ) -> dict | None:
        """Refresh once for key rotation without permitting unknown-kid floods."""
        async with self._jwks_refresh_lock:
            cached = self._fresh_jwks(issuer)
            if cached is None:
                return None
            if self._find_key(cached, kid) is not None:
                return cached

            # A concurrent request already refreshed this cache generation.
            if self._jwks_generation != observed_generation:
                return cached

            now = time.monotonic()
            since_last_refresh = now - self._jwks_last_unknown_refresh_at
            if since_last_refresh < 0 or since_last_refresh < UNKNOWN_KID_REFRESH_INTERVAL_SECONDS:
                return cached

            # Charge the cooldown before I/O so failed issuer requests are also
            # bounded. Existing fresh keys remain usable by other requests.
            self._jwks_last_unknown_refresh_at = now
            jwks = await self._fetch_valid_jwks()
            if jwks is None or self._validated_oidc_issuer() != issuer:
                return None
            self._install_jwks(jwks, issuer)
            return self._jwks_cache

    async def _fetch_valid_jwks(self) -> dict | None:
        try:
            jwks = await self._fetch_jwks()
        except Exception:
            logger.debug("JWKS fetch failed")
            return None
        return jwks if self._valid_jwks(jwks) else None

    async def _fetch_jwks(self) -> dict | None:
        """Fetch and parse the issuer's current JWKS without cache fallback."""
        try:
            import httpx

            issuer = self._validated_oidc_issuer()
            if issuer is None:
                return None
            discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            timeout = httpx.Timeout(
                OIDC_HTTP_TOTAL_TIMEOUT_SECONDS,
                connect=OIDC_HTTP_CONNECT_TIMEOUT_SECONDS,
            )
            limits = httpx.Limits(
                max_connections=2,
                max_keepalive_connections=0,
            )
            async with httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with asyncio.timeout(OIDC_HTTP_TOTAL_TIMEOUT_SECONDS):
                    discovery = await self._fetch_bounded_json(
                        client,
                        discovery_url,
                        max_bytes=MAX_OIDC_DISCOVERY_BYTES,
                        content_types=("application/json",),
                    )
                    if not isinstance(discovery, dict):
                        return None
                    if discovery.get("issuer") != issuer:
                        return None
                    jwks_uri = discovery.get("jwks_uri")
                    if not self._safe_jwks_uri(jwks_uri, issuer):
                        return None
                    jwks = await self._fetch_bounded_json(
                        client,
                        jwks_uri,
                        max_bytes=MAX_OIDC_JWKS_BYTES,
                        content_types=(
                            "application/json",
                            "application/jwk-set+json",
                        ),
                    )
                return jwks if self._valid_jwks(jwks) else None
        except ImportError:
            logger.warning("httpx not installed — JWKS fetch unavailable")
        except Exception:
            logger.debug("JWKS fetch failed")
        return None

    async def _fetch_bounded_json(
        self,
        client: Any,
        url: str,
        *,
        max_bytes: int,
        content_types: tuple[str, ...],
    ) -> Any:
        """Read one non-redirected, identity-encoded JSON response within bounds."""
        headers = {
            "Accept": ", ".join(content_types),
            "Accept-Encoding": "identity",
        }
        async with client.stream(
            "GET",
            url,
            headers=headers,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return None

            content_encoding = response.headers.get("content-encoding", "")
            if content_encoding.lower().strip() not in ("", "identity"):
                return None

            content_type = response.headers.get("content-type", "")
            media_type = content_type.partition(";")[0].lower().strip()
            if media_type not in content_types:
                return None

            content_length = response.headers.get("content-length")
            if content_length is not None:
                if not content_length.isascii() or not content_length.isdigit():
                    return None
                parsed_length = int(content_length)
                if parsed_length < 0 or parsed_length > max_bytes:
                    return None

            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    return None
            if not body:
                return None

        try:
            return json.loads(
                body.decode("utf-8"),
                object_pairs_hook=self._reject_duplicate_json_keys,
                parse_constant=self._reject_non_finite_json,
            )
        except (UnicodeDecodeError, ValueError):
            return None

    def _jwks_cache_ttl(self) -> float:
        """Clamp configured cache lifetime so signing keys cannot remain unbounded."""
        ttl = self._config.jwks_cache_ttl
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            return 0
        return max(0, min(float(ttl), MAX_JWKS_CACHE_TTL_SECONDS))

    def _fresh_jwks(self, issuer: str) -> dict | None:
        cache_age = time.monotonic() - self._jwks_fetched_at
        if (
            self._jwks_cache_issuer == issuer
            and self._jwks_cache_ttl() > 0
            and self._valid_jwks(self._jwks_cache)
            and 0 <= cache_age < self._jwks_cache_ttl()
        ):
            return self._jwks_cache
        return None

    def _install_jwks(self, jwks: dict, issuer: str) -> None:
        self._jwks_cache = jwks
        self._jwks_cache_issuer = issuer
        self._jwks_fetched_at = time.monotonic()
        self._jwks_generation += 1

    def _clear_jwks_cache(self) -> None:
        if self._jwks_cache or self._jwks_cache_issuer is not None:
            self._jwks_generation += 1
        self._jwks_cache = {}
        self._jwks_cache_issuer = None
        self._jwks_fetched_at = 0

    @classmethod
    def _valid_jwks(cls, jwks: Any) -> bool:
        if not isinstance(jwks, dict):
            return False
        keys = jwks.get("keys")
        if (
            not isinstance(keys, list)
            or not keys
            or len(keys) > MAX_OIDC_JWKS_KEYS
            or any(not isinstance(key, dict) for key in keys)
        ):
            return False

        key_ids = [key.get("kid") for key in keys]
        return all(cls._valid_direct_oidc_kid(kid) for kid in key_ids) and len(set(key_ids)) == len(key_ids)

    def _validated_oidc_issuer(self) -> str | None:
        issuer = self._config.issuer
        return issuer if self._parse_safe_https_url(issuer) is not None else None

    def _validated_oidc_audience(
        self,
    ) -> str | tuple[str, ...] | None:
        audience = self._config.audience
        try:
            encoded_audience = audience.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return None
        if (
            not isinstance(audience, str)
            or not audience
            or audience != audience.strip()
            or len(encoded_audience) > 2048
            or any(not character.isprintable() or character.isspace() for character in audience)
        ):
            return None
        audiences = tuple(audience.split(","))
        if (
            len(audiences) > 8
            or any(not value for value in audiences)
            or len(set(audiences)) != len(audiences)
        ):
            return None
        return audiences[0] if len(audiences) == 1 else audiences

    @staticmethod
    def _valid_direct_oidc_token(token: Any) -> bool:
        if not isinstance(token, str) or not token:
            return False
        try:
            encoded = token.encode("ascii")
        except UnicodeEncodeError:
            return False
        return len(encoded) <= MAX_DIRECT_OIDC_JWT_BYTES

    @staticmethod
    def _valid_direct_oidc_kid(kid: Any) -> bool:
        if (
            not isinstance(kid, str)
            or not kid
            or kid != kid.strip()
            or any(not character.isprintable() or character.isspace() for character in kid)
        ):
            return False
        try:
            return len(kid.encode("utf-8")) <= MAX_DIRECT_OIDC_KID_BYTES
        except UnicodeEncodeError:
            return False

    @classmethod
    def _safe_jwks_uri(cls, jwks_uri: Any, issuer: str) -> bool:
        jwks_parts = cls._parse_safe_https_url(jwks_uri)
        issuer_parts = cls._parse_safe_https_url(issuer)
        if jwks_parts is None or issuer_parts is None:
            return False
        return cls._url_origin(jwks_parts) == cls._url_origin(issuer_parts)

    @staticmethod
    def _url_origin(parts: SplitResult) -> tuple[str, int]:
        return parts.hostname.lower(), parts.port or 443

    @staticmethod
    def _parse_safe_https_url(value: Any) -> SplitResult | None:
        try:
            encoded_value = value.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return None
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(encoded_value) > MAX_OIDC_URL_BYTES
            or "\\" in value
            or _INVALID_PERCENT_ESCAPE_PATTERN.search(value)
            or any(not character.isprintable() or character.isspace() for character in value)
        ):
            return None

        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            port = parts.port
        except (UnicodeError, ValueError):
            return None
        if (
            parts.scheme != "https"
            or not parts.netloc
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or hostname.endswith(".")
            or hostname.lower() == "localhost"
            or hostname.lower().endswith(".localhost")
            or port == 0
        ):
            return None

        try:
            ipaddress.ip_address(hostname)
            return None
        except ValueError:
            pass

        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        labels = ascii_hostname.split(".")
        if (
            len(ascii_hostname) > 253
            or any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
            or all(_NUMERIC_HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
        ):
            return None

        decoded_path = unquote(parts.path)
        if (
            "\\" in decoded_path
            or any(not character.isprintable() or character.isspace() for character in decoded_path)
            or any(segment in (".", "..") for segment in decoded_path.split("/"))
        ):
            return None
        return parts

    def _find_key(self, jwks: dict, kid: str | None) -> dict | None:
        """Return the one JWK whose kid exactly matches; reject ambiguity."""
        if not isinstance(kid, str) or not kid:
            return None
        keys = jwks.get("keys", [])
        if not isinstance(keys, list):
            return None
        matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == kid]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _jwk_supports_algorithm(key: dict, algorithm: str) -> bool:
        """Ensure the selected JWK is suitable for the pinned token algorithm."""
        if key.get("kty") != _DIRECT_OIDC_KEY_TYPES.get(algorithm):
            return False
        if key.get("alg") not in (None, algorithm):
            return False
        if key.get("use") not in (None, "sig"):
            return False
        key_ops = key.get("key_ops")
        if key_ops is not None:
            if (
                not isinstance(key_ops, list)
                or any(not isinstance(operation, str) for operation in key_ops)
                or "verify" not in key_ops
            ):
                return False
        return True

    def _map_claims_to_context(self, claims: dict) -> RequestContext:
        """Map well-typed JWT claims to RequestContext."""
        mappings = self._config.claim_mappings

        def _string_claim(claim_key: str, default: str = "") -> str:
            value = claims.get(claim_key, default)
            if not isinstance(value, str):
                raise ValueError(f"OIDC claim {claim_key!r} must be a string")
            return value

        roles_claim = mappings.get("roles", "custom:roles")
        roles_raw = claims.get(roles_claim, [])
        if isinstance(roles_raw, str):
            roles = [role.strip() for role in roles_raw.split(",") if role.strip()]
        elif isinstance(roles_raw, list):
            if any(not isinstance(role, str) or not role.strip() for role in roles_raw):
                raise ValueError(f"OIDC claim {roles_claim!r} must contain non-empty strings")
            roles = [role.strip() for role in roles_raw]
        else:
            raise ValueError(f"OIDC claim {roles_claim!r} must be a string or string list")

        scope_raw = claims.get("scope", "")
        if not isinstance(scope_raw, str):
            raise ValueError("OIDC claim 'scope' must be a string")
        scopes = scope_raw.split()

        project_id = _string_claim(mappings.get("project_id", "custom:project_id"))
        tenant_id = _string_claim(mappings.get("tenant_id", "custom:tenant_id"))
        business_unit = _string_claim(mappings.get("business_unit", "custom:business_unit"))
        email = _string_claim(mappings.get("email", "email"))
        issuer = _string_claim("iss") if "iss" in claims else None
        subject = _string_claim("sub") if "sub" in claims else None

        return RequestContext(
            user_id=_string_claim(mappings.get("user_id", "sub")),
            project_id=project_id,
            roles=roles,
            scopes=scopes,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_id=tenant_id or None,
            business_unit=business_unit or None,
            email=email or None,
            issuer=issuer,
            subject=subject,
        )
