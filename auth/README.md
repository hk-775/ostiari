# Ostiari Authentication (OIDC / Cognito)

How users, services, and agents authenticate to Ostiari, and how to set it up on
AWS Cognito (or any OIDC provider). Ostiari validates tokens; it does **not**
issue them — identity is delegated to the IdP.

## Model in one line

**Identity in the token (JWT, validated locally via JWKS); authorization in the
Ostiari engine (RBAC + policy).** See `docs/internal/security-faq-jwt-vs-ostiari.md`
for the PDP/PEP rationale.

## Three principals, one validation path

| Principal | How it gets a token | Grant |
|---|---|---|
| **User** (human) | Interactive login at the IdP (redirect + code exchange), incl. SSO | OIDC Authorization Code + PKCE |
| **Service** (no human) | Direct call to the IdP token endpoint with client_id + secret | OAuth2 **Client Credentials** |
| **Agent** | Same as service (M2M), or a delegated token when acting for a user | Client Credentials / token exchange |

All three produce an **RS256 JWT** that Ostiari validates identically via JWKS.
Only the claims differ (user has `email`/`cognito:groups`; service has
`scope`/`client_id`).

## How Ostiari validates (no secrets stored)

```
   App startup / on unknown key ──▶ GET <issuer>/.well-known/jwks.json   (Cognito, public keys)
                                     │  cached in memory
   per request ──▶ verify RS256 signature + iss + aud + exp   (LOCAL, no IdP call)
                   → extract claims → identity + role + tenant_id
```

Ostiari only ever fetches Cognito's **public** keys. There is **no secret to
store on the Ostiari side.** The only real secret is each service's
`client_secret`, which lives in **AWS Secrets Manager on the client**, not here
(see `get_service_token.py`).

## Enable it (env — all off by default)

Control plane:
```bash
export OSTIARI_AUTH_MODE=oidc                     # default: local (unchanged demo behavior)
export OSTIARI_OIDC_ISSUER=https://cognito-idp.<region>.amazonaws.com/<pool-id>
export OSTIARI_OIDC_JWKS_URL=$OSTIARI_OIDC_ISSUER/.well-known/jwks.json   # optional; derived by default
export OSTIARI_OIDC_AUDIENCE=<app-client-id>      # optional aud/client_id check
```

Gateway (agent/service tool calls):
```bash
export OSTIARI_GATEWAY_AUTH=required              # default: off (X-Agent-Id header trusted)
export OSTIARI_OIDC_ISSUER=...                    # same issuer
```
When required, a tool call must carry a valid Bearer token whose asserted agent
identity (`agent_id`/`client_id`/`sub`) **matches** the `X-Agent-Id` header, or
it's rejected (401 no/invalid token, 403 identity mismatch).

**Off by default on purpose:** `make demo-full`, the seeded admin login, and the
register scripts all keep working with zero config. OIDC is opt-in.

## Cognito setup (one user pool, many app clients)

1. **Create one user pool** (`ostiari`). This is the single issuer.
2. **User app client** — auth code + PKCE; enable Hosted UI and/or SSO
   federation to the company IdP (Okta / Azure AD via SAML/OIDC). No secret if
   it's a public SPA client; secret if the backend does the code exchange.
3. **One M2M app client per service/agent** — enable the **client_credentials**
   grant, define **resource-server scopes** (e.g. `ostiari/invoke`,
   `ostiari/read`). Each gets its own `client_id` + `client_secret`.
4. **Role mapping** — put users in Cognito **groups** (`ostiari-admin`,
   `ostiari-operator`, `ostiari-viewer`) or set a custom `role` claim. Ostiari
   maps groups/scopes → admin | operator | viewer (least-privilege default).
5. **Optional custom claims** — `custom:agent_id` for agents; `custom:tenant_id`
   for the (future) multi-tenant seam.

## Role mapping (claims → Ostiari role)

Precedence: explicit `role`/`custom:role`/`ostiari_role` → `cognito:groups` /
`groups` → OAuth `scope` → default `viewer`. Admin beats operator beats viewer.

## Multi-tenancy

Single-tenant today (every token → `tenant_id="default"`), multi-tenant-ready:
the tenant is read from a claim and the issuer is a resolvable function, so going
SaaS later is additive. See the internal security FAQ.

## Off-AWS portability

Nothing here is Cognito-specific. Point `OSTIARI_OIDC_ISSUER` at any OIDC
provider (Keycloak, Auth0, Okta, self-hosted) and the same validation works —
only the issuer/JWKS URL changes.

## Files

- `get_service_token.py` — runnable example: a service reads its `client_secret`
  from Secrets Manager, calls Cognito's token endpoint (client_credentials), and
  calls Ostiari with the resulting JWT. Copy-paste starting point for client teams.
