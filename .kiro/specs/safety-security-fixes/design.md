# Safety & Security Fixes Bugfix Design

## Overview

This design specifies the fix for eleven verified defects in Ostiari, the runtime safety/guardrail layer for AI agents. Each defect shares a common failure mode: the system keeps running and appears healthy while a safety guarantee it advertises is silently violated. The fixes are therefore treated as safety regressions, not cosmetic bugs.

The defects fall into three severity bands, matching the approved requirements:

- **Critical (safety/security)** — Defect 1 (policy merge drops block rules), Defect 2 (WebSocket auth bypass), Defect 3 (dashboard open by default), Defect 4 (`fail_open` default flip — a documented breaking change).
- **High (correctness)** — Defect 5 (`trace_id` mismatch), Defect 6 (stale `AGENTGUARD_` env prefix), Defect 7 (`allow` decision not honored), Defect 8 (inert `LIKE` escaping).
- **Medium (reliability)** — Defect 9 (shared single-worker intervention pool), Defect 10 (silent trace drops), Defect 11 (breaker recovery timer resets on restart).

The general strategy for every defect is the same: make the smallest change that restores the intended safety behavior for the buggy inputs, while proving through property-based preservation tests that all non-buggy inputs behave exactly as they do today. Because the project enforces 90% coverage and strict mypy, every fix must ship with typed code and both unit and (where a domain exists to quantify over) property-based tests.

Each defect is treated as an independent bug condition `C(X)` with its own property `P(result)` and preservation goal for `¬C(X)`. The defects are largely orthogonal, so they can be implemented and tested in isolation; the only shared surface is `guard.py` (Defects 5 and 7) and the dashboard app (Defects 2, 3, 6).

## Glossary

- **Bug_Condition (C)**: The predicate identifying inputs that trigger a given defect. Defined per-defect in the Bug Details section.
- **Property (P)**: The correct behavior the fixed function must satisfy for inputs where `C(X)` holds.
- **Preservation**: The requirement that for inputs where `¬C(X)`, the fixed function `F'` produces the same observable result as the original function `F`.
- **F / F'**: The original (unfixed) function and the fixed function, respectively.
- **PolicyEngine.merge** — the static method in `src/ostiari/policy/engine.py` that combines two `PolicySet` objects into one, used both for multi-file loads and decorator merges.
- **union semantics** — combining rule collections so no rule from either input is dropped (as opposed to replace semantics, where a non-empty override collection discards the base collection).
- **TokenAuthMiddleware** — the Starlette `BaseHTTPMiddleware` subclass in `src/ostiari/dashboard/middleware.py` that enforces bearer-token auth on HTTP routes only.
- **/ws/live** — the FastAPI WebSocket endpoint in `src/ostiari/dashboard/app.py` that streams live traces.
- **fail_open** — the `OstiariConfig` flag that, when `True`, allows an action if a pipeline stage errors; when `False`, the pipeline fails closed (block/intervene).
- **ValidationResult.trace_id** — the trace identifier returned to the caller of `Guard.validate`; must resolve to a persisted `TraceEntry`.
- **policy decision** — the `decision` field of `PolicyResult`, one of `"allow"`, `"block"`, `"evaluate"`.
- **_glob_to_sql** — the helper in `src/ostiari/storage/sqlite.py` that translates glob patterns (`*`/`?`) into SQL `LIKE` patterns and escapes literal `%`/`_` with a backslash.
- **_callback_pool** — the module-level `ThreadPoolExecutor` in `src/ostiari/gateway.py` used to run intervention callbacks under a timeout.
- **tracer queue** — the bounded `deque(maxlen=queue_max)` in `src/ostiari/tracer.py` buffering traces awaiting persistence.
- **tripped_at** — the timestamp at which a circuit breaker opened; wallclock (`datetime`) when persisted, but the in-memory `BreakerInstance.tripped_at` is measured by `self._clock` which defaults to `time.monotonic`.
- **monotonic vs wallclock** — `time.monotonic()` is a process-relative, unsynchronized clock that resets each process; `datetime.now(timezone.utc)` is wallclock. Comparing a persisted wallclock `tripped_at` to a monotonic in-memory clock is invalid and is the crux of Defect 11.

## Bug Details

### Defect 1 — Policy merge drops block rules

**Bug Condition.** The bug manifests when two or more policy sources are merged (multi-file load via `_load_from_paths`, decorator merge, or remote reload) and more than one source defines `block` rules (or more than one defines `allow` rules). `PolicyEngine.merge` computes `merged_block = override_block if override_block else base_block`, so any non-empty override `block` list replaces (rather than unions with) the base `block` list, silently dropping earlier block rules. The same replace logic applies to `allow`.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = ordered list of PolicySet sources being merged
  OUTPUT: boolean
  RETURN (EXISTS i < j SUCH THAT hasBlockRule(X[i]) AND hasBlockRule(X[j]))
         OR (EXISTS i < j SUCH THAT hasAllowRule(X[i]) AND hasAllowRule(X[j]))
