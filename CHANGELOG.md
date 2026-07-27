# Changelog

All notable changes to Ostiari will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Native detection engine (`ostiari.detect`)** — PII redaction and prompt-injection
  detection with no external dependencies. 12 PII types (Luhn-checked cards,
  parser-validated IPv6, credentials), 7 injection categories, Unicode/zero-width
  obfuscation handling, reversible or irreversible redaction, and a `flag` mode for
  observe-before-enforce. See [docs/detection-engine.md](docs/detection-engine.md).
- `InjectionResult.risk_points` maps the 0.0–1.0 score onto Ostiari's 0–100 risk scale,
  so detection can compose with Guard tiers rather than only hard-blocking.
- xAI (Grok) and Together as first-class providers, with a shared OpenAI-compatible
  connectivity probe and four seeded models.
- `gateway/register_demo_providers.py` — seeds the in-memory provider store from the
  env file the gateways already load.
- **AxonLLM is now a required runtime dependency.** With `llm_gateway` enabled, the
  gateway refuses to start unless AxonLLM embeds successfully, naming the failure and
  the fix. Routing governance and token cost tracking live in AxonLLM, and the
  direct-provider fallback is good enough that a gateway without it serves traffic and
  reports healthy — so the absence has to be fatal, not inferred. `OSTIARI_ALLOW_NO_AXON=1`
  downgrades it to a warning for running the non-LLM surface.
- `GET /health` reports `llm_router` (`embedded`, `root`, `governed`, `cost_tracking`,
  `tools`, and `reason` when down) — "the gateway is up" and "LLM calls are governed"
  are different facts, and nothing else in the payload distinguished them.
- `ModuleRegistry.get()` — lets the server inspect an activated module's state without
  reaching through private dicts.
- 154 tests: `tests/unit/test_detect.py` (103), `tests/unit/test_http_limits.py` (16),
  IPv6/MRN/openapi edge cases, plus gateway and control-plane regression tests.
- Gateway tests for the AxonLLM requirement, the `/health` router report, and tool
  specs reaching AxonLLM on all three endpoints.
- `gateway/tests/test_hitl.py` gained 8 tests pinning the production/HITL interaction:
  a fail-closed intervene queues rather than 403s, the approve and deny loops complete,
  the explanation survives the raise, and the three non-bypass properties (genuine
  block, HITL off, shadow mode) each stay blocked.

### Documentation
- `docs/axon-router.md` — AxonLLM is required (startup refusal, `/health` `llm_router`,
  `OSTIARI_ALLOW_NO_AXON`), tool calls route *through* AxonLLM with a per-provider
  dialect table, and why the router silently never loaded (import ordering).
- `docs/embedded-routing.md` — replaced "graceful degradation if AxonLLM is absent"
  with the actual requirement; documented ensemble as wired on `/invoke` (it was
  described as "not yet wired") and added the tool-call pointer.
- `docs/claude-code-shim.md` — the shim routes through AxonLLM; its own
  cross-provider translation is now the *degraded* mid-flight path, not the primary
  one, and the buffered-streaming tradeoff is stated.
- `docs/codex-shim.md` — tool calls translate through AxonLLM rather than passing
  through; 503 vs 501 distinguished.
- `docs/gateway-architecture.md` — "Graceful Degradation" (which described AxonLLM
  as an optional paid module and credited it with PII/injection) rewritten as the
  requirement plus what one mid-flight failure degrades to; added tool translation;
  corrected the provider table's "direct fallback calls" framing.
- `docs/internal/ostiari-architecture-and-features.md` §6 — the requirement, tool
  forwarding, and the import-ordering bug that motivated both.
- `docs/control-plane-guide.md` — brought current with the shipped UI and backend.
  Three routed pages were undocumented: **Approvals** (the guide described the
  *intervene* tier at length but never said where a human acts on it, nor that
  `OSTIARI_HITL` is off by default and the 202 → `X-Approval-Id` resubmit is the
  caller's job), **Discovery**, and **Token Efficiency** (routed but absent from
  the nav, so unreachable except by URL). Also: the gate-chain diagram gained the
  approval gate it was missing; the nav order is documented as it actually ships
  (Observe first) separately from the setup lifecycle the guide follows; the
  `operator` vs UI-"Editor" role split is spelled out, along with the fact that
  viewer is a frontend affordance — only providers, users, and audit check the role
  server-side; production now *refuses to boot* without `OSTIARI_ADMIN_PASSWORD`
  rather than merely warranting a "change it immediately"; `OSTIARI_INGEST_KEY` is
  required in production; and the detection engine is documented as
  gateway-config-only with no control-plane surface. §7.4 now documents what HITL-off
  means *per environment* — advisory in dev, a refusal in production — since a
  threshold tuned where the intervene band is effectively "log it" becomes a wall of
  403s the day `OSTIARI_ENV=production` is set.

