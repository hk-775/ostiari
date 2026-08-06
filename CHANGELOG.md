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
- **A gateway starting without AxonLLM now says so.** Routing governance and token cost
  tracking live in AxonLLM, and the direct-provider fallback is good enough that a
  gateway without it serves traffic and reports healthy — which is how it once ran
  unnoticed. With `llm_gateway` enabled, startup logs a warning naming exactly what
  stopped applying and how to fix it. It warns rather than refusing because AxonLLM is
  a separate private repo, not on PyPI: a hard requirement would make it a deployment
  dependency of every gateway, CI runner, and contributor checkout, including the ones
  that only ever proxy tools. `OSTIARI_REQUIRE_AXON=1` refuses to start instead, and is
  the right setting in production.
- `GET /health` reports `llm_router` (`embedded`, `root`, `governed`, `cost_tracking`,
  `tools`, and `reason` when down) — "the gateway is up" and "LLM calls are governed"
  are different facts, and nothing else in the payload distinguished them.
- `ModuleRegistry.get()` — lets the server inspect an activated module's state without
  reaching through private dicts.
- 154 tests: `tests/unit/test_detect.py` (103), `tests/unit/test_http_limits.py` (16),
  IPv6/MRN/openapi edge cases, plus gateway and control-plane regression tests.
- Gateway tests for the startup warning (it must name both governance and cost
  tracking, plus its own off switch), `OSTIARI_REQUIRE_AXON=1` refusing to boot, the
  `/health` router report, and tool specs reaching AxonLLM on all three endpoints.
- `gateway/tests/test_hitl.py` gained 8 tests pinning the production/HITL interaction:
  a fail-closed intervene queues rather than 403s, the approve and deny loops complete,
  the explanation survives the raise, and the three non-bypass properties (genuine
  block, HITL off, shadow mode) each stay blocked.
- **Demo seeds for the three pages that rendered empty on a fresh start.** The demo's
  value is that everything in it is real, which is undercut by a page with nothing on
  it. All three follow the existing `demo_seed` conventions — idempotent, suppressed by
  `OSTIARI_NO_DEMO=1`, and skipped when real data already exists — and all three are
  constrained to agree with the rest of the demo rather than merely look plausible:
  - **Approvals (HITL)** — four pending calls plus a four-row decision history. The
    pending calls *are* the four intervene-tier calls already seeded into traces, so the
    queue and the trace view tell the same story.
  - **Quotas** — one gateway-scoped quota per demo gateway. `current_spend` is summed
    from the `UsageRecord` rows the metering seed already writes, not invented, so a
    budget bar cross-checked against the Costs page shows the same number.
  - **Broker pools** — seeded pools with the MCP card wired, plus the `block-destructive`
    policy created rather than only patched. It previously depended on a row added by
    hand in an earlier session: recreate the database and `crm-agent` came up with an
    empty block list, so the destructive scenarios silently *executed* while the Policies
    page showed nothing for that gateway.
- **The demo login is prefilled** with the seeded admin, gated on `DEMO_LOGIN` (on under
  `vite dev`; `VITE_DEMO_LOGIN=true` opts a deployed demo in). Deliberately not
  unconditional: `_seed_admin` refuses `admin/admin` when `OSTIARI_ENV=production` and
  requires `OSTIARI_ADMIN_PASSWORD`, so a hardcoded prefill would display a password that
  cannot work there — and would ship a well-known credential in the bundle of a repo
  heading for public. Verified absent from the production build: Vite folds the constant
  and drops both the address and the hint.
- **Budget alerts now land somewhere.** `on_budget_alert` had exactly one caller in the
  repo — its own definition — so crossing 80% produced a log line an operator had to be
  tailing to see. The gateway now subscribes at startup and reports each crossing to
  `POST /api/quotas/alerts`; read them from `GET /api/quotas/alerts` (newest first, 200
  per org) and acknowledge with `DELETE`. Alerts are held in memory rather than given a
  table: an alert is a notification, and the spend behind it is already durable in
  `usage_records`. `record_spend` is synchronous, so the report is dispatched as a task
  on the running loop — with no loop (a sync script) it logs and the spend still books
  rather than failing the booking. Org comes from the reporting gateway's row, never the
  payload, like every other unauthenticated ingest path.
- **`POST /api/experiments/{name}/push`**, and an automatic push on create, toggle, and
  delete. The gateway had implemented the traffic split all along; the control plane's
  experiment store was never read by any push path, so creating an experiment in the UI
  split no traffic. Every mutating response now carries `pushed` and `push_error` instead
  of throwing, so the UI can say "saved, not live" for a gateway that was down rather
  than implying the split is running; the explicit push route returns 502.