END FUNCTION
```

**Examples:**
- File A blocks `*.delete_database`; File B blocks `*.drop_table`. Today the merged policy blocks only `*.drop_table`. Expected: both are blocked.
- File A allows `read_*`; File B allows `list_*`. Today only `list_*` is allowed. Expected: both allowed.
- File A blocks `*.delete_database`; File B has no block rules. Today (and expected): `*.delete_database` remains blocked (no regression).
- Edge case: File A and File B both block the identical pattern `*.delete_database`. Expected: union deduplicates to a single effective block (union should not create contradictory duplicates that change evaluation outcome).

### Defect 2 — WebSocket bypasses authentication

**Bug Condition.** The bug manifests when a token is configured and a client opens a WebSocket connection to `/ws/live`. `TokenAuthMiddleware` extends Starlette `BaseHTTPMiddleware`, which is only invoked for `http` scope; the `websocket` branch inside `dispatch()` is dead code. The endpoint accepts the socket and streams traces without ever checking the token.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = incoming connection to /ws/live
  OUTPUT: boolean
  RETURN tokenConfigured() AND X.scope == "websocket"
END FUNCTION
```

**Examples:**
- Token configured; client connects to `/ws/live` with no `?token=`. Today: stream served. Expected: connection rejected (closed before any trace is sent).
- Token configured; client connects with `?token=<wrong>`. Today: stream served. Expected: connection rejected.
- Token configured; client connects with `?token=<correct>`. Today and expected: stream served (no regression).
- Edge case: no token configured; client connects to `/ws/live`. Expected: stream served (auth not enforced when unauthenticated mode is in effect — see Defect 3 warning).

### Defect 3 — Dashboard open by default

**Bug Condition.** The bug manifests when the dashboard is created via `create_app` without a token (no `token` argument and no token env var), which exposes the trace viewer, policy editor, and intervention-approval endpoints with no authentication and emits no warning — even when bound to a non-loopback host such as `0.0.0.0`.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = dashboard startup config (token, host)
  OUTPUT: boolean
  RETURN X.token IS EMPTY
END FUNCTION
```

**Examples:**
- `create_app()` with no token, default host. Today: silent. Expected: an unauthenticated-mode warning is logged.
- `create_app()` with no token, host `0.0.0.0`. Today: silent, network-exposed. Expected: a strong warning (or refusal, per design decision below) because an open surface is bound to a non-loopback interface.
- `create_app(token="secret")`. Today and expected: no unauthenticated warning; auth enforced (no regression).

### Defect 4 — fail_open default (breaking change)

**Bug Condition.** The bug manifests when a `Guard`/`OstiariConfig` is constructed without explicitly setting `fail_open`, and a pipeline stage raises an error. `fail_open` defaults to `True`, so the action is allowed on error, and the `ostiari init` scaffold writes `fail_open: true`, propagating the unsafe default into user projects.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = pipeline execution where a stage raised an error
  OUTPUT: boolean
  RETURN failOpenNotExplicitlySet(X) AND pipelineErrorOccurred(X)
END FUNCTION
```

**Examples:**
- `Guard()` with no config; policy engine raises during evaluation. Today: action allowed. Expected: action blocked/intervened (fail closed).
- `ostiari init` scaffold. Today: writes `fail_open: true`. Expected: writes `fail_open: false`.
- `OstiariConfig(fail_open=True)` explicitly; stage raises. Today and expected: action allowed (explicit opt-in preserved — no regression).

### Defect 5 — trace_id mismatch

**Bug Condition.** The bug manifests on any successful `validate()` call that records a trace. `_record_trace` generates one `uuid` for the stored `TraceEntry`, while the `ValidationResult` is built with a different `uuid`, so the returned `trace_id` never resolves to the persisted trace.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = any successful validate() call that records a trace
  OUTPUT: boolean
  RETURN traceRecorded(X)
END FUNCTION
```

**Examples:**
- `validate("read_file")` returns `ValidationResult(trace_id=A)`; storage holds `TraceEntry(trace_id=B)`. Today: `get_trace(A)` is `None`. Expected: `get_trace(A)` returns the stored trace with `trace_id == A`.
- Edge case: block path. `validate` raises `ActionBlockedError` before building a `ValidationResult`; the block trace is still recorded. The correction must not require a returned `trace_id` on the block path (no `ValidationResult` is returned), but the generate-once helper should be used consistently for all trace records.

### Defect 6 — env var prefix

**Bug Condition.** The bug manifests when a user sets `OSTIARI_*` environment variables (config prefix, `OSTIARI_CONFIG`, `OSTIARI_TOKEN`, `OSTIARI_REDIS_URL`, `OSTIARI_DB`, `OSTIARI_HOST`). `config.py` and `dashboard/app.py` still read the legacy `AGENTGUARD_*` prefix from before the project rename, so the documented `OSTIARI_*` variables are silently ignored.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = environment mapping
  OUTPUT: boolean
  RETURN EXISTS v IN X SUCH THAT v STARTS WITH "OSTIARI_"
END FUNCTION
```

**Examples:**
- `OSTIARI_FAIL_OPEN=false`. Today: ignored, config keeps default. Expected: `config.fail_open is False`.
- `OSTIARI_TOKEN=secret` for the dashboard. Today: ignored, dashboard runs open. Expected: token auth enabled.
- `OSTIARI_THRESHOLDS__ALLOW_MAX=20`. Today: ignored. Expected: `thresholds.allow_max == 20`.
- Transition case: `AGENTGUARD_FAIL_OPEN=false` set by an existing user. Behavior depends on the design decision below (dual-prefix with deprecation warning).

### Defect 7 — allow decision not honored

**Bug Condition.** The bug manifests when policy evaluation returns `decision == "allow"` for an action. The pipeline only short-circuits on `decision == "block"`; for `allow` it still runs anomaly detection and gateway risk scoring, so anomaly/risk signals can escalate an allowed action to `intervene` or `block`.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = action whose policy evaluation yields decision == "allow"
  OUTPUT: boolean
  RETURN policyDecision(X) == "allow"
