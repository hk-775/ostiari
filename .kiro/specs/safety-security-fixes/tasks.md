# Implementation Plan

## Overview

This plan fixes eleven verified safety/security defects in Ostiari using the two-phase-per-defect
methodology from the design's Testing Strategy: for each defect, (1) write an exploratory test that
reproduces the bug on the UNFIXED code, then (2) implement the fix, then (3) run fix-checking and
preservation tests. Tests use pytest + Hypothesis (Hypothesis where a value domain exists). Every fix
ships typed (strict mypy) and must keep the suite at the 90% coverage threshold.

Defects are grouped by severity — Critical (1–4), High (5–8), Medium (9–11) — and each defect's
exploration + fix + verification is a discrete, independently implementable unit. Two files are shared
and MUST be edited in the sequenced order below to avoid conflicts:

- `src/ostiari/guard.py` — touched by Defect 5 then Defect 7 (7 reuses 5's generate-once trace id).
- `src/ostiari/dashboard/app.py` — touched by Defect 2 → Defect 3 → Defect 6 (3's host check uses
  the `OSTIARI_HOST` var that 6 formalizes; keep the edits in this order).

Test-task titles use the `**Property N: Type**` format (matching the design's Correctness Properties
1–21) so PBT hover status works. Odd properties are Bug Condition (fix-checking); even properties are
Preservation.

## Tasks

### Phase A — Critical defects (safety/security): Defects 1–4

#### Defect 1 — Policy merge drops block rules (`src/ostiari/policy/engine.py`)

- [ ] 1. Explore: reproduce Defect 1 with a failing property test
  - **Property 1: Bug Condition** - Policy merge unions block and allow rules
  - **IMPORTANT**: Write this Hypothesis property test BEFORE implementing the fix; DO NOT fix code when it fails
  - **GOAL**: Surface a counterexample proving `PolicyEngine.merge` drops earlier `block`/`allow` rules
  - Hypothesis strategy: generate ordered lists of `PolicySet` sources where ≥2 sources define `block` rules (and a variant for `allow`)
  - Assert `blockRules(merge(...)) == UNION(all block rules)` and same for allow (from Bug Condition `isBugCondition(X)` in design)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (only the last override survives, e.g. File A `*.delete_database` + File B `*.drop_table` → only `*.drop_table`)
  - Place in `tests/property/test_policy_merge.py`; document the counterexample
  - _Requirements: 2.1_

- [ ] 2. Preserve: capture baseline non-buggy merge behavior for Defect 1
  - **Property 2: Preservation** - Non-allow/block merge semantics unchanged
  - **IMPORTANT**: Follow observation-first methodology on UNFIXED code
  - Observe/record pre-fix oracle for `risk_adjust`/`threshold_override`/`context_rule` override results, rule ordering (priority desc, then `type_order`), and merged thresholds (`global_thresholds` + `per_tool`)
  - Hypothesis strategy: generate policy pairs where the bug condition does NOT hold (no overlapping block/allow across sources)
  - Assert fixed `merge` equals the captured oracle
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES (baseline behavior to preserve)
  - _Requirements: 3.1_

- [ ] 3. Fix Defect 1 — union block/allow rules in `PolicyEngine.merge`
  - [ ] 3.1 Implement union semantics in `merge`
    - Replace `merged_block = override_block if override_block else base_block` (and the `allow` twin) with `base + [r for r in override if not duplicate]`
    - Deduplicate by rule equality (pydantic `Rule` equality), preserving first occurrence, so identical cross-file rules do not multiply
    - Leave `merged_other` override semantics, `type_order` sort, and threshold merge exactly as today
    - Confirm `_load_from_paths`, `register_decorator_rules`, `reload_from_content` inherit union behavior (all route through `merge`)
    - _Bug_Condition: isBugCondition(X) = ∃ i<j with block(X[i]) ∧ block(X[j]) (or allow)_
    - _Expected_Behavior: blockRules(merge) = UNION of all sources' block rules; same for allow_
    - _Preservation: risk_adjust/threshold_override/context_rule override, ordering, threshold merge unchanged_
    - _Requirements: 2.1, 3.1_
  - [ ] 3.2 Verify bug-condition test now passes
    - **Property 1: Expected Behavior** - Policy merge unions block and allow rules
    - Re-run the SAME property test from task 1 (do NOT write a new one) — **EXPECTED**: PASSES
    - _Requirements: 2.1_
  - [ ] 3.3 Verify preservation test still passes
    - **Property 2: Preservation** - Non-allow/block merge semantics unchanged
    - Re-run the SAME test from task 2 — **EXPECTED**: PASSES (no regression)
    - Add unit tests: 3-file mixed block/allow/other merge; duplicate-block dedup; empty-override no-op (`tests/unit/test_policy_merge.py`)
    - _Requirements: 3.1_

#### Defect 2 — WebSocket bypasses authentication (`src/ostiari/dashboard/app.py`, `middleware.py`)

Shared-file order: this is the FIRST edit to `dashboard/app.py`; Defects 3 then 6 follow.

- [ ] 4. Explore: reproduce Defect 2 with a failing test
  - **Property 3: Bug Condition** - WebSocket auth enforced
  - **IMPORTANT**: Write BEFORE the fix; structural/targeted test (input domain is not value-generated)
  - **GOAL**: Prove `/ws/live` streams traces without token checking because `BaseHTTPMiddleware` never runs for websocket scope
  - Use the dashboard test client with a token configured; open `/ws/live` with no token and with a wrong token
  - Assert the socket is closed before any trace message is received (from `isBugCondition`: `tokenConfigured() ∧ scope=="websocket"`)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (stream served without auth)
  - Place in `tests/integration/test_dashboard_ws_auth.py`; document counterexample
  - _Requirements: 2.2_

- [ ] 5. Preserve: capture baseline valid-token / HTTP auth behavior (shared with Defect 3)
  - **Property 4: Preservation** - HTTP auth and valid-token access unchanged
  - Observe on UNFIXED code: valid bearer-token HTTP request authorized; exempt paths `/api/health`, `/static`, `/favicon.ico` unaffected; valid `?token=` websocket connects
  - Write tests asserting these hold unchanged after the fix (`tests/integration/test_dashboard_auth_preserve.py`)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.2, 3.3_

- [ ] 6. Fix Defect 2 — enforce token auth inside `/ws/live`
  - [ ] 6.1 Implement in-endpoint websocket auth
    - In `websocket_endpoint`, read `websocket.query_params.get("token", "")`; compare with configured token via `hmac.compare_digest`
    - On mismatch: `await websocket.close(code=1008)` and return BEFORE `ws_manager.connect` (no trace sent)
    - Capture the configured token in a closure available to the endpoint; extract a shared token-compare helper to avoid divergence with `TokenAuthMiddleware`
    - Remove (or comment) the now-dead websocket branch in `TokenAuthMiddleware.dispatch`; keep HTTP route auth intact
    - _Bug_Condition: isBugCondition(X) = tokenConfigured() ∧ scope=="websocket"_
    - _Expected_Behavior: stream only if supplied token matches; else close before any trace_
    - _Preservation: HTTP bearer auth and valid-token websocket access unchanged_
    - _Requirements: 2.2, 3.2, 3.3_
  - [ ] 6.2 Verify bug-condition test now passes
    - **Property 3: Expected Behavior** - WebSocket auth enforced
    - Re-run task 4's test — **EXPECTED**: PASSES (missing/invalid token rejected)
    - Add unit tests: reject missing token, reject invalid token, accept valid token, accept in no-token mode
    - _Requirements: 2.2_
  - [ ] 6.3 Verify preservation test still passes
    - **Property 4: Preservation** - HTTP auth and valid-token access unchanged
    - Re-run task 5's tests — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.2, 3.3_

#### Defect 3 — Dashboard open by default (`src/ostiari/dashboard/app.py`, `src/ostiari/cli.py`)

Shared-file order: SECOND edit to `dashboard/app.py` (after Defect 2). Task 3's host check reads
`OSTIARI_HOST`, which Defect 6 later formalizes with legacy fallback — keep this ordering.

- [ ] 7. Explore: reproduce Defect 3 with a failing test
  - **Property 5: Bug Condition** - Unauthenticated dashboard warns (and guards non-loopback bind)
  - **IMPORTANT**: Write BEFORE the fix; targeted test with log-capture fixture
  - **GOAL**: Prove `create_app()` with no token emits no unauthenticated warning, even bound to `0.0.0.0`
  - Call `create_app()` with no token under log capture; assert an unauthenticated-mode warning is logged; assert CLI refuses non-loopback bind without token
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (no warning; CLI starts open)
  - Place in `tests/integration/test_dashboard_startup_warning.py`; document counterexample
  - _Requirements: 2.3_

- [ ] 8. Preserve: token-configured startup emits no unauthenticated warning
  - **Property 4: Preservation** - HTTP auth and valid-token access unchanged (token-present branch)
  - Observe on UNFIXED code: `create_app(token="secret")` logs no unauthenticated warning and auth is enforced; loopback bind without token still starts
  - Write test asserting these hold after the fix (extend `tests/integration/test_dashboard_auth_preserve.py`)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.2, 3.3_

- [ ] 9. Fix Defect 3 — warn on unauthenticated startup; CLI guards non-loopback bind
  - [ ] 9.1 Implement warning + CLI refusal
    - In `create_app`, add an `else` to the `if token:` block: `logger.warning(...)` a clear unauthenticated-mode message
    - Read host from `OSTIARI_HOST`; if no token and host is non-loopback, escalate to a stronger warning (warn loudly, do not hard-refuse in the library)
    - In the `dashboard` CLI command (`cli.py`): refuse to start when host is non-loopback and no token is set, unless `--allow-unauthenticated` is passed; add that flag
    - _Bug_Condition: isBugCondition(X) = X.token IS EMPTY_
    - _Expected_Behavior: unauthenticated warning emitted; non-loopback-without-token warned/refused_
    - _Preservation: token-configured startup emits no warning; loopback-without-token still starts_
    - _Requirements: 2.3, 3.2, 3.3_
  - [ ] 9.2 Verify bug-condition test now passes
    - **Property 5: Expected Behavior** - Unauthenticated dashboard warns (and guards non-loopback bind)
    - Re-run task 7's test — **EXPECTED**: PASSES
    - Add unit tests: warning when no token; CLI refusal on non-loopback + no token; `--allow-unauthenticated` overrides refusal
    - _Requirements: 2.3_
  - [ ] 9.3 Verify preservation test still passes
    - **Property 4: Preservation** - token-present branch unchanged
    - Re-run task 8's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.2, 3.3_

#### Defect 4 — fail_open default flip / breaking change (`src/ostiari/models.py`, `src/ostiari/cli.py`)

- [ ] 10. Explore: reproduce Defect 4 with a failing test
  - **Property 6: Bug Condition** - Default is fail-closed
  - **IMPORTANT**: Write BEFORE the fix; targeted unit test
  - **GOAL**: Prove `OstiariConfig()` defaults `fail_open=True` and the scaffold writes `fail_open: true`
  - Assert `OstiariConfig().fail_open is False`; assert `ostiari init` scaffold content contains `fail_open: false`
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (default is `True`; scaffold writes `true`)
  - Place in `tests/unit/test_fail_open_default.py`; document counterexample
  - _Requirements: 2.4_

- [ ] 11. Preserve: explicit fail_open=True still fails open
  - **Property 7: Preservation** - Explicit fail_open=True still fails open
  - Observe on UNFIXED code: `OstiariConfig(fail_open=True)` with a stage that raises yields `allow`
  - Write test asserting the fail-open code path is unchanged when explicitly opted in (`tests/unit/test_fail_open_default.py`)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.4_

- [ ] 12. Fix Defect 4 — flip default to fail-closed
  - [ ] 12.1 Implement default flip
    - Change `OstiariConfig.fail_open` default `True` → `False` in `models.py`
    - Change the `init` scaffold in `cli.py` to write `fail_open: false`
    - Do NOT alter the fail-open code path itself — only the default and scaffold value
    - _Bug_Condition: isBugCondition(X) = failOpenNotExplicitlySet(X) ∧ pipelineErrorOccurred(X)_
    - _Expected_Behavior: outcome ∈ {block, intervene}; scaffold writes fail_open: false_
    - _Preservation: explicit fail_open=True still allows on stage error_
    - _Requirements: 2.4, 3.4_
  - [ ] 12.2 Verify bug-condition test now passes
    - **Property 6: Expected Behavior** - Default is fail-closed
    - Re-run task 10's test — **EXPECTED**: PASSES
    - _Requirements: 2.4_
  - [ ] 12.3 Verify preservation test still passes
    - **Property 7: Preservation** - Explicit fail_open=True still fails open
    - Re-run task 11's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.4_

### Phase B — High defects (correctness): Defects 5–8

#### Defect 5 — trace_id mismatch (`src/ostiari/guard.py`)

Shared-file order: FIRST edit to `guard.py`; Defect 7 follows and reuses the generate-once trace id.

- [ ] 13. Explore: reproduce Defect 5 with a failing property test
  - **Property 8: Bug Condition** - Returned trace_id resolves to the stored trace
  - **IMPORTANT**: Write BEFORE the fix; Hypothesis over benign actions/params
  - **GOAL**: Prove the returned `ValidationResult.trace_id` never matches the persisted `TraceEntry.trace_id`
  - Hypothesis strategy: generate benign actions/params that record a trace; `validate()`; assert `storage.get_trace(result.trace_id)` is not `None` and its `trace_id == result.trace_id`
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (`get_trace(A)` is `None`; stored id is a different `B`)
  - Place in `tests/property/test_trace_id.py`; document counterexample
  - _Requirements: 2.5_

- [ ] 14. Preserve: all TraceEntry fields round-trip unchanged
  - **Property 9: Preservation** - Trace fields and retrieval unchanged
  - Observe on UNFIXED code: all `TraceEntry` fields persist and retrieve via `get_trace`/`get_traces`
  - Hypothesis strategy: generate traces; assert every field round-trips identically after the fix (only the returned id is corrected)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.5_

- [ ] 15. Fix Defect 5 — generate the trace id once and thread it through
  - [ ] 15.1 Implement generate-once trace id
    - Refactor `_record_trace` to accept an optional `trace_id` (or return the id it used)
    - In `_execute_pipeline`, allocate `trace_id = str(uuid.uuid4())` before recording, pass it to `_record_trace`, and reuse it in `ValidationResult(trace_id=trace_id, ...)`
    - Apply the same generate-once call on the block-path `_record_trace` for consistency (no returned result there)
    - _Bug_Condition: isBugCondition(X) = traceRecorded(X)_
    - _Expected_Behavior: ValidationResult.trace_id == persisted TraceEntry.trace_id_
    - _Preservation: all TraceEntry fields stored/retrievable unchanged_
    - _Requirements: 2.5, 3.5_
  - [ ] 15.2 Verify bug-condition test now passes
    - **Property 8: Expected Behavior** - Returned trace_id resolves to the stored trace
    - Re-run task 13's test — **EXPECTED**: PASSES
    - Add unit test: `trace_id` equality on allow and (via recorded trace) block paths
    - _Requirements: 2.5_
  - [ ] 15.3 Verify preservation test still passes
    - **Property 9: Preservation** - Trace fields and retrieval unchanged
    - Re-run task 14's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.5_

#### Defect 6 — env var prefix `AGENTGUARD_` → `OSTIARI_` (`src/ostiari/config.py`, `src/ostiari/dashboard/app.py`)

Shared-file order: THIRD (final) edit to `dashboard/app.py` (after Defects 2 and 3).

- [ ] 16. Explore: reproduce Defect 6 with a failing property test
  - **Property 10: Bug Condition** - OSTIARI_* variables take effect
  - **IMPORTANT**: Write BEFORE the fix; Hypothesis over `OSTIARI_*` key/value maps
  - **GOAL**: Prove `OSTIARI_*` variables are silently ignored (legacy `AGENTGUARD_*` prefix still read)
  - Hypothesis strategy: generate maps of `OSTIARI_*` vars (e.g. `OSTIARI_FAIL_OPEN`, `OSTIARI_TOKEN`, `OSTIARI_THRESHOLDS__ALLOW_MAX`); `ConfigLoader.load()`; assert each is reflected in effective config
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (vars ignored; defaults retained)
  - Place in `tests/property/test_env_prefix.py`; document counterexample
  - _Requirements: 2.6_

- [ ] 17. Preserve: non-prefixed paths unchanged; legacy prefix documented
  - **Property 11: Preservation** - Non-prefixed config paths unchanged; legacy prefix documented
  - Observe on UNFIXED code: explicit-arg, YAML, overrides, and nested `__` env decoding; `_coerce` type coercion
  - Assert these produce identical config after the fix; assert legacy `AGENTGUARD_*`-only env still applies but with a deprecation warning
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES (baseline for non-prefixed paths)
  - _Requirements: 3.6_

- [ ] 18. Fix Defect 6 — dual-prefix with deprecation warning
  - [ ] 18.1 Implement `OSTIARI_` prefix with legacy fallback
    - `config.py`: set default `env_prefix="OSTIARI_"`; in `_parse_env`, also scan legacy `AGENTGUARD_` at lower precedence, emitting a deprecation warning per legacy key
    - Update `_resolve_yaml_path` and `_describe_sources` to check `OSTIARI_CONFIG` first, then legacy `AGENTGUARD_CONFIG` (with warning)
    - `dashboard/app.py`: read `OSTIARI_REDIS_URL`/`OSTIARI_TOKEN`/`OSTIARI_DB`/`OSTIARI_HOST` first, falling back to legacy names with a deprecation warning (align with the `OSTIARI_HOST` read added in Defect 3)
    - `OSTIARI_*` takes precedence when both are present; leave nested `__` decoding and `_coerce` unchanged
    - _Bug_Condition: isBugCondition(X) = ∃ v ∈ X starting with "OSTIARI_"_
    - _Expected_Behavior: each OSTIARI_* variable is read and applied_
    - _Preservation: explicit-arg/YAML/overrides/nested `__` unchanged; legacy prefix → documented deprecation_
    - _Requirements: 2.6, 3.6_
  - [ ] 18.2 Verify bug-condition test now passes
    - **Property 10: Expected Behavior** - OSTIARI_* variables take effect
    - Re-run task 16's test — **EXPECTED**: PASSES
    - Add unit tests: each `OSTIARI_*` var takes effect; legacy deprecation warning; precedence when both set
    - _Requirements: 2.6_
  - [ ] 18.3 Verify preservation test still passes
    - **Property 11: Preservation** - Non-prefixed config paths unchanged; legacy prefix documented
    - Re-run task 17's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.6_

#### Defect 7 — allow decision not honored (`src/ostiari/guard.py`, `_execute_pipeline`)

Shared-file order: SECOND edit to `guard.py` (after Defect 5); reuses Defect 5's generate-once trace id.

- [ ] 19. Explore: reproduce Defect 7 with a failing property test
  - **Property 12: Bug Condition** - allow is authoritative
  - **IMPORTANT**: Write BEFORE the fix; Hypothesis over actions/scores with `decision=="allow"`
  - **GOAL**: Prove an `allow` decision can be escalated to `intervene`/`block` by anomaly/risk signals
  - Configure an `allow` rule and an anomaly detector that adds a high score; `validate()`; assert final tier is `allow`
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (escalated to block)
  - Place in `tests/property/test_allow_authoritative.py`; document counterexample
  - _Requirements: 2.7_

- [ ] 20. Preserve: block and evaluate paths unchanged
  - **Property 13: Preservation** - block and evaluate paths unchanged
  - Observe on UNFIXED code: `block` short-circuits; `evaluate` runs anomaly + gateway scoring; capture pre-fix oracle (final tier + score)
  - Hypothesis strategy: generate actions/scores with `decision` in `{block, evaluate}`; assert outcome equals oracle
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.7, 3.8_

- [ ] 21. Fix Defect 7 — short-circuit authoritative allow
  - [ ] 21.1 Implement allow short-circuit
    - In `_execute_pipeline`, after the `block` short-circuit and before anomaly detection: if `policy_result and policy_result.decision == "allow"`, build a `tier="allow"` outcome (score 0), record the trace with `tier="allow"`, run post-hooks, and return a `ValidationResult(tier="allow", ...)` WITHOUT invoking the anomaly detector or gateway
    - Keep breaker probe reporting and `_record_breaker_metrics(is_block=False)` on the allow path to match existing non-block behavior
    - Reuse the Defect 5 generate-once trace id on this new path
    - Leave `block` and `evaluate` code paths untouched
    - _Bug_Condition: isBugCondition(X) = policyDecision(X) == "allow"_
    - _Expected_Behavior: finalTier == "allow"; never escalated by anomaly/risk_
    - _Preservation: block short-circuits; evaluate runs anomaly + gateway scoring_
    - _Requirements: 2.7, 3.7, 3.8_
  - [ ] 21.2 Verify bug-condition test now passes
    - **Property 12: Expected Behavior** - allow is authoritative
    - Re-run task 19's test — **EXPECTED**: PASSES
    - Add unit test: allow short-circuit skips anomaly/gateway (assert via spy/mock the detector is not called); block/evaluate still call them
    - _Requirements: 2.7_
  - [ ] 21.3 Verify preservation test still passes
    - **Property 13: Preservation** - block and evaluate paths unchanged
    - Re-run task 20's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.7, 3.8_

#### Defect 8 — LIKE escaping inert (`src/ostiari/storage/sqlite.py`, `get_traces`)

- [ ] 22. Explore: reproduce Defect 8 with a failing property test
  - **Property 14: Bug Condition** - literal %/_ matched literally
  - **IMPORTANT**: Write BEFORE the fix; Hypothesis over action strings containing literal `%`/`_`
  - **GOAL**: Prove the missing `ESCAPE '\'` clause makes `_glob_to_sql` escaping inert (wildcards still match)
  - Insert traces `read_file` and `readXfile`; query `action="read_file"`; assert only `read_file` matches. Also test literal `%` (e.g. `50%_done`)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (`_` acts as wildcard, matches `readXfile`)
  - Place in `tests/property/test_like_escaping.py`; document counterexample
  - _Requirements: 2.8_

- [ ] 23. Preserve: glob and plain patterns unchanged
  - **Property 15: Preservation** - glob and plain patterns unchanged
  - Observe on UNFIXED code: glob `*`/`?` translation and no-wildcard plain patterns; capture pre-fix oracle result sets
  - Hypothesis strategy: generate glob (`*`/`?`) and plain patterns; assert `get_traces` result sets match the oracle after the fix
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.9_

- [ ] 24. Fix Defect 8 — add ESCAPE clause to the LIKE query
  - [ ] 24.1 Implement ESCAPE clause
    - Change the action condition in `get_traces` from `"action LIKE ?"` to `"action LIKE ? ESCAPE '\\'"`
    - Keep `_glob_to_sql` unchanged (already emits `\%`/`\_` escapes and `%`/`?`→`_` wildcards); verify the backslash escape is correct for the sqlite3 driver
    - Leave all other `TraceFilters` conditions (time, risk, tier, correlation) unchanged
    - _Bug_Condition: isBugCondition(X) = X.action contains literal '%' or '_'_
    - _Expected_Behavior: only actions containing the literal char match_
    - _Preservation: glob `*`/`?` and plain no-wildcard patterns unchanged_
    - _Requirements: 2.8, 3.9_
  - [ ] 24.2 Verify bug-condition test now passes
    - **Property 14: Expected Behavior** - literal %/_ matched literally
    - Re-run task 22's test — **EXPECTED**: PASSES
    - Add unit tests: literal `%`/`_` matched literally; glob `*`/`?` still wildcard
    - _Requirements: 2.8_
  - [ ] 24.3 Verify preservation test still passes
    - **Property 15: Preservation** - glob and plain patterns unchanged
    - Re-run task 23's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.9_

### Phase C — Medium defects (reliability): Defects 9–11

#### Defect 9 — shared single-worker intervention pool (`src/ostiari/gateway.py`)

- [ ] 25. Explore: reproduce Defect 9 with a failing test
  - **Property 16: Bug Condition** - one hung callback does not wedge all interventions
  - **IMPORTANT**: Write BEFORE the fix; targeted concurrency test (structural input domain)
  - **GOAL**: Prove a single module-level `ThreadPoolExecutor(max_workers=1)` lets one hanging callback wedge all interventions
  - Register a hanging callback and trigger an intervention; on another thread trigger a second intervention with a short timeout; assert the second returns/times out promptly
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (second intervention wedged behind the hang)
  - Place in `tests/integration/test_intervention_isolation.py`; document counterexample
  - _Requirements: 2.9_

- [ ] 26. Preserve: normal callback semantics unchanged
  - **Property 17: Preservation** - normal callback semantics unchanged
  - Observe on UNFIXED code: callbacks returning approve/deny within timeout yield the correct outcome; timeout raises `ActionInterventionTimeout`; sync and async callbacks supported
  - Write tests asserting these hold after the fix (`tests/unit/test_gateway_callbacks.py`)
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.10_

- [ ] 27. Fix Defect 9 — per-instance multi-worker pool
  - [ ] 27.1 Implement per-instance executor
    - Remove the module-level `_callback_pool`; give each `ActionGateway` its own lazily-created `ThreadPoolExecutor(max_workers=N, thread_name_prefix="ostiari-intervention")` with `N > 1` (default e.g. 4)
    - On timeout, `future.cancel()` and mark the callback timed out; document that a hung callback occupies one worker until it returns but no longer blocks others
    - Add a `close()`/shutdown path for the per-instance pool, invoked from `Guard.shutdown` via the gateway, to avoid thread leaks
    - Preserve sync/async invocation, `ActionInterventionTimeout` on timeout, and the `_fail_open` fallback on generic exceptions
    - _Bug_Condition: isBugCondition(X) = ∃ callback ∈ X that hangs_
    - _Expected_Behavior: other interventions progress/time out independently; sole worker never permanently consumed_
    - _Preservation: within-timeout approve/deny honored; timeout semantics; sync/async support unchanged_
    - _Requirements: 2.9, 3.10_
  - [ ] 27.2 Verify bug-condition test now passes
    - **Property 16: Expected Behavior** - one hung callback does not wedge all interventions
    - Re-run task 25's test — **EXPECTED**: PASSES
    - Add unit tests: per-instance pool isolation; two instances independent; pool shutdown on `Guard.shutdown`
    - _Requirements: 2.9_
  - [ ] 27.3 Verify preservation test still passes
    - **Property 17: Preservation** - normal callback semantics unchanged
    - Re-run task 26's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.10_

#### Defect 10 — silent trace drops (`src/ostiari/tracer.py`, `src/ostiari/storage/sqlite.py`)

- [ ] 28. Explore: reproduce Defect 10 with a failing test
  - **Property 18: Bug Condition** - trace drops are counted/surfaced
  - **IMPORTANT**: Write BEFORE the fix; targeted test (fill queue past capacity)
  - **GOAL**: Prove queue overflow and storage fail-open write drops happen with no counter/warning
  - Fill the tracer queue past `queue_max` without draining; assert `dropped_count > 0` and a warning was logged; add a case for a failing storage batch write
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (no `dropped_count`, no warning)
  - Place in `tests/unit/test_trace_drops.py`; document counterexample
  - _Requirements: 2.10_

- [ ] 29. Preserve: within-capacity tracing unchanged
  - **Property 19: Preservation** - within-capacity tracing unchanged
  - Observe on UNFIXED code: within-capacity sequences record and persist in order with non-blocking batching
  - Write test asserting order + non-blocking behavior preserved and `dropped_count` stays 0 after the fix
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.11_

- [ ] 30. Fix Defect 10 — account for and surface dropped traces
  - [ ] 30.1 Implement drop accounting
    - In `ExecutionTracer.record`, detect overflow before appending: when `len(self._queue) == self._queue_max`, increment `self._dropped_count` and emit a rate-limited warning, then append
    - Add a `dropped_count` property and expose it (stats/health)
    - In `_flush_batch`, when `save_traces_batch` fails, increment `self._dropped_count` by `len(batch)` and warn (covers the storage fail-open write-drop at the tracer boundary)
    - Keep the batching/flush loop and non-blocking `record` semantics otherwise unchanged
    - _Bug_Condition: isBugCondition(X) = queueOverflowOccurred(X) ∨ storageWriteDropped(X)_
    - _Expected_Behavior: dropped-trace counter increments and/or warning emitted_
    - _Preservation: within-capacity record/persist order + non-blocking batching unchanged_
    - _Requirements: 2.10, 3.11_
  - [ ] 30.2 Verify bug-condition test now passes
    - **Property 18: Expected Behavior** - trace drops are counted/surfaced
    - Re-run task 28's test — **EXPECTED**: PASSES
    - Add unit tests: overflow counter increment + warning; storage-batch-failure counter increment
    - _Requirements: 2.10_
  - [ ] 30.3 Verify preservation test still passes
    - **Property 19: Preservation** - within-capacity tracing unchanged
    - Re-run task 29's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.11_

#### Defect 11 — breaker recovery timer resets on restart (`src/ostiari/breaker.py`, `CircuitBreaker.restore_state`)

- [ ] 31. Explore: reproduce Defect 11 with a failing property test
  - **Property 20: Bug Condition** - original trip time preserved across restart
  - **IMPORTANT**: Write BEFORE the fix; Hypothesis over persisted trip ages + `recovery_after_seconds` under an injected clock
  - **GOAL**: Prove `restore_state` resets `tripped_at` to now, restarting the recovery countdown every restart
  - Persist an open breaker with `tripped_at` in the past beyond `recovery_after_seconds`; construct a new `CircuitBreaker`, `restore_state()`, `check()`; assert it becomes eligible for `half_open` immediately
  - Run on UNFIXED code — **EXPECTED OUTCOME**: FAILS (countdown reset; not eligible)
  - Place in `tests/property/test_breaker_restore.py`; document counterexample
  - _Requirements: 2.11_

- [ ] 32. Preserve: genuine recovery transition unchanged
  - **Property 21: Preservation** - genuine recovery transition unchanged
  - Observe on UNFIXED code: a breaker whose recovery genuinely elapses in the running process transitions open → half_open and probes; `trip`/`reopen`/`close`/`record`/`report_outcome` unchanged; closed/`tripped_at is None` cases unchanged
  - Hypothesis strategy with injected clock: assert elapsed computation matches for both monotonic and wallclock clocks
  - Run on UNFIXED code — **EXPECTED OUTCOME**: PASSES
  - _Requirements: 3.12_

- [ ] 33. Fix Defect 11 — reconcile clock domains in `restore_state`
  - [ ] 33.1 Implement clock-domain reconciliation
    - Compute `elapsed_since_trip = (now_wallclock - persisted.tripped_at).total_seconds()`, clamped `>= 0`
    - Set in-memory `breaker.tripped_at = self._clock() - elapsed_since_trip` so a later `check()` computing `self._clock() - breaker.tripped_at` yields true elapsed recovery time for both monotonic and injected clocks
    - Only apply when `state == "open"` and `tripped_at is not None`; leave `closed`/`half_open`/null cases as today; restore `counter` as today
    - _Bug_Condition: isBugCondition(X) = persistedState.state=="open" ∧ tripped_at IS NOT NULL_
    - _Expected_Behavior: recovery elapsed computed from original wallclock trip time, not reset to now_
    - _Preservation: genuine open→half_open recovery + trip/reopen/close/record/probe unchanged_
    - _Requirements: 2.11, 3.12_
  - [ ] 33.2 Verify bug-condition test now passes
    - **Property 20: Expected Behavior** - original trip time preserved across restart
    - Re-run task 31's test — **EXPECTED**: PASSES
    - Add unit tests: past-trip restore eligibility; clock-skew clamp to 0; closed/None cases unchanged
    - _Requirements: 2.11_
  - [ ] 33.3 Verify preservation test still passes
    - **Property 21: Preservation** - genuine recovery transition unchanged
    - Re-run task 32's test — **EXPECTED**: PASSES (no regression)
    - _Requirements: 3.12_

### Phase D — Documentation / breaking-change updates

- [ ] 34. Document Defect 4 breaking change (`fail_open` default flip)
  - Update `README.md` and any docs describing `fail_open`: call out that post-upgrade a pipeline error blocks/intervenes rather than allows
  - Document how to restore the old behavior (`fail_open: true`) and note the scaffold now writes `fail_open: false`
  - _Requirements: 2.4, 3.4_

- [ ] 35. Document Defect 6 env prefix migration + deprecation
  - Update `README.md`/docs to list the supported `OSTIARI_*` variables (`OSTIARI_CONFIG`, `OSTIARI_TOKEN`, `OSTIARI_REDIS_URL`, `OSTIARI_DB`, `OSTIARI_HOST`, config env prefix, nested `__` keys)
  - Document the deprecation of the legacy `AGENTGUARD_*` prefix (accepted at lower precedence with a one-time deprecation warning; removal in a future release)
  - _Requirements: 2.6, 3.6_

### Phase E — Final verification

- [ ] 36. Full quality gate — lint, types, tests, coverage
  - Run `ruff` and fix any lint issues
  - Run `mypy --strict` (per project config) and confirm zero type errors across all changed modules
  - Run the full `pytest` suite (unit + property + integration) in single-execution mode (no watch)
  - Confirm the 90% coverage threshold is met; add targeted tests for any uncovered new branches
  - Confirm every exploration test (tasks 1,4,7,10,13,16,19,22,25,28,31) now PASSES and every preservation test still PASSES (no regressions)
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12_

## Notes

Shared-file coordination and cross-defect dependencies:

- `src/ostiari/guard.py` is edited by Defect 5 (task 15) then Defect 7 (task 21). Task 21's allow
  short-circuit reuses the generate-once trace id introduced in task 15, so 15 must land first.
- `src/ostiari/dashboard/app.py` is edited by Defect 2 (task 6) → Defect 3 (task 9) → Defect 6
  (task 18). Task 9 reads `OSTIARI_HOST` for its non-loopback guard; task 18 formalizes that env
  var with legacy fallback. Apply in this order to avoid merge conflicts and keep host reading correct.
- All other defects are orthogonal and can be implemented/tested in parallel.
- Every defect follows: explore (fails on unfixed code) → preserve (passes on unfixed code) →
  fix.implement → verify-fix (passes) → verify-preserve (passes). Do NOT rewrite the explore/preserve
  tests during verification — re-run the same tests.
- Hypothesis-backed defects (value domains): 1, 5, 6, 7, 8, 11. Structural/targeted-test defects:
  2, 3, 4, 9, 10.
- Every task must keep strict mypy clean and the suite at ≥90% coverage; the final gate (task 36)
  enforces ruff + mypy --strict + full pytest + coverage.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "description": "Independent defect starts (parallel): Defects 1, 2, 4, 5, 8, 9, 10, 11",
      "tasks": ["1", "2", "3", "4", "5", "6", "10", "11", "12", "13", "14", "15", "22", "23", "24", "25", "26", "27", "28", "29", "30", "31", "32", "33"]
    },
    {
      "wave": 2,
      "description": "Second edits to shared files: Defect 3 (after Defect 2 on app.py), Defect 7 (after Defect 5 on guard.py)",
      "tasks": ["7", "8", "9", "19", "20", "21"]
    },
    {
      "wave": 3,
      "description": "Final edit to app.py: Defect 6 (after Defects 2 and 3)",
      "tasks": ["16", "17", "18"]
    },
    {
      "wave": 4,
      "description": "Documentation after the code they describe is merged",
      "tasks": ["34", "35"]
    },
    {
      "wave": 5,
      "description": "Full quality gate: ruff, mypy --strict, full pytest, coverage",
      "tasks": ["36"]
    }
  ]
}
```
