# Bugfix Requirements Document

## Introduction

This bugfix addresses a cluster of verified defects found during a review of Ostiari, the runtime safety/guardrail layer for AI agents. The defects span three severity bands:

- **Critical (safety/security)** — defects that silently weaken or bypass the guardrail: block rules dropped during policy merge, an unauthenticated WebSocket trace stream, a dashboard that runs fully open by default, and a fail-open default that allows actions on any pipeline error.
- **High (correctness)** — defects that make the guardrail return wrong or contradictory results: a returned `trace_id` that never matches the stored trace, a stale `AGENTGUARD_` env-var prefix that silently ignores user config, a policy `allow` decision that does not guarantee an allow outcome, and inert `LIKE` escaping in trace queries.
- **Medium (reliability)** — defects that degrade robustness under load or across restarts: a shared single-worker intervention thread pool, silent trace drops with no counter/warning, and a circuit-breaker recovery timer that resets on every restart.

The common thread is that each defect fails silently: the system keeps running and appears healthy while the safety guarantee it advertises is quietly violated. Because Ostiari's entire value is being a trustworthy guardrail, these are treated as safety regressions rather than cosmetic bugs.

Scope decisions confirmed with the requester:
- All eleven defects are in scope, including the medium-reliability items (defects 9–11).
- Defect 4 intentionally changes the default value of `fail_open` from `True` (fail-open) to `False` (fail-closed). This is an accepted **breaking change**: after upgrade, a pipeline error results in the action being blocked/intervened rather than allowed.

## Bug Analysis

### Current Behavior (Defect)

The following describes what the system does today when each defective condition is triggered.

1.1 WHEN multiple policy files are loaded via `PolicyEngine._load_from_paths` and a later file defines any `block` rule THEN the system discards all `block` rules from earlier files, because `PolicyEngine.merge` computes `merged_block = override_block if override_block else base_block` (replace instead of union).

1.2 WHEN a WebSocket client connects to the dashboard `/ws/live` trace stream and a token is configured THEN the system serves the real-time trace stream without checking the token, because `TokenAuthMiddleware` extends Starlette `BaseHTTPMiddleware` which never runs for WebSocket scope, making the websocket branch in `dispatch()` dead code.

1.3 WHEN the dashboard is created via `create_app` without a token (no `token` argument and no `AGENTGUARD_TOKEN`) THEN the system exposes the trace viewer, policy editor, and intervention-approval endpoints with no authentication, and emits no warning even when bound to a non-loopback host such as `0.0.0.0`.

1.4 WHEN a `Guard`/`OstiariConfig` is constructed without explicitly setting `fail_open`, and any stage of the evaluation pipeline raises an error THEN the system allows the action, because `fail_open` defaults to `True` in `OstiariConfig` and the `ostiari init` scaffold writes `fail_open: true`.

1.5 WHEN `Guard.validate` completes and returns a `ValidationResult` THEN the returned `trace_id` never matches the persisted trace, because `_record_trace` generates one `uuid` for the stored `TraceEntry` while the `ValidationResult` is built with a different `uuid`.

1.6 WHEN a user configures Ostiari through environment variables using an `OSTIARI_*` prefix (e.g. `OSTIARI_CONFIG`, `OSTIARI_TOKEN`, `OSTIARI_REDIS_URL`, `OSTIARI_DB`, `OSTIARI_HOST`) THEN the system silently ignores them, because `config.py` and `dashboard/app.py` still read the legacy `AGENTGUARD_*` prefix from before the project rename.

1.7 WHEN a policy evaluation returns `decision == "allow"` for an action THEN the system still runs anomaly detection and lets the gateway compute a risk tier, so an allowed action can be pushed to `intervene` or `block` by anomaly/risk signals; the pipeline only short-circuits on `decision == "block"`.