END FUNCTION
```

**Examples:**
- `read_file` matched by an `allow` rule but anomaly detector adds +80 for repetition. Today: escalated to `block`. Expected: `allow` (authoritative).
- `list_dir` allowed, no anomalies. Today and expected: `allow` (no observable change).
- `decision == "block"`. Today and expected: short-circuit block (no regression).
- `decision == "evaluate"`. Today and expected: run anomaly + gateway scoring (no regression).

### Defect 8 — LIKE escaping inert

**Bug Condition.** The bug manifests when a trace query's `action` filter contains a literal `%` or `_`. `_glob_to_sql` escapes them as `\%`/`\_`, but the SQL `action LIKE ?` clause has no `ESCAPE '\'` clause, so SQLite treats the backslash as a literal character and the `%`/`_` still act as wildcards.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = TraceFilters.action containing a literal '%' or '_'
  OUTPUT: boolean
  RETURN containsLiteralWildcard(X.action)   // '%' or '_' present in the filter string
END FUNCTION
```

**Examples:**
- Filter `action="50%_done"`. Today: `%` and `_` match arbitrary characters, over-matching unrelated actions. Expected: only actions containing the literal substring `50%_done` match.
- Filter `action="get_*"` (glob `*`). Today and expected: `*` → `%` wildcard match on the prefix `get_` (no regression; note the `_` here is literal and must match literally).
- Filter `action="read_file"` (plain, contains `_`). Today: `_` acts as wildcard, matching `readXfile`. Expected: matches only literal `read_file`.

### Defect 9 — shared single-worker intervention pool

**Bug Condition.** The bug manifests when an intervention callback hangs. `gateway.py` uses a single module-level `ThreadPoolExecutor(max_workers=1)`; a timed-out callback keeps running in the sole worker (a `future.result(timeout=...)` timeout does not cancel the running task), so every subsequent intervention across the whole process is wedged behind it.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = set of concurrent interventions where at least one callback hangs
  OUTPUT: boolean
  RETURN EXISTS callback IN X SUCH THAT hangs(callback)
END FUNCTION
```

**Examples:**
- Callback A hangs indefinitely; callback B submitted afterward. Today: B never runs, its own timeout never fires because it never starts. Expected: B runs and times out (or completes) independently of A.
- Two `Guard`/`ActionGateway` instances in one process. Today: both share `_callback_pool`; a hang in one wedges the other. Expected: instances are isolated.
- Callback returns within timeout. Today and expected: approve/deny honored (no regression).

### Defect 10 — silent trace drops

**Bug Condition.** The bug manifests when traces are produced faster than they can be persisted: the tracer `deque(maxlen=queue_max)` silently discards the oldest entry on overflow, and the storage `RetryExecutor` returns `None` (fail-open) on write failure, dropping the batch. Neither path increments a counter or emits a warning.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = trace production/persistence sequence
  OUTPUT: boolean
  RETURN queueOverflowOccurred(X) OR storageWriteDropped(X)
END FUNCTION
```

**Examples:**
- 1001 traces recorded into a queue of maxlen 1000 before the writer drains it. Today: 1 trace silently evicted. Expected: a dropped-trace counter increments and a (rate-limited) warning is logged.
- Storage batch write fails all retries with `fail_open=True`. Today: batch silently dropped (only an ERROR log inside RetryExecutor). Expected: the tracer records the drop count for the batch and surfaces it via the counter/warning.
- Traces within capacity. Today and expected: recorded and persisted in order, non-blocking (no regression).

### Defect 11 — breaker recovery timer resets on restart

**Bug Condition.** The bug manifests when a process restarts and `CircuitBreaker.restore_state` loads a persisted open breaker with a prior `tripped_at`. The current code sets `breaker.tripped_at = self._clock()` (i.e., "now"), restarting the recovery countdown from zero on every restart. The subtlety: the persisted `tripped_at` is wallclock (`datetime`), but the in-memory `tripped_at` is compared against `self._clock()`, which defaults to `time.monotonic()` — a process-relative clock. Simply storing the persisted wallclock value into `breaker.tripped_at` would make `check()`'s `elapsed = self._clock() - breaker.tripped_at` meaningless (subtracting a Unix timestamp from a monotonic reading).

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = restart restoring a persisted open breaker with a prior tripped_at
  OUTPUT: boolean
  RETURN X.persistedState.state == "open" AND X.persistedState.tripped_at IS NOT NULL
END FUNCTION
```

**Examples:**
- Breaker tripped 50s ago (wallclock), `recovery_after_seconds=60`, process restarts. Today: countdown resets, breaker won't probe until 60s after restart. Expected: after ~10 more seconds of real time the breaker transitions to `half_open`.
- Breaker tripped 120s ago (wallclock), `recovery_after_seconds=60`, process restarts. Today: 60s more wait. Expected: recovery already elapsed; breaker immediately eligible for `half_open` on first `check()`.
- Persisted state is `closed`, or `tripped_at` is `None`. Today and expected: no change (no regression).

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors (per defect):**

