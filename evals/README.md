# Ostiari model evaluations

This directory contains the repository's offline model-quality harness, built
with Inspect AI. It calls Ostiari's governed OpenAI-compatible endpoint, keeps
candidate-model runs reproducible, and stores detailed local logs for
comparison.

The harness deliberately complements rather than replaces Ostiari's existing
features:

- the Sandbox checks live end-to-end behavior;
- A/B experiments measure live request, token, and cost outcomes;
- the Locust profile measures gateway performance;
- these Inspect tasks score model behavior against fixed datasets.

## Suites

| Task | Scoring | Purpose |
|---|---|---|
| `ostiari_contract` | Deterministic JSON contract | Tool selection, required arguments, clarification, and destructive-action refusal |
| `ostiari_quality` | Independent LLM judge | Reasoning, coding, groundedness, summarization, decisions, and epistemic honesty |
| `ostiari_safety` | Independent LLM judge | Destructive actions, secrets, injection, privacy, false execution claims, and ambiguity |

Every sample has `category` metadata. Reports therefore include both aggregate
accuracy and category-level metrics.

## Setup

Use Python 3.11 or newer for the complete platform:

```bash
uv sync --project evals --locked
cp evals/.env.example evals/.env
```

The evaluation dependencies are deliberately locked in `evals/uv.lock`
instead of the root project. Inspect AI includes AWS SDK dependencies of its
own; isolation prevents those constraints from changing Ostiari's production
runtime lock.

Run an Ostiari gateway with its LLM module and provider routes configured at
`127.0.0.1:8421`. The default model URI,
`openai-api/ostiari/claude-sonnet-4-6`, makes Inspect send
`claude-sonnet-4-6` to:

```text
http://127.0.0.1:8421/v1/chat/completions
```

The solver adds `X-Agent-Id`, `X-Framework`, `X-Session-Id`, `X-Plan`, and
`X-Step` headers so requests remain attributable in Ostiari.

## Run

List the available tasks:

```bash
make eval-list
```

Run the deterministic contract suite:

```bash
make eval-contract
```

Run judged quality and safety suites. An explicit independent grader is
required:

```bash
make eval-quality \
  EVAL_MODEL=openai-api/ostiari/claude-sonnet-4-6 \
  EVAL_GRADER_MODEL=anthropic/claude-sonnet-4-6

make eval-safety \
  EVAL_MODEL=openai-api/ostiari/claude-sonnet-4-6 \
  EVAL_GRADER_MODEL=anthropic/claude-sonnet-4-6
```

Any Inspect-supported provider can be used for the grader. Configure that
provider's credential in `evals/.env` or your shell. Do not use the candidate
model as its own judge.

Pass additional Inspect flags through `EVAL_FLAGS`, for example:

```bash
make eval-quality EVAL_GRADER_MODEL=anthropic/claude-sonnet-4-6 \
  EVAL_FLAGS="--epochs 3 --max-connections 4"
```

To compare candidates, run the same suite once per explicit model:

```bash
make eval-contract EVAL_MODEL=openai-api/ostiari/claude-sonnet-4-6
make eval-contract EVAL_MODEL=openai-api/ostiari/claude-haiku-4-5
make eval-report EVAL_REPORT_FLAGS="--all"
```

The OpenAI-compatible endpoint intentionally evaluates the requested model.
Ostiari's native A/B selector applies to `/invoke` and `/v1/messages`, not
`/v1/chat/completions`; use A/B only after the fixed-dataset comparison passes.

## Regression gates

Summarize the newest log:

```bash
make eval-report
```

Fail when aggregate accuracy is below a threshold:

```bash
make eval-report EVAL_REPORT_FLAGS="--minimum-accuracy 0.80"
```

Inspect logs are written to `evals/logs/` and excluded from Git. They can
contain prompts, responses, model metadata, and judge explanations. Use
synthetic or approved test data and do not commit the logs.

## Adding a case

Add one JSON Lines record under `evals/datasets/` with:

- a stable, unique `id`;
- the model `input`;
- a `target` contract or narrative grading criterion;
- `metadata.category`.

Contract targets are JSON strings interpreted as subset contracts. Literal
values match exactly. Supported operators are:

- `{"$contains": ["term", "other"]}` for case-insensitive required text;
- `{"$one_of": ["a", "b"]}`;
- `{"$regex": "pattern"}`;
- `{"$type": "string|number|boolean|null|object|array"}`.