- **`POST` / `GET /config/ab-experiments`** on the gateway — a *partial* config endpoint,
  because `POST /config/llm` is a whole-document replace and pushing experiments through
  it would wipe the provider credentials loaded at startup. The push sends the complete
  experiment set rather than a delta, which is what makes a delete take effect.
- **A/B experiments survive a gateway restart.** `_build_config` adds an
  `ab_experiments` key when the gateway has any, and `_apply_bundle` applies it. A
  restart used to silently end a running experiment.
- **Audit coverage beyond gateways and policies** — tools (including one entry per
  OpenAPI import, not one per generated tool), MCP servers (including `discover`, which
  changes the callable tool surface even though no row changes), quotas, models (recording
  only the changed fields as `{from, to}`, since the Models page PUTs the whole record),
  agents (re-registering an existing name is an `update`, because a tool-list change is a
  privilege change), and experiments. Entries are written inside the same transaction as
  the change, so the hash chain stays verifiable and a failed commit leaves no entry.
  Delete routes capture their details *before* the row is gone.
- `actor_of()` in `audit_service` — the `X-Actor` lookup was duplicated privately in the
  two routers that already audited, which is part of how the others ended up with none.

### Changed
- **The quota push now carries a `pricing` table** built from the model registry
  (`model_config.py::pricing_table`). The gateway's enforcer had read a `pricing` key all
  along and nothing sent it, so editing a model's `input_cost_per_1k` changed what the
  control plane *reported* but not what the gateway used to project a budget. The
  registry already stores per-1k costs — the same unit the enforcer wants — so nothing is
  converted. Models priced `0.0` on both sides are omitted rather than sent as free: a
  missing entry falls back to `DEFAULT_PRICING`, while `0.0` would assert a real model is
  free and disable the budget for it.
- **The Sandbox Code tab issues the tool calls in its editor.** It previously ignored the
  textarea and fired one hardcoded `db_query`, so editing the code changed nothing about
  what ran. `runCode` now extracts each `/tool/<name>` call and its JSON body and issues
  them in order, streaming responses. Deliberately still not an interpreter — loops and
  conditionals are not evaluated, and arbitrary execution would mean a remote-eval
  endpoint on the control plane. A refusal is reported as a result, not a failure: `403`
  is `✗ BLOCKED`, `429` is `✗ QUOTA`.