1.8 WHEN a trace query filter contains a literal `%` or `_` in the action pattern THEN the escaping applied by `_glob_to_sql` is inert, because the SQL `action LIKE ?` clause in `get_traces` has no `ESCAPE '\'` clause, so the backslash escapes are treated as literal characters and the wildcards still match.

1.9 WHEN an intervention callback hangs THEN all interventions process-wide are wedged, because `gateway.py` uses a single module-level `ThreadPoolExecutor(max_workers=1)`; timed-out callbacks continue running in the pool and are not cancelled.

1.10 WHEN traces are produced faster than they can be persisted THEN the system silently drops traces once the tracer `deque(maxlen=...)` is full (and storage fail-open silently drops writes), with no drop counter and no warning.

1.11 WHEN a process restarts and `CircuitBreaker.restore_state` loads a persisted open breaker THEN the system resets `tripped_at` to the current time, restarting the recovery countdown from zero on every restart instead of preserving the original trip time.

### Expected Behavior (Correct)

The following describes the correct behavior for the same conditions.

2.1 WHEN multiple policy files are loaded and one or more files define `block` rules THEN the system SHALL retain the union of all `block` rules across all merged files (block rules must never be dropped by a later file). The same union semantics SHALL apply to `allow` rules.

2.2 WHEN a WebSocket client connects to `/ws/live` and a token is configured THEN the system SHALL enforce token authentication inside the endpoint before streaming any trace data, rejecting connections with a missing or invalid token.

2.3 WHEN the dashboard is created without a token THEN the system SHALL emit a clear unauthenticated-mode warning, and SHALL warn (or refuse, per design) when bound to a non-loopback host (e.g. `0.0.0.0`) without a token, so an open trace viewer / policy editor / intervention-approval surface is never silently network-exposed.

2.4 WHEN a `Guard`/`OstiariConfig` is constructed without explicitly setting `fail_open`, and a pipeline stage raises an error THEN the system SHALL fail closed by default (block/intervene rather than allow), because `fail_open` SHALL default to `False` and the `ostiari init` scaffold SHALL write `fail_open: false`. This is an accepted breaking change.

2.5 WHEN `Guard.validate` completes and returns a `ValidationResult` THEN the returned `trace_id` SHALL equal the `trace_id` of the persisted `TraceEntry`, so the caller can look up the exact stored trace.

2.6 WHEN a user configures Ostiari through `OSTIARI_*` environment variables THEN the system SHALL read them (`OSTIARI_CONFIG`, `OSTIARI_TOKEN`, `OSTIARI_REDIS_URL`, `OSTIARI_DB`, `OSTIARI_HOST`, and the config env prefix), so the documented `OSTIARI_*` variables take effect.

2.7 WHEN a policy evaluation returns `decision == "allow"` for an action THEN the system SHALL treat it as an authoritative allow and short-circuit to an allow outcome without letting anomaly/risk signals escalate it to `intervene` or `block`, honoring the documented "Always allow read operations" behavior.

2.8 WHEN a trace query filter contains a literal `%` or `_` THEN the system SHALL match it literally, by pairing the escaped `LIKE` pattern with an `ESCAPE '\'` clause so `_glob_to_sql` escaping is effective.

2.9 WHEN an intervention callback hangs or times out THEN the system SHALL prevent one wedged callback from blocking all other interventions process-wide, and SHALL not leave timed-out callbacks silently consuming the only worker.

2.10 WHEN traces are produced faster than they can be persisted THEN the system SHALL track and surface dropped traces (a drop counter and/or a warning) rather than dropping them completely silently, for both the tracer queue and storage fail-open write drops.

2.11 WHEN a process restarts and `restore_state` loads a persisted open breaker THEN the system SHALL preserve the original trip time so the recovery countdown reflects real elapsed time, rather than resetting `tripped_at` to now on every restart.

### Unchanged Behavior (Regression Prevention)

The following existing behaviors must be preserved by the fixes.