### Fixed
- **Production silently deleted the *intervene* tier.** Production is fail-closed, so a
  Guard with no way to resolve an intervene in-process collapsed it to a block and
  *raised* — and the sidecar's exception handler returned 403 from a point upstream of
  the approval gate, which was therefore unreachable. Every scored-intervene call was
  refused and the Approvals queue stayed empty regardless of `OSTIARI_HITL`: the
  three-tier model degraded to two, in the one environment where the middle tier
  matters most. `ActionBlockedError` now carries `original_tier` and `signals`, so
  "this is forbidden" and "a human needs to look at this, and none was reachable"
  stay distinguishable across the raise, and the gateway defers the second to the
  approvals queue. It is an escalation path, not a bypass — a genuine block still
  403s and creates no approval, HITL off still blocks (nobody to defer to), and
  shadow mode still never queues. `/validate` reports `original_tier` alongside
  `tier` for the same reason.
- **PII redaction and injection detection never worked outside a dev machine.** Both
  imported from AxonLLM, an *optional* install — and because an enabled-but-unavailable
  control fails closed, turning either one on blocked **every** request, benign ones
  included. They now come from `ostiari.detect`, a hard dependency.
- **Trace ingest trusted the payload's `org_id`**, letting any caller that could reach
  `/api/traces/ingest` file a trace into another tenant's buffer — read back by
  `/recent`, the WebSocket fan-out, compliance, ROI, trust scoring, and discovery. The
  org is now derived from the reporting gateway's row and the payload value overwritten.
- **The approvals queue was not tenant-scoped**, exposing one tenant's raw tool
  parameters (SQL, recipients, payloads) in every other tenant's review queue, and
  letting anyone decide them.
- Usage records were never stamped with an org, so every tenant's spend accumulated
  under `default` while their own ledger read empty. Cost summaries, usage lists,
  token-broker economics, experiment results, and discovery are now org-scoped.
- Discovery read the outer org-keyed dict as if it held agent names, so every properly
  registered agent was reported as shadow AI; onboarding wrote to the same wrong level,
  corrupting the registry.
- **AxonLLM was never actually loading.** `AxonRouter._ensure()` imported `src.gateway`
  to locate AxonLLM's checkout, then called `_axon_root()`, which imported
  `src.gateway` again — but AxonLLM's editable install puts `<root>/src` on `sys.path`,
  so `src.gateway` is not importable until the root is added. The chicken-and-egg meant
  `available` was always False: **every** LLM call took the direct-provider fallback,
  with no AxonLLM cost tracking or routing governance, and nothing looked wrong. The
  root is now found with `importlib.util.find_spec` (no import) and added to `sys.path`
  first. Predates this release; introduced with "Unify routing on AxonLLM".
- **AxonLLM silently dropped tool specs** (`ChatCompletionRequest` had no `tools`
  field), so tool-carrying calls got a fluent "I have no database access" that looked
  like success. Fixed at the source in AxonLLM — it now carries `tools`/`tool_choice`
  and translates them into each provider's dialect (OpenAI, Anthropic, Bedrock
  Converse, Gemini, Cohere) in both directions — so tool-using traffic routes through
  AxonLLM like everything else instead of around it. `supports_tools()` survives as a
  version guard for an older AxonLLM checkout: `/invoke` and `/v1/messages` warn and
  degrade, `/v1/chat/completions` returns 501.
- AxonLLM returns `{"error": …}` instead of raising; parsing it optimistically produced
  an empty HTTP 200. It now raises so the caller falls back.
- `/invoke` passed a configured default model straight to AxonLLM, 404ing whenever that
  default was a dated ID absent from AxonLLM's registry.
- `/invoke` returned 500 with a stack trace for malformed JSON or a schema violation;
  now 400 and 422 respectively.