- **Defect 1**: `risk_adjust`, `threshold_override`, and `context_rule` merge/override semantics are unchanged; rule ordering (priority desc, then `type_order`) is unchanged; threshold merge (`global_thresholds` selection and `per_tool` update) is unchanged.
- **Defect 2**: HTTP bearer-token auth on routes is unchanged; a correct `?token=` still connects; exempt behavior for the socket path is unaffected.
- **Defect 3**: When a token IS configured, no unauthenticated warning is emitted and auth behaves exactly as today; loopback bind without token still starts (with a warning).
- **Defect 4**: Explicit `fail_open=True` still allows on pipeline error; the fail-open code path itself is unchanged — only the default and the scaffold value change.
- **Defect 5**: All existing `TraceEntry` fields are stored and remain retrievable via `get_trace`/`get_traces`; only the returned `trace_id` is corrected to match the persisted one.
- **Defect 6**: Explicit-argument and non-prefixed (YAML, overrides) configuration paths are unchanged; nested `__` env decoding is unchanged; the `_coerce` type coercion is unchanged.
- **Defect 7**: `block` short-circuit is unchanged; `evaluate` still runs anomaly detection and gateway risk scoring; trace recording still occurs for allow outcomes.
- **Defect 8**: Glob `*`/`?` translation is unchanged; plain patterns with no wildcards still match as before; all other `TraceFilters` conditions (time, risk, tier, correlation) are unchanged.
- **Defect 9**: Callbacks that return within timeout still have their approve/deny result honored; timeout semantics (raising `ActionInterventionTimeout`) are unchanged; sync and async callback support is unchanged.
- **Defect 10**: Within-capacity recording and persistence order, and the non-blocking batching behavior, are unchanged; no new blocking is introduced.
- **Defect 11**: Genuine open → half_open recovery still works; `trip`, `reopen`, `close`, `record`, `report_outcome`, and probe logic are unchanged for the running process.

**Scope.** For each defect, all inputs where `¬C(X)` holds must be completely unaffected. The preservation property tests below quantify over these input domains.

**Note:** The correct behavior for buggy inputs is defined in the Correctness Properties section below.

## Hypothesized Root Cause

1. **Defect 1 — Replace-instead-of-union**: `merge` intentionally uses replace semantics for `allow`/`block` (`override_block if override_block else base_block`). The design intent was "later file wins", but for `block`/`allow` that silently weakens the guardrail. Root cause: wrong merge operator for safety-critical rule types.

2. **Defect 2 — Wrong middleware base for WebSockets**: `BaseHTTPMiddleware.dispatch` never runs for `websocket` scope; the websocket branch is unreachable. Root cause: auth was implemented at the HTTP middleware layer, which structurally cannot cover WebSockets.

3. **Defect 3 — Missing warning on unauthenticated startup**: `create_app` only logs when a token IS present and host is non-loopback; the no-token case has no branch. Root cause: absence of a guard clause for the unauthenticated path.

4. **Defect 4 — Unsafe default**: `OstiariConfig.fail_open` defaults to `True`, and the scaffold hardcodes `fail_open: true`. Root cause: default chosen for availability over safety.

5. **Defect 5 — Two independent UUIDs**: `_record_trace` and `_execute_pipeline` each call `uuid.uuid4()` independently. Root cause: the trace id is generated in two places instead of once and threaded through.

6. **Defect 6 — Stale prefix after rename**: the project was renamed AgentGuard → Ostiari but the env prefix and env var names were not updated in `config.py`/`dashboard/app.py`. Root cause: incomplete rename.

7. **Defect 7 — Missing allow short-circuit**: `_execute_pipeline` short-circuits on `block` but falls through for `allow`. Root cause: allow was treated as advisory rather than authoritative.

8. **Defect 8 — Missing ESCAPE clause**: `_glob_to_sql` escapes with backslash but the SQL has no `ESCAPE '\'`, so SQLite ignores the escape. Root cause: escape character declared in the value but not in the SQL statement.

9. **Defect 9 — Shared single-worker pool**: a module-level `ThreadPoolExecutor(max_workers=1)` is shared process-wide and cannot cancel a hung task. Root cause: global shared resource with no isolation and no cancellation.

10. **Defect 10 — Silent drop paths**: `deque(maxlen=...)` eviction and `RetryExecutor` fail-open return `None` both discard data without accounting. Root cause: no observability on the drop paths.

11. **Defect 11 — Clock domain confusion + reset**: `restore_state` overwrites `tripped_at` with `self._clock()`, and there is a clock-domain mismatch between persisted wallclock and in-memory monotonic. Root cause: recovery elapsed-time was never designed to survive a restart, and the two clock domains were never reconciled.

## Correctness Properties

Property 1: Bug Condition — Policy merge unions block and allow rules

_For any_ ordered list of policy sources where more than one source defines `block` rules (or more than one defines `allow` rules), the fixed `merge`/`_load_from_paths` SHALL retain the union of all `block` rules and the union of all `allow` rules across every source, dropping none.

**Validates: Requirements 2.1**

Property 2: Preservation — Non-allow/block merge semantics unchanged

_For any_ pair of policy sources where the bug condition does NOT hold, the fixed `merge` SHALL produce the same result as the original for `risk_adjust`/`threshold_override`/`context_rule` override semantics, rule ordering, and threshold merge.

**Validates: Requirements 3.1**

Property 3: Bug Condition — WebSocket auth enforced

_For any_ WebSocket connection to `/ws/live` when a token is configured, the fixed endpoint SHALL stream trace data only if the supplied token matches the configured token, and SHALL reject (close) connections with a missing or invalid token before sending any trace.

**Validates: Requirements 2.2**

Property 4: Preservation — HTTP auth and valid-token access unchanged

_For any_ HTTP request or WebSocket connection with a valid token, or any exempt HTTP path, the fixed dashboard SHALL authorize exactly as the original.

