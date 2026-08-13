# Stress-testing the Ostiari gateway

Stress testing answers "how does it behave under load and at its limits" —
throughput, latency percentiles, saturation, and graceful degradation. (That's
distinct from the adversarial pass in [`docs/adversarial-review.md`](../../docs/adversarial-review.md),
which asks "can the rules be broken.")

## Reference baseline

Measured with the harness below on **Apple M4 Pro (14 cores), Python 3.12**,
a **single gateway / single uvicorn worker** (= one core), rate limiter off,
in-process state. ApacheBench, 6–8k requests per run, **zero errors** at every
level. These are a per-instance floor to compare against, not a target — your
hardware, policy complexity, and tool latency will move them.

Per-endpoint throughput at concurrency 50:

| Endpoint | Exercises | req/s | p50 | p95 | p99 |
|---|---|--:|--:|--:|--:|
| `GET /tools` | catalog read | 4,538 | 11 ms | 14 ms | 21 ms |
| `GET /health` | heartbeat | 4,472 | 11 ms | 12 ms | 21 ms |
| `POST /validate` (allow) | full guard/policy engine, no proxy | 3,542 | 13 ms | 20 ms | 26 ms |
| `POST /validate` (block) | policy + block-glob match | 3,553 | 14 ms | 19 ms | 25 ms |
| `POST /tool/{action}` | full gate chain + real downstream | 3,033 | 16 ms | 22 ms | 28 ms |

Concurrency sweep on `/validate` (finding the knee):

| Concurrency | req/s | p50 | p95 | p99 |
|--:|--:|--:|--:|--:|
| 1 | 1,638 | 1 ms | 1 ms | 1 ms |
| 10 | 3,612 | 3 ms | 3 ms | 6 ms |
| 50 | 3,502 | 14 ms | 20 ms | 27 ms |
| 100 | 3,511 | 27 ms | 41 ms | 45 ms |
| 200 | 3,472 | 38 ms | 67 ms | 132 ms |

**Reading it:**
- **~3,500 req/s per worker** for governed calls; throughput flatlines at
  **c≈10** (the knee), after which added concurrency only grows queue latency.
- **Governance overhead is small** — single-request latency is ~1 ms (c=1), and
  allow vs. block are identical (blocking is not a slow path).
- **Proxy tax ≈ 9%** — `/tool` (3,033) vs `/validate` (3,542) is the extra
  downstream hop. The demo tool alone did 9,256 req/s at 5 ms, so Ostiari adds
  ~11 ms p50 of governance+proxy and is the binding constraint, not the backend.
- Scales roughly linearly with workers/replicas (CPU-bound async). On a 14-core
  box, ~8–10 workers ≈ 25k–30k req/s per node — **but** quota/budget/rate-limit
  state is per-process, so multi-worker/replica needs Redis to keep limits
  fleet-wide (see Scaling notes).
- **LLM paths** (`/v1/chat/completions`, `/v1/messages`, `/invoke`) are bounded
  by upstream model latency, not Ostiari; not represented above.

## What to test, and why

| Endpoint | What it measures | Downstream dependency |
|---|---|---|
| `POST /validate` | **Ostiari's own overhead** — the full guard/policy engine, no proxying | none (cleanest signal) |
| `GET /health` | Heartbeat/health-poll capacity | none |
| `GET /tools` | Catalog reads | none |
| `POST /tool/{action}` | Full gate chain **+ real downstream call** | the tool's endpoint |
| `POST /v1/chat/completions`, `/v1/messages`, `/invoke` | LLM paths incl. the budget-reservation logic | a live model provider |

Start with `/validate` — it isolates Ostiari from network noise, so a
regression there is unambiguous. Layer in `/tool` and the LLM paths once you
have a baseline.

## Prerequisites

- `locust` — scenario-based load with a live web UI and percentiles
  (`pip install locust`).
