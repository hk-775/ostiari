# Ostiari Authentication (OIDC / Cognito)

How users, services, and agents authenticate to Ostiari, and how to set it up on
AWS Cognito (or any OIDC provider). Ostiari validates tokens; it does **not**
issue them — identity is delegated to the IdP.

## Model in one line

**Identity in the token (JWT, validated locally via JWKS); authorization in the
Ostiari engine (RBAC + policy).** The token says *who you are*; Ostiari decides
*what that principal may do* — a JWT can't express "this agent may call
`send_email` but not `db_delete`, under a $10 cap, unless the risk score clears
70". Validation lives in `control-plane/backend/control_plane/auth/oidc.py` (and
its gateway twin, `gateway/ostiari_gateway/oidc.py`).

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
export OSTIARI_REQUIRE_AUTH=true                  # default: off — see below
```

`OSTIARI_AUTH_MODE=oidc` only decides *how* a token is validated. It does not
make tokens mandatory: without `OSTIARI_REQUIRE_AUTH`, `AuthMiddleware` passes
every request straight through and an unauthenticated caller still reads and
rewrites the whole API. Production needs **both**. When it's on, every `/api/*`
route is fail-closed except a short allowlist — `/api/health`,
`/api/auth/login`, `/api/auth/register`, `/api/auth/sso/*`, `/api/traces/ingest`
(machine ingest, guarded by its own `X-Ingest-Key`), and the OpenAPI docs.

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

Precedence in `_role_from_claims`: explicit `ostiari_role` / `custom:role` /
`role` → `cognito:groups` or `groups` → OAuth `scope` → default `viewer` (least
privilege). Group names are matched case-insensitively **by substring**, and
admin beats operator beats viewer — so `ostiari-admin`, `Admin`, and
`eng-admins` all map to `admin`. Scopes map by keyword: anything containing
`admin` → admin, `write` or `operator` → operator.

## Multi-tenancy

`tenant_from_claims` reads the tenant from `tenant_id`, `custom:tenant_id`,
`org_id`, or `custom:org`, defaulting to `"default"` — so a single-issuer,
single-tenant deployment needs no extra claims. Both the control plane and the
gateway have this function; `get_current_org` in `auth/dependencies.py` is the
route-layer seam, and it also falls back to `"default"` for a tokenless request
(the demo posture — `AuthMiddleware` has already 401'd those when
`OSTIARI_REQUIRE_AUTH` is on).

The database is scoped: the tables behind gateways, tools, policies, MCP servers,
usage, A2A agents, wallets, payments, audit logs, token pools, and reconciliations
each carry an `org_id`, and the routers scope their queries through
`control_plane/models/scoping.py`. Routers whose state is in-memory (quotas,
agents, models, providers, ROI, agent-routing, discovery) key their dicts by org
instead, which is equivalent.

`token_pools` is the one table where `org_id` is part of the **primary key** rather
than an indexed column beside it. Pool identity is `(org, provider)`: two tenants
must each be able to hold an `anthropic` pool, and with `provider` alone as the key
the second to fund one would collide with the first — or worse, a lookup would hand
a tenant whichever row existed and draw *their* traffic down against it. Fetch one
with `broker_pilot._get_pool(db, org, provider)`; `get_scoped` is the wrong tool
here, since it fetches by pk and only *then* compares `org_id`.

One route is deliberately unscoped: `GET /api/token-broker/pilot/collector` reports
which billing backend the process runs, which is deployment config rather than
tenant data.

Gateways post usage, payments, and approvals with no user token, so their org is
derived from their own `gateways` row (`org_of_gateway`) rather than trusted from
the payload — a tenant can't write into another's ledger.

## Off-AWS portability

Nothing here is Cognito-specific. Point `OSTIARI_OIDC_ISSUER` at any OIDC
provider (Keycloak, Auth0, Okta, self-hosted) and the same validation works —
only the issuer/JWKS URL changes.

## Files

- `get_service_token.py` — runnable example: a service reads its `client_secret`
  from Secrets Manager, calls Cognito's token endpoint (client_credentials), and
  calls Ostiari with the resulting JWT. Copy-paste starting point for client teams.