**Validates: Requirements 3.2, 3.3**

Property 5: Bug Condition — Unauthenticated dashboard warns (and guards non-loopback bind)

_For any_ dashboard startup where no token is configured, the fixed `create_app`/CLI SHALL emit a clear unauthenticated-mode warning, and SHALL warn or refuse when bound to a non-loopback host without a token.

**Validates: Requirements 2.3**

Property 6: Bug Condition — Default is fail-closed

_For any_ pipeline execution where `fail_open` was not explicitly set and a stage raises, the fixed system SHALL produce an outcome in `{block, intervene}` (never `allow`), and the `ostiari init` scaffold SHALL write `fail_open: false`.

**Validates: Requirements 2.4**

Property 7: Preservation — Explicit fail_open=True still fails open

_For any_ pipeline execution where `fail_open` is explicitly `True` and a stage raises, the fixed system SHALL allow the action, identical to the original.

**Validates: Requirements 3.4**

Property 8: Bug Condition — Returned trace_id resolves to the stored trace

_For any_ successful `validate()` call that records a trace, the fixed `ValidationResult.trace_id` SHALL equal the persisted `TraceEntry.trace_id`, such that `storage.get_trace(result.trace_id)` returns that exact trace.

**Validates: Requirements 2.5**

Property 9: Preservation — Trace fields and retrieval unchanged

_For any_ recorded trace, the fixed system SHALL store all existing `TraceEntry` fields and keep them retrievable via `get_trace`/`get_traces` exactly as the original; only the returned id is corrected.

**Validates: Requirements 3.5**

Property 10: Bug Condition — OSTIARI_* variables take effect

_For any_ environment containing `OSTIARI_*` variables (config prefix and the dashboard `OSTIARI_CONFIG`/`OSTIARI_TOKEN`/`OSTIARI_REDIS_URL`/`OSTIARI_DB`/`OSTIARI_HOST`), the fixed system SHALL read and apply each one.

**Validates: Requirements 2.6**

Property 11: Preservation — Non-prefixed config paths unchanged; legacy prefix documented

_For any_ configuration via explicit argument, YAML, overrides, or nested `__` env decoding, the fixed system SHALL behave identically to the original; legacy `AGENTGUARD_*` handling SHALL follow the documented transition behavior.

**Validates: Requirements 3.6**

Property 12: Bug Condition — allow is authoritative

_For any_ action whose policy evaluation yields `decision == "allow"`, the fixed pipeline SHALL short-circuit to a final tier of `allow`, never escalated by anomaly/risk signals.

**Validates: Requirements 2.7**

Property 13: Preservation — block and evaluate paths unchanged

_For any_ action whose policy evaluation yields `decision` in `{block, evaluate}`, the fixed pipeline SHALL produce the same result as the original (block short-circuits; evaluate runs anomaly + gateway scoring).

**Validates: Requirements 3.7, 3.8**

Property 14: Bug Condition — literal %/_ matched literally

_For any_ trace query `action` filter containing a literal `%` or `_`, the fixed `get_traces` SHALL match only actions containing that literal character, by pairing the escaped `LIKE` pattern with `ESCAPE '\'`.

**Validates: Requirements 2.8**

Property 15: Preservation — glob and plain patterns unchanged

_For any_ trace query filter using glob `*`/`?` or containing no wildcard characters, the fixed `get_traces` SHALL return the same results as the original.

**Validates: Requirements 3.9**

Property 16: Bug Condition — one hung callback does not wedge all interventions

_For any_ set of interventions where one callback hangs, the fixed gateway SHALL allow other interventions to make progress or time out independently, and SHALL not leave the only worker permanently consumed.

**Validates: Requirements 2.9**

Property 17: Preservation — normal callback semantics unchanged

_For any_ intervention callback that returns within its timeout, the fixed gateway SHALL honor its approve/deny result and preserve existing timeout semantics.

**Validates: Requirements 3.10**

Property 18: Bug Condition — trace drops are counted/surfaced

_For any_ sequence where the tracer queue overflows or a storage write is dropped fail-open, the fixed system SHALL increment a dropped-trace counter and/or emit a warning rather than dropping silently.

**Validates: Requirements 2.10**

Property 19: Preservation — within-capacity tracing unchanged

_For any_ trace sequence within queue capacity, the fixed tracer SHALL record and persist in order with the existing non-blocking batching behavior.

**Validates: Requirements 3.11**

Property 20: Bug Condition — original trip time preserved across restart

_For any_ restart restoring a persisted open breaker with a prior `tripped_at`, the fixed `restore_state` SHALL compute recovery elapsed time from the original (wallclock) trip time rather than resetting to now, correctly reconciling the wallclock-vs-monotonic clock domains.

**Validates: Requirements 2.11**

Property 21: Preservation — genuine recovery transition unchanged

_For any_ breaker whose recovery interval has genuinely elapsed, the fixed system SHALL still transition open → half_open and probe for recovery as the original.

**Validates: Requirements 3.12**

## Fix Implementation

Assuming the root-cause analysis is correct, the following changes are required.

### Defect 1 — `src/ostiari/policy/engine.py`, `PolicyEngine.merge`

- Replace the replace-semantics lines with union:
  - `merged_allow = base_allow + [r for r in override_allow if <not a duplicate>]`
  - `merged_block = base_block + [r for r in override_block if <not a duplicate>]`