- `ab` (ApacheBench) — quick single-endpoint baseline (ships with macOS; on
  Debian/Ubuntu it's `apache2-utils`).

## Step 1 — start a gateway to test

For a clean overhead measurement, run one gateway pointed at nothing (no
control plane, so no heartbeat noise), with a tool + policy loaded:

```bash
cd gateway
PYTHONPATH=. ../.venv/bin/python -m ostiari_gateway.main --port 8421 --sidecar-id loadtest &
# register an allowed tool + a block policy so both paths exist
curl -s -X POST http://localhost:8421/config/tools -H 'Content-Type: application/json' \
  -d '{"tools":[{"name":"web_search","endpoint":"http://localhost:9300/web_search","method":"POST"}]}'
curl -s -X POST http://localhost:8421/config/policy -H 'Content-Type: application/json' \
  -d '{"block":["*.delete","*.drop"],"thresholds":{"global":{"allow_max":30,"intervene_max":70}}}'
```

To stress the **full demo topology** instead (4 gateways + demo tools), run
`make demo-full` and point the load at `:8421`.

## Step 2 — quick baseline with `ab`

```bash
# 10k requests, 100 concurrent, against the pure-overhead endpoint
ab -n 10000 -c 100 -p /tmp/v.json -T application/json \
   -H 'X-Agent-Id: load-1' http://localhost:8421/validate
# where /tmp/v.json = {"action":"web_search","params":{"q":"x"}}
```

Read: **Requests per second**, **Time per request**, and the latency
percentile table (look at p95/p99, not the mean).

Two macOS gotchas:
- Use `127.0.0.1`, **not** `localhost` — `ab` fails with
  `apr_socket_connect(): Invalid argument` on the IPv6 `localhost` here.
- `ab` reports "Failed requests" for **response-length variance**. Ostiari's
  `/validate` body includes a per-call `score`/`tier`, so lengths differ and
  every request is counted "failed" even though they're all `200`. Confirm with
  the `Non-2xx responses` line (absent = all 2xx). A quick baseline on this box:
  **~2700 req/s**, p50 16 ms / p95 25 ms / p99 37 ms on one uvicorn worker.

## Step 3 — scenario load with Locust

```bash
# Headless: ramp to 200 concurrent users at 50/s, hold 2 minutes
locust -f gateway/loadtest/locustfile.py --host http://localhost:8421 \
       --headless -u 200 -r 50 -t 2m

# Or interactive UI (tune users live, watch charts):
locust -f gateway/loadtest/locustfile.py --host http://localhost:8421
#   → http://localhost:8089
```

The profile weights `/validate` heaviest, includes an allow and a block case,
and a slice of real `/tool` proxying. Edit the weights/tool names in
`locustfile.py` to match your gateway's catalog.

## Step 4 — find the knee (saturation)

Ramp concurrency in steps (50 → 100 → 200 → 400 → 800) and plot RPS vs.
latency. The **knee** — where RPS flattens but p99 latency climbs — is your
practical capacity per gateway instance. Beyond it, requests queue.

## What to watch while it runs

- **Latency percentiles.** p50 is marketing; p99 is the truth. A rising p99
  with flat throughput = saturation.
- **Error rate.** 5xx under load = something's breaking (upstream timeouts,
  exhausted connections). 429 = the rate limiter firing (see below). 402 on
  `/tool` = payment gate; 403 = policy/agent-auth.
- **Gateway process:** `top -pid <pid>` (or `ps -o %cpu,rss`) — a single
  uvicorn worker is one core; RSS should plateau, not climb (a climbing RSS
  under steady load hints at a leak — the guard's per-session/breaker state or
  trace buffers are the places to check).
- **Event-loop stalls.** The gate chain is async; any accidental blocking call
  shows up as latency that grows with concurrency even when CPU isn't maxed.

## Testing the safety limits themselves

Stress the guards, not just the happy path:

- **Rate limiter** (off by default). Restart with
  `OSTIARI_GATEWAY_RATE_LIMIT_RPM=600` and confirm a single agent's excess
  requests get `429 Retry-After` while others aren't affected (it's keyed by
  `X-Agent-Id`, falling back to client IP when that header is absent). Note it's
  **per-process** by default — set `OSTIARI_REDIS_URL` to share the sliding
  window across replicas (see Scaling notes).
- **Body-size limit.** `OSTIARI_MAX_BODY_BYTES` (default 10 MiB) — POST a body
  over the cap and confirm `413`.
- **Budget under concurrency.** Configure a small `budget_limit_usd`, fire
  concurrent LLM calls, and confirm the reserve-then-settle logic (added in the
  hardening pass) holds the ceiling instead of overshooting.

## Scaling notes (from the deploy docs)

Quota, budget, rate-limit, and wallet state are **per gateway process** by
default. A horizontally scaled fleet (k8s `gateway-shared`, ECS with >1 task)
therefore multiplies the effective limits by the replica count. Set
`OSTIARI_REDIS_URL` (or `REDIS_ENDPOINT`) and
`gateway/ostiari_gateway/shared_store.py` moves those counters into Redis, each
mutation a single atomic Lua script — so the limits hold fleet-wide. It's
optional only in development, where unreachable Redis degrades to per-process
limits. Production requires Redis, fails shared enforcement closed during an
outage, and reports `/ready` as unavailable. Load-test a **single** instance to
get true per-node capacity, then size the fleet and repeat with shared Redis.

## Don't stress a shared/prod environment

Run this against a local or dedicated test gateway. Pointing a load generator
at production (or a shared demo) is itself a denial-of-service.