- Tool schemas were omitted from the agentic loop's tool list, so the model could never
  supply arguments.
- `cache_hit` was always reported `false`: one variable served as both the reported
  fact and loop control, and the loop reset it after round 0.
- The trace viewer's Gateway column was blank for every live trace — the reporter sent
  `sidecar_id` where consumers read `gateway_id`. Cost reporting had the mirror-image
  bug and 422'd the whole batch.
- Alembic resolved its SQLite path one directory short of the app's, so migrations ran
  against a different database than the one served.
- `openapi_import._load` returned unchecked `json.loads` output, so a spec that was
  valid JSON but not an object escaped as a list/str/int and failed later somewhere
  unrelated; `schema: true` (valid JSON Schema) crashed a whole import.
- IPv6 detection missed the `::`-compressed form — the way most real addresses are
  written — while matching clock times like `12:30:45`. Medical-record numbers written
  `MRN-1234567` were missed.
- `net_guard` annotated IPs with `ipaddress._BaseAddress`, a private base class that
  doesn't declare `is_private`/`is_loopback`/`is_link_local`, so the SSRF guard
  type-errored on every check it exists to perform.

### Changed
- **The deploy manifests now ship `OSTIARI_ENV=production` with `OSTIARI_HITL=on`.**
  Helm (`gateway.env` / `gateway.hitl` values), both Kubernetes manifests, and the ECS
  task definition set them together, because in a fail-closed deployment they are not
  independent knobs: production refuses an intervene nobody can resolve, so HITL off is
  what makes the middle tier disappear. Shipping production without it hands operators
  a wall of 403s and an empty Approvals page. Both stay values/ConfigMap keys, so either
  can be turned off deliberately rather than by omission. `docker-compose.yml` passes
  both through from the environment (`OSTIARI_HITL:-off`, `OSTIARI_ENV` unset) — it is
  the local stack, where fail-open is the intended demo posture.
- HITL is not free, and `deploy/README.md` now says so beside the variables: a mid-band
  call answers **202** and does not execute, so someone has to staff the queue and
  callers have to re-submit with `X-Approval-Id`. Tune thresholds until the intervene
  band is rare (validate in `shadow` mode) before turning it on. That README also gained
  the ten production-relevant env vars it omitted entirely — the four the control plane
  now *requires* in production, and the two gateway controls
  (`OSTIARI_CONFIG_ADMIN_KEY`, `OSTIARI_GATEWAY_AUTH`) that stay fail-open unless set.
- `mypy --strict src/` is clean (was 20 errors); `ruff check src/ gateway/` is clean
  (was 49). Two ruff suggestions are deliberately declined with rationale in-comment:
  negating the fail-open default would make the codebase's most safety-critical branch
  a double negative, and suppressing the budget-alert callback error silently is
  exactly the failure nobody notices (it logs a warning instead).
- `http_limits` ASGI middlewares are fully typed, with local ASGI aliases rather than a
  dependency on `starlette.types` — the module is shared by both apps and shouldn't
  pull in a web framework.

## [0.1.0] - 2026-06-22

### Added
- Policy engine with YAML-based rules (allow, block, score-based evaluation)
- Risk scoring (0-100) with configurable thresholds (allow / intervene / block)
- Anomaly detection: loop detection, drift detection, hallucination checks, contradiction detection
- Circuit breaker with configurable failure threshold, recovery timeout, and half-open state
- Multi-framework adapters: OpenAI, Anthropic Claude, AWS Bedrock, Strands Agents
- Intervention gateway for human-in-the-loop approval of medium-risk actions
- Checkpoint/rollback engine for agent state management
- Full trace storage with SQLite backend and redaction filter
- Real-time WebSocket streaming for live monitoring
- Web dashboard (FastAPI) with trace viewer, policy editor, and agent metrics
- Terminal UI (Textual) for local development monitoring
- CLI with validate, traces, report, tui, and dashboard commands
- Policy hot-reload from file, HTTPS, or S3 sources
- `@protect` decorator for inline function guarding
- Health checker with dependency status reporting
- Report generator for trace analytics
- PyPI publish workflow via trusted publishing
- CI matrix: Python 3.10-3.13, Ubuntu/macOS/Windows
- 585 tests (unit, property-based, integration) with 90%+ coverage target