- Deduplicate by a rule identity key (e.g., `(type, action, priority, description, enabled, risk_adjust, threshold_override, context)` — since `Rule` is frozen/hashable via pydantic, use equality-based dedup preserving first occurrence) so identical rules across files do not multiply.
- Preserve `merged_other` override semantics, the `type_order` sort, and the threshold merge exactly as today.
- Confirm all three call sites (`_load_from_paths`, `register_decorator_rules`, `reload_from_content`) inherit the union behavior automatically since they all route through `merge`.

### Defect 2 — `src/ostiari/dashboard/app.py` (and `middleware.py`)

- Enforce token auth inside the `/ws/live` endpoint (the only place that runs for websocket scope). Read the token from `websocket.query_params.get("token", "")`, compare with `hmac.compare_digest`, and on mismatch call `await websocket.close(code=1008)` (policy violation) and return before `ws_manager.connect`.
- Capture the configured token in a closure variable available to `websocket_endpoint`.
- Keep `TokenAuthMiddleware` for HTTP routes; optionally remove the now-dead websocket branch in `dispatch()` (or leave it with a comment) — removing is cleaner. Extract the token-compare into a small shared helper to avoid divergence.

### Defect 3 — `src/ostiari/dashboard/app.py` and `src/ostiari/cli.py`

- In `create_app`, add an `else` branch to the `if token:` block: when no token is configured, `logger.warning(...)` an explicit unauthenticated-mode message.
- Read host from the (fixed) `OSTIARI_HOST` env var; if no token and host is non-loopback, escalate to a stronger warning. **Design decision:** warn loudly by default (do not hard-refuse in `create_app`, to avoid breaking programmatic embedding), but in the `dashboard` CLI command refuse to start when `host` is non-loopback and no token is set, unless an explicit `--allow-unauthenticated` flag is passed. This puts the refusal at the user-facing entry point while keeping the library flexible.
- Add the `--allow-unauthenticated` flag to the `dashboard` CLI command.

### Defect 4 — `src/ostiari/models.py` and `src/ostiari/cli.py`

- Change `OstiariConfig.fail_open` default from `True` to `False`.
- Change the `init` scaffold in `cli.py` to write `fail_open: false`.
- Documentation: update `README.md` and any docs that describe `fail_open` to call out the breaking change (post-upgrade, pipeline errors block/intervene rather than allow) and how to restore the old behavior (`fail_open: true`).

### Defect 5 — `src/ostiari/guard.py`

- Generate the trace id once. Refactor `_record_trace` to accept an optional `trace_id` (or return the id it used) so the same id is threaded into the returned `ValidationResult`.
- In `_execute_pipeline`, allocate `trace_id = str(uuid.uuid4())` before recording, pass it to `_record_trace`, and use the same value in `ValidationResult(trace_id=trace_id, ...)`.
- Apply the same generate-once approach on the block path's `_record_trace` call for consistency (no returned result there, but keeps the helper uniform).

### Defect 6 — `src/ostiari/config.py` and `src/ostiari/dashboard/app.py`

- **Design decision (transition):** accept both prefixes with a deprecation warning, rather than a hard switch. Rationale: this is a rename, not a semantic change; silently breaking existing `AGENTGUARD_*` deployments would repeat the same "silent config ignored" failure mode this bugfix is eliminating. `OSTIARI_*` takes precedence; if only `AGENTGUARD_*` is present, apply it and log a one-time deprecation warning naming the new variable.
- `config.py`: default `env_prefix="OSTIARI_"`; in `_parse_env`, also scan the legacy prefix and merge with lower precedence, emitting a deprecation warning when a legacy key is used. Update `_resolve_yaml_path` and `_describe_sources` to check `OSTIARI_CONFIG` first, then legacy `AGENTGUARD_CONFIG` (with warning).
- `dashboard/app.py`: read `OSTIARI_REDIS_URL`/`OSTIARI_TOKEN`/`OSTIARI_DB`/`OSTIARI_HOST` first, falling back to the legacy names with a deprecation warning.
- Documentation: document the supported `OSTIARI_*` variables and the deprecation of `AGENTGUARD_*`.

### Defect 7 — `src/ostiari/guard.py`, `_execute_pipeline`

- After the `block` short-circuit and before anomaly detection, add: if `policy_result and policy_result.decision == "allow"`, build a `GatewayDecision(tier="allow", score=0, signals=[...])` (or short-circuit directly), record the trace with `tier="allow"`, run post-hooks, and return a `ValidationResult` with `tier="allow"` — without invoking the anomaly detector or gateway.
- Preserve the `block` and `evaluate` code paths untouched. Ensure breaker probe reporting and `_record_breaker_metrics(is_block=False)` still run on the allow path to match existing non-block behavior.
- Reuse the Defect 5 generate-once trace id on this new path.

### Defect 8 — `src/ostiari/storage/sqlite.py`, `get_traces`

- Change the action condition from `"action LIKE ?"` to `"action LIKE ? ESCAPE '\\'"`.
- Keep `_glob_to_sql` unchanged (it already produces `\%`/`\_` escapes and `%`/`_` wildcards). Verify the backslash escape string is correct for the sqlite3 driver.

### Defect 9 — `src/ostiari/gateway.py`

- Remove the module-level `_callback_pool`. Give each `ActionGateway` instance its own executor (created lazily), e.g. `self._callback_pool = ThreadPoolExecutor(max_workers=N, thread_name_prefix="ostiari-intervention")` with `N > 1` (default small, e.g. 4) so a single hang cannot consume the only worker.
- On timeout, cancel the future (`future.cancel()`) and, since threads cannot be force-killed, ensure the pool has spare workers and mark the callback as timed out; document that a hung callback still occupies one worker until it returns but no longer blocks others.
- Add an `close()`/shutdown path for the per-instance pool (call from `Guard.shutdown` via the gateway) to avoid thread leaks.
- Preserve sync/async invocation, timeout raising `ActionInterventionTimeout`, and the `_fail_open` fallback on generic exceptions.