3.1 WHEN policy files are merged and later files legitimately override `risk_adjust`, `threshold_override`, or `context_rule` entries for the same action pattern THEN the system SHALL CONTINUE TO apply the existing override/merge semantics for those non-block/non-allow rule types, and SHALL CONTINUE TO apply the existing rule ordering and threshold merge behavior.

3.2 WHEN a normal HTTP request hits the dashboard with a valid token, or hits an exempt path (`/api/health`, `/static`, `/favicon.ico`) THEN the system SHALL CONTINUE TO authorize the request exactly as before.

3.3 WHEN a token IS configured for the dashboard THEN the system SHALL CONTINUE TO enforce bearer-token authentication on HTTP routes as it does today.

3.4 WHEN `fail_open` is explicitly set to `True` by the user THEN the system SHALL CONTINUE TO allow actions on pipeline errors (the fix only changes the default, not the fail-open code path when explicitly requested).

3.5 WHEN a trace is persisted THEN the system SHALL CONTINUE TO store all existing `TraceEntry` fields and remain retrievable via `get_trace`/`get_traces` as before; only the returned `trace_id` is corrected to match.

3.6 WHEN a user continues to set legacy `AGENTGUARD_*` variables during a transition period (if backward compatibility is retained per design) THEN documented behavior SHALL be explicit; regardless, standard non-prefixed and explicit-argument configuration paths SHALL CONTINUE TO work unchanged.

3.7 WHEN a policy evaluation returns `decision == "block"` THEN the system SHALL CONTINUE TO short-circuit and block the action as it does today.

3.8 WHEN a policy evaluation returns `decision == "evaluate"` (no matching allow/block) THEN the system SHALL CONTINUE TO run anomaly detection and gateway risk scoring exactly as before.

3.9 WHEN a trace query filter contains no wildcard characters, or intentionally uses `*`/`?` glob wildcards THEN the system SHALL CONTINUE TO match actions using the existing glob-to-SQL translation.

3.10 WHEN an intervention callback returns normally within its timeout THEN the system SHALL CONTINUE TO honor its approve/deny result and existing timeout semantics.

3.11 WHEN traces are produced within queue capacity THEN the system SHALL CONTINUE TO record and persist them in order with the existing non-blocking batching behavior.

3.12 WHEN a breaker's recovery interval has genuinely elapsed THEN the system SHALL CONTINUE TO transition open → half_open and probe for recovery as it does today.

## Bug Conditions and Properties

Each defect is expressed as a bug condition `C(X)` (inputs that trigger the bug), a property `P(result)` (correct behavior for those inputs), and a preservation goal for `¬C(X)`. `F` is the original (unfixed) function and `F'` is the fixed function.

### Defect 1 — Policy merge drops block rules

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = ordered list of PolicySet files being merged
  OUTPUT: boolean
  // Bug triggers when an earlier file has a block rule AND a later file also has any block rule
  RETURN EXISTS i < j SUCH THAT hasBlockRule(X[i]) AND hasBlockRule(X[j])
END FUNCTION

// Property: Fix Checking — block rules union across files
FOR ALL X WHERE isBugCondition(X) DO
  merged ← mergeAll'(X)
  ASSERT blockRules(merged) = UNION(blockRules(X[0]), ..., blockRules(X[n]))
END FOR

// Preservation: non-block/allow merge semantics unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT mergeAll(X) = mergeAll'(X)   // for other rule types and ordering
END FOR
```

### Defect 2 — WebSocket bypasses authentication

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = incoming connection to /ws/live
  OUTPUT: boolean
  RETURN tokenConfigured() AND X.scope = "websocket"
END FUNCTION

// Property: Fix Checking — websocket auth enforced
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT (X.token = configuredToken) OR connectionRejected(X)
END FOR
```

### Defect 3 — Dashboard open by default

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = dashboard startup config
  OUTPUT: boolean
  RETURN X.token IS EMPTY
END FUNCTION

