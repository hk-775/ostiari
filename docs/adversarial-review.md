# Adversarial Security Review

A structured attack pass over the gateway's enforcement surface: the tool-call
gate chain, the OIDC/JWT identity layer, quota/budget enforcement, and the x402
payment path. Every candidate finding was **empirically verified** (a live
exploit or a focused test), which mattered — it ruled out a plausible-looking
finding that turned out not to be real.

Regression tests for everything below live in `gateway/tests/test_adversarial.py`
(15 tests; `cd gateway && PYTHONPATH=. pytest tests/test_adversarial.py`).

## Method

Three parallel code audits mapped (1) the `POST /tool/{action}` gate chain +
`agent_auth`, (2) the OIDC/JWT layer across gateway and control plane, and
(3) quota/payment/body-size enforcement. Findings were then reproduced before
being accepted, and fixed under the standard branch → PR → CI flow.

## Confirmed vulnerability — fixed

### Negative-amount payment credit (config-independent logic bug)

A malicious or compromised downstream tool returns `HTTP 402 {"amount_usdc": -100}`.
The gateway's `parse_402` accepted the negative, and `Wallet.can_afford` had no
sign check, so `Wallet.debit(-100)` **credited** the wallet $100 and drove
`spent_today_usdc` negative — refilling a drained wallet and resetting its daily
cap. Proven live:

```
BEFORE: balance=$1.00 spent_today=$0.40
AFTER : balance=$101.00 spent_today=$-99.60   ← credited, cap reset
```

**Fix:** reject `amount < 0` in `Wallet.can_afford` (defense in depth for every
settle path) and clamp negatives to 0 in `parse_402` (reject at the boundary).

## Ruled out by live testing

### Wallet double-spend under concurrency — NOT a bug

The static pass flagged `can_afford`→`debit` as a non-atomic TOCTOU. In fact
they run back-to-back with no `await` between them, so under asyncio's
single-threaded model they're effectively atomic. Five concurrent charges
against a one-charge wallet settled exactly once; the balance never went
negative. No fix needed. (Regression-guarded so it stays that way.)

## Hardened

### Budget TOCTOU on the single-shot LLM paths — fixed

On the chat/messages shims, `quota.check()` and `quota.record_spend()` straddle
the awaited upstream model call, so concurrent completions could all read the
same stale spend total and overshoot a hard `budget_limit_usd`.

**Fix:** `QuotaEnforcer.check(reserve=True)` now books the cost estimate as an
in-flight reservation (atomically, in the same await-free block that passes the
projection). The budget projection counts `spend + live reservations`, so
concurrent calls see each other. `record_spend(reservation_id=…)` releases the
reservation and books the real cost; `release_reservation` handles error paths;
and reservations self-expire on a TTL as a leak backstop. The `/invoke`
executor is intentionally left as-is — it books real per-round spend
incrementally, so its concurrency window is one round, not the whole call.

### Production fail-open detection — added

Every gateway control is off by default for the demo flow (config-admin key
unset → `/config/*` open; gateway auth off → `X-Agent-Id` trusted with no
token). `create_app` now runs a production posture check: under
`OSTIARI_ENV=production` it **warns** on each fail-open control, and under
`OSTIARI_STRICT=1` it **refuses to start** — mirroring the control plane's
existing production JWT-secret guard. Non-production (the demo default) is
unaffected.

### OIDC audience warning — added

When gateway auth is enabled but `OSTIARI_OIDC_AUDIENCE` is unset, any token
from the trusted issuer is accepted regardless of its `aud` — a real risk in a
shared-IdP / multi-app pool. `get_validator` now logs a warning so operators
pin the audience. (Behavior unchanged to avoid breaking existing single-app
deployments.)

## Attacked and held (no change needed)

- **JWT crypto:** algorithm pinned to RS256 (control-plane OIDC), gateway OIDC,
  and HS256 for local tokens — the accepted algorithm is never read from the
  token header, so `alg=none` and RS256→HS256 key confusion are rejected.
  Signatures are actually verified; `exp` and `iss` are enforced. A hand-forged
  `alg=none` token is rejected (test-verified).
- **Token ↔ identity binding:** when gateway auth is required, the token's
  asserted identity must equal `X-Agent-Id` (403 on mismatch) — no cross-agent
  replay.
- **Passwords:** bcrypt with per-hash salt and constant-time `checkpw`.
- **Missing wallet** fails closed (unfunded, not free).
- **Config admin key**, when set, is compared with `hmac.compare_digest`.

## The dominant theme: fail-open by default

The security-critical controls — gateway agent-auth, cross-agent delegation,
`/config/*` admin, control-plane API auth, OIDC audience — are all off by
default and gated behind operators setting `OSTIARI_ENV=production`,
`OSTIARI_REQUIRE_AUTH`, `OSTIARI_GATEWAY_AUTH=required`, `OSTIARI_JWT_SECRET`,
`OSTIARI_OIDC_AUDIENCE`, `OSTIARI_CONFIG_ADMIN_KEY`, and `OSTIARI_ADMIN_PASSWORD`.
This is the documented demo posture, and the [`STARTUP.md`](../STARTUP.md)
production checklist enumerates the flags. The additions above make a
misconfigured production deployment *loud* (warnings) or *safe* (strict-mode
refusal) instead of silently open.

## Residual / informational (not fixed)

- **Per-process quota/budget state** — each gateway replica has its own
  counters by default, so a horizontally-scaled fleet gets K× the configured
  limit. Since this review, `gateway/ostiari_gateway/shared_store.py` closes it:
  set `OSTIARI_REDIS_URL` (or `REDIS_ENDPOINT`) and rate-limit windows, budget
  spend, and wallet balances move to Redis, each mutation a single atomic Lua
  script so replicas can't race. It stays **opt-in and fail-safe** — no Redis
  configured or reachable means the in-process path, so a Redis outage degrades
  to per-process limits rather than taking the gateway down. A fleet running
  without it still gets K× limits.
- **Body-size append-then-check** — the middleware appends a chunk before
  testing the cap, but uvicorn bounds individual chunks (~64KB), so peak memory
  is `cap + one small chunk`. Informational.
- **Login timing oracle** — `verify_password` is skipped for unknown emails,
  giving a small enumeration side-channel. Low severity.