### Defect 10 — `src/ostiari/tracer.py` (and `storage/sqlite.py`)

- In `ExecutionTracer.record`, detect overflow before appending: when `len(self._queue) == self._queue_max`, increment `self._dropped_count` and emit a rate-limited warning; then append (the deque still evicts the oldest, but the drop is now accounted).
- Add a `dropped_count` property and expose it (e.g., for stats / health).
- In `_flush_batch`, when `save_traces_batch` fails (exception path already logs), also increment `self._dropped_count` by `len(batch)` and warn — this covers the storage fail-open write-drop case observably at the tracer boundary. Optionally surface the storage-side fail-open `None` return as a signal, but the tracer-boundary accounting is sufficient and simplest.
- Keep the batching/flush loop and non-blocking `record` semantics otherwise unchanged.

### Defect 11 — `src/ostiari/breaker.py`, `CircuitBreaker.restore_state`

- Reconcile clock domains. The persisted `tripped_at` is wallclock (`datetime`). Compute how long ago the breaker tripped in real seconds: `elapsed_since_trip = (now_wallclock - persisted.tripped_at).total_seconds()` (clamped to `>= 0`). Then set the in-memory `breaker.tripped_at = self._clock() - elapsed_since_trip`, so that a subsequent `check()` computing `self._clock() - breaker.tripped_at` yields the true elapsed recovery time regardless of whether `self._clock` is monotonic or wallclock.
- This works for both the default monotonic clock and an injected wallclock/test clock, because it anchors the persisted elapsed time to the current clock reading in the clock's own units.
- Guard against negative/clock-skew values (clamp to 0) and only apply when `state == "open"` and `tripped_at is not None`; leave `closed`/`half_open` and null cases as today (`state` restored, `tripped_at` left None/unchanged).
- Restore `counter` as today.

## Testing Strategy

### Validation Approach

The strategy is two-phase for every defect: first, write an exploratory test that reproduces the bug on the UNFIXED code (confirming the root cause and producing a counterexample); then implement the fix and add fix-checking and preservation tests. The project uses pytest + Hypothesis, enforces 90% coverage, and runs strict mypy, so every fix ships with typed tests and, where a meaningful input domain exists, a Hypothesis property test. Preservation tests capture current behavior on non-buggy inputs and assert the fix does not change it.

Run tests with `pytest` (single execution — no watch mode). Property-based tests should be marked so the tasks phase can track them individually.

### Exploratory Bug Condition Checking

**Goal:** Surface counterexamples that demonstrate each bug BEFORE the fix. Confirm or refute the root cause; if refuted, re-hypothesize.

**Test Plan and Cases (per defect):**
1. **Defect 1:** Merge two `PolicySet`s each with a distinct `block` rule; assert both survive. Fails on unfixed code (only the override survives).
2. **Defect 2:** Spin up the dashboard test client with a token; open `/ws/live` with no/invalid token; assert the socket is closed before any message. Fails on unfixed code (stream served).
3. **Defect 3:** Call `create_app()` with no token under a log-capture fixture; assert an unauthenticated warning was logged. Fails on unfixed code (no warning). Also assert the CLI refuses non-loopback bind without token.
4. **Defect 4:** Construct `OstiariConfig()` and assert `fail_open is False`; assert `init` scaffold writes `fail_open: false`. Fails on unfixed code.
5. **Defect 5:** `validate()` a benign action; assert `storage.get_trace(result.trace_id)` is not `None`. Fails on unfixed code (returns `None`).
6. **Defect 6:** Set `OSTIARI_FAIL_OPEN=false`; `ConfigLoader.load()`; assert `config.fail_open is False`. Fails on unfixed code (ignored).
7. **Defect 7:** Configure an `allow` rule for an action and an anomaly detector that adds a high score; `validate()`; assert final tier `allow`. Fails on unfixed code (escalated).
8. **Defect 8:** Insert traces with actions `read_file` and `readXfile`; query with `action="read_file"`; assert only `read_file` matches. Fails on unfixed code (`_` wildcard matches both).
9. **Defect 9:** Register a hanging callback, trigger an intervention on one thread, then trigger a second intervention with a short timeout on another; assert the second returns/times out promptly. Fails on unfixed code (second wedged).
10. **Defect 10:** Fill the tracer queue past `queue_max` without draining; assert `dropped_count > 0` and a warning was logged. Fails on unfixed code (no counter).
11. **Defect 11:** Persist an open breaker with `tripped_at` in the past beyond `recovery_after_seconds`; construct a new `CircuitBreaker`, `restore_state()`, `check()`; assert it becomes eligible for `half_open` immediately. Fails on unfixed code (countdown reset).

**Expected Counterexamples:** As noted per case above (dropped block rule; streamed socket; missing warning; `fail_open=True`; `get_trace → None`; ignored env var; escalated allow; over-matching `_`; wedged second intervention; zero drop counter; reset countdown).

### Fix Checking