// Property: Fix Checking — warn (and guard non-loopback bind) when unauthenticated
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT unauthenticatedWarningEmitted()
  ASSERT (X.host IN {"127.0.0.1","localhost"}) OR nonLoopbackWithoutTokenIsWarnedOrRefused()
END FOR
```

### Defect 4 — fail_open default (breaking change)

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = pipeline execution where a stage raised an error
  OUTPUT: boolean
  RETURN failOpenNotExplicitlySet(X) AND pipelineErrorOccurred(X)
END FUNCTION

// Property: Fix Checking — default is fail-closed
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT outcome(F'(X)) IN {"block","intervene"}   // NOT "allow"
END FOR

// Preservation: explicit fail_open=True still fails open
FOR ALL X WHERE failOpenExplicitlyTrue(X) DO
  ASSERT outcome(F'(X)) = "allow"
END FOR
```

### Defect 5 — trace_id mismatch

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = any successful validate() call that records a trace
  OUTPUT: boolean
  RETURN traceRecorded(X)
END FUNCTION

// Property: Fix Checking — returned id resolves to stored trace
FOR ALL X WHERE isBugCondition(X) DO
  result ← validate'(X)
  ASSERT storage.get_trace(result.trace_id) IS NOT NULL
  ASSERT storage.get_trace(result.trace_id).trace_id = result.trace_id
END FOR
```

### Defect 6 — env var prefix

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = environment with OSTIARI_* variables set
  OUTPUT: boolean
  RETURN EXISTS v IN X SUCH THAT v STARTS WITH "OSTIARI_"
END FUNCTION

// Property: Fix Checking — OSTIARI_* variables take effect
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT effectiveConfig'(X) reflects each OSTIARI_* variable
END FOR
```

### Defect 7 — allow decision not honored

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = action whose policy evaluation yields decision = "allow"
  OUTPUT: boolean
  RETURN policyDecision(X) = "allow"
END FUNCTION

// Property: Fix Checking — allow is authoritative
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT finalTier(F'(X)) = "allow"   // never escalated by anomaly/risk signals
END FOR

// Preservation: block and evaluate paths unchanged
FOR ALL X WHERE policyDecision(X) IN {"block","evaluate"} DO
  ASSERT F(X) = F'(X)
END FOR
```

### Defect 8 — LIKE escaping inert

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = TraceFilters.action containing a literal '%' or '_'
  OUTPUT: boolean
  RETURN containsLiteralWildcard(X.action)   // '%' or '_' not from glob '*'/'?'

// Property: Fix Checking — literal %/_ matched literally
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT get_traces'(X) matches only actions containing the literal character
END FOR

// Preservation: glob '*'/'?' and plain patterns unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT get_traces(X) = get_traces'(X)
END FOR
```

### Defect 9 — shared single-worker intervention pool

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = set of concurrent interventions where one callback hangs
  OUTPUT: boolean
  RETURN EXISTS callback IN X SUCH THAT hangs(callback)
END FUNCTION

// Property: Fix Checking — one hang does not wedge all interventions
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT otherInterventions(X) still progress / time out independently
END FOR
```

### Defect 10 — silent trace drops

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = trace production rate exceeding persistence capacity
  OUTPUT: boolean
  RETURN queueFull(X) OR storageWriteDropped(X)
END FUNCTION

// Property: Fix Checking — drops are counted/surfaced
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT droppedCounterIncreased() OR warningEmitted()
END FOR
```

### Defect 11 — breaker recovery timer resets on restart

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = restart restoring a persisted open breaker with a prior tripped_at
  OUTPUT: boolean
  RETURN X.persistedState.state = "open" AND X.persistedState.tripped_at IS NOT NULL
END FUNCTION

// Property: Fix Checking — original trip time preserved
FOR ALL X WHERE isBugCondition(X) DO
  ASSERT restoredBreaker'(X).tripped_at reflects X.persistedState.tripped_at
         (NOT reset to now)
END FOR
```