- **CI runs on every pull request, not only those targeting `main`.** The
  `pull_request` trigger was filtered to `branches: [main]`, so a PR based on another
  feature branch got an empty status rollup — which reads as "nothing to report" rather
  than "nothing ran." Two stacked PRs merged untested that way, and the break (a
  control-plane test importing `ostiari_gateway`, which that job didn't install) only
  surfaced when the parent PR retriggered against `main`. `push` stays limited to `main`
  so merge commits don't run twice.
- All three container images run as **non-root** (gateway and control plane `10001`,
  frontend `101` — nginx's own uid), with every manifest re-asserting it. Both layers
  matter: a Dockerfile `USER` is only a default a manifest can override, while
  `runAsNonRoot` makes the kubelet *refuse* a container that would run as uid 0, so an
  image rebuilt without `USER` fails loudly instead of quietly regaining root. Plus
  `allowPrivilegeEscalation: false`, all capabilities dropped, and seccomp
  `RuntimeDefault`. For a product whose job is governing what agents may do, a
  compromised gateway holding uid 0 could rewrite the policy engine deciding whether
  calls are allowed.
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

### Documentation
- `docs/axon-router.md` — what running without AxonLLM actually costs (startup warning,
  `/health` `llm_router`, `OSTIARI_REQUIRE_AXON` for production), tool calls route
  *through* AxonLLM with a per-provider dialect table, and why the router silently
  never loaded (import ordering).
- `docs/embedded-routing.md` — replaced "graceful degradation if AxonLLM is absent"
  with what is actually lost when it is; documented ensemble as wired on `/invoke` (it
  was described as "not yet wired") and added the tool-call pointer.
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
- **A restarted gateway came back enforcing, whatever mode the control plane held.**
  `_build_config` sent `mode` and `SidecarConfig` had the field, but `_apply_bundle`
  never read it — so a gateway deliberately left in shadow started blocking traffic
  meant only to be observed, while the Gateways page kept rendering `Shadow`. Mode is
  now applied **first**, before tools or policy, so such a gateway doesn't spend even one
  request enforcing. An unrecognized value is ignored rather than defaulted: a typo must
  not flip an observing gateway into enforcement.
- **Budget alerts never fired under Redis.** `_check_alert_thresholds` compared the
  process-local `_total_spend`, which stays `0.0` when a shared store is booking the
  spend — so alerting was silently absent from exactly the fleet deployments where it
  matters. It now reads the store when one is attached.
- **A pushed price could lose to a built-in one.** `_get_pricing` checked the pushed table
  for an exact match, then fell into a fuzzy substring pass over `DEFAULT_PRICING` only —
  so a pushed `gpt-4o-mini` could be beaten by a fuzzy hit on built-in `gpt-4o`, 16x more
  expensive and in the wrong direction for a budget. Both tables are now checked for an
  exact match before either fuzzy pass.
- **A queued config push was dropped by the reconnect meant to deliver it.**
  `gateway_register` popped `config_queue` into `bundle["queued_updates"]`, a key nothing
  on the gateway read — and the pop emptied the queue, so no later heartbeat recovered
  it. It is now the sibling `config_updates` the heartbeat path already applies, and
  `lifecycle.register()` drains it. What was lost is exactly what
  `POST /api/gateways/{id}/push-config` (the Policies and Quotas page buttons) sends.
- **The demo fleet shipped an ungoverned destructive tool.** `register_fleet_tools.py`
  set `"policy": None` for `devops-agent` on a comment claiming a `devops-strict` policy
  already blocked `github.delete_repo` — nothing in the repo ever created one, so a fresh
  demo left that tool callable. A governance product's own demo now governs it: a
  `devops-guard` policy blocks `*delete*`, `*.drop`, `*.destroy`, `db_delete` and allows
  the three read/write tools. The test asserts the *invariant* rather than this one
  gateway — no gateway may register a tool whose name contains a destructive verb without
  a policy that blocks it — so the next tool added to the fleet can't reintroduce the gap.
  `analytics-agent`'s `None` is pinned as deliberate: it registers nothing destructive.
- **`make clean-start` now actually starts clean.** Two independent causes. The recipe
  deleted `control-plane/backend/data/state.json`, the path from before `env.data_dir()`,
  while the lifespan restored the live `control-plane/data/state.json` — so quotas,
  experiments, and providers came back on a "clean" start. It now removes both, so an
  older checkout doesn't leave a file a future refactor might read. Separately,
  `seed_models()` ran at *import* time in `routers/model_config.py`, the one seeder
  outside the `OSTIARI_NO_DEMO` block in `app.py`, so the 18 model configs appeared
  regardless. It's now gated on the same flag. The argument for an always-on catalog — a
  routing table is not demo content — isn't wrong, but it lost to a simpler one: a page
  showing 18 models you didn't add contradicts the promise of the command. Register what
  you use via `POST /api/models`, or call `seed_models()` to get the built-in set back.
- **Every Bedrock Mantle Claude call failed on a parameter no client sent.** `temperature`
  was typed `float` with a `0.7` default from both shims down through `providers`, plus
  `LLMConfig.temperature = 0.7` and an explicit `temperature: 0.7` in the shipped demo
  config — so "the caller sent nothing" and "the caller asked for 0.7" were the same
  value by the time a request was built, and the key went on the wire every time.
  Mantle's current Claude models *reject* the parameter rather than ignoring it
  (`400 "temperature is deprecated for this model."`). It is now `float | None`,
  defaulting to None and omitted from the request when None; providers that still accept
  it apply their own default, which is what a caller who said nothing wants. The
  `opt_float` helper in `translate.py` preserves *absence* through inbound parsing, since
  a non-numeric value degrading to the provider default is better than the 500 that
  `float()` raised.
- **Control-plane state was lost on every restart**, as a consequence of the non-root
  change above. `database._DB_DIR` and `persistence.STATE_FILE` resolved independently:
  the deployment redirected only the database onto the mounted volume and left
  `state.json` in `/app/data`, a root-owned directory the now-unprivileged process could
  not write. `save_state` raised `PermissionError` during shutdown, so every restart
  discarded the persisted quotas, experiments, models, and provider config. Both now
  resolve to one writable dir via `env.data_dir()`.
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

### Known issues
Found while auditing the docs against the code. One remains from that audit; the
rest are in **Fixed** above. It is documented where an operator would hit it.

- **The broker pilot is not tenant-scoped.** `TokenPool` and
  `ReconciliationRecord` have no `org_id`, and `routers/broker_pilot.py` takes no
  `get_current_org`; `routers/proxy.py` likewise forwards to any gateway by id
  without an org check. Harmless in the single-org deployment that is the only
  supported one today. Deliberately deferred: closing it means adding `org_id` to
  both models plus a migration, and it only bites a deployment that doesn't exist
  yet. Don't turn on multi-org without doing it first.

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

> Entries above describe 0.1.0 as released and are left as written. Two have
> since changed: the CI matrix was collapsed to a single Python 3.11 on
> `ubuntu-latest` in exchange for running all three suites on every push, and the
> coverage gate is `fail_under = 70`. Nothing has been published to PyPI — the
> workflow exists but fires only on a published GitHub release.