**Goal:** Verify that for all inputs where `isBugCondition` holds, the fixed function satisfies the property.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT property(result)
END FOR
```

Concretely, one fix-checking test (or Hypothesis property) per defect, asserting Properties 1, 3, 5, 6, 8, 10, 12, 14, 16, 18, 20 respectively. Hypothesis is the primary tool for Defects 1, 8, 14 (generate rule sets / action strings), and 6 (generate env var value sets). Defects 2, 3, 5, 9, 11 use targeted unit/integration tests because their input domains are structural rather than value-generated.

### Preservation Checking

**Goal:** Verify that for all inputs where `isBugCondition` does NOT hold, the fixed function produces the same result as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) == fixedFunction(input)
END FOR
```

**Testing Approach:** Property-based testing is the primary tool for preservation, because it generates many inputs across the non-buggy domain and catches edge cases manual tests miss. Where the original function is being modified in place, "original behavior" is captured as an oracle from current observed behavior (golden values) before the fix.

**Test Plan and Cases (per defect):**
1. **Defect 1 (Property 2):** Hypothesis-generate policy pairs whose `block`/`allow` sets do not overlap-conflict; assert `risk_adjust`/`threshold_override`/`context_rule` override results, rule ordering, and merged thresholds match the pre-fix oracle.
2. **Defect 2/3 (Property 4):** Valid-token HTTP requests and a valid-token `/ws/live` connection succeed; exempt paths (`/api/health`, `/static`, `/favicon.ico`) unaffected; token-configured startup emits no unauthenticated warning.
3. **Defect 4 (Property 7):** `OstiariConfig(fail_open=True)` with a failing stage still yields `allow`.
4. **Defect 5 (Property 9):** All `TraceEntry` fields round-trip through `get_trace`/`get_traces` unchanged (Hypothesis-generated traces).
5. **Defect 6 (Property 11):** Explicit-arg, YAML, overrides, and nested `__` env decoding produce identical config to pre-fix; legacy `AGENTGUARD_*`-only env applies with a deprecation warning.
6. **Defect 7 (Property 13):** Hypothesis-generate actions with `block`/`evaluate` decisions; assert final tier and score match the pre-fix oracle.
7. **Defect 8 (Property 15):** Hypothesis-generate glob (`*`/`?`) and plain (no-wildcard) patterns; assert `get_traces` result sets match the pre-fix oracle.
8. **Defect 9 (Property 17):** Callbacks returning approve/deny within timeout still yield the correct outcome; timeout still raises `ActionInterventionTimeout`.
9. **Defect 10 (Property 19):** Within-capacity trace sequences record and persist in order; `dropped_count` stays 0; no new blocking.
10. **Defect 11 (Property 21):** A breaker whose recovery has genuinely elapsed in the running process still transitions open → half_open; injected-clock tests confirm the elapsed computation matches for both monotonic and wallclock clocks.

### Unit Tests

- **Defect 1:** merge of 3 files with mixed block/allow/other; duplicate-block dedup; empty-override no-op.
- **Defect 2:** `/ws/live` reject on missing/invalid token; accept on valid token; no-token mode accepts.
- **Defect 3:** warning emitted when no token; CLI refusal on non-loopback + no token; `--allow-unauthenticated` overrides refusal.
- **Defect 4:** `OstiariConfig()` default; scaffold content; explicit `fail_open=True` preserved.
- **Defect 5:** `trace_id` equality on allow and (via recorded trace) block paths.
- **Defect 6:** each `OSTIARI_*` variable takes effect; legacy prefix deprecation warning; precedence when both set.
- **Defect 7:** allow short-circuit skips anomaly/gateway (assert via spy/mock that detector not called); block/evaluate still call them.
- **Defect 8:** literal `%`/`_` matched literally; glob `*`/`?` still wildcard.
- **Defect 9:** per-instance pool isolation; two instances independent; pool shutdown on `Guard.shutdown`.
- **Defect 10:** overflow counter increment + warning; storage-batch-failure counter increment.
- **Defect 11:** past-trip restore eligibility; clock-skew clamp; closed/None cases unchanged.

### Property-Based Tests

- **Defect 1:** generated policy-set lists → union invariant for block/allow (Property 1) and equality oracle for other types/ordering/thresholds (Property 2).
- **Defect 6:** generated `OSTIARI_*` key/value maps → every variable reflected in effective config (Property 10).
- **Defect 7:** generated actions/scores under block/evaluate → outcome equals pre-fix oracle (Property 13); under allow → always `allow` (Property 12).
- **Defect 8:** generated action strings (literal `%`/`_`, glob `*`/`?`, plain) → literal match for literals (Property 14) and oracle equality for glob/plain (Property 15).
- **Defect 5:** generated benign actions/params → returned `trace_id` always resolves via `get_trace` (Property 8).
- **Defect 11:** generated persisted trip ages and `recovery_after_seconds` under an injected clock → restored eligibility matches real elapsed time (Property 20) and genuine recovery still transitions (Property 21).

### Integration Tests

- **Dashboard (Defects 2, 3, 6):** full app via test client — token-protected HTTP + WebSocket, unauthenticated warning on startup, `OSTIARI_*` env wiring end to end.
- **Guard pipeline (Defects 4, 5, 7):** end-to-end `validate()` covering fail-closed default on injected stage error, `trace_id` round-trip to storage, and authoritative allow with an active anomaly detector.
- **Breaker restart (Defect 11):** persist an open breaker via storage, construct a fresh `Guard`/`CircuitBreaker`, `start()` → `restore_state()`, and confirm recovery timing across the (simulated) restart.
- **Interventions (Defect 9):** concurrent interventions with one hanging callback through the gateway, asserting independent progress.
