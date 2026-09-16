.PHONY: install test lint eval-list eval-contract eval-quality eval-safety eval-report dev demo demo-full clean-start build clean help

# LLM credentials for the Sandbox chat (/invoke). Override with an absolute path:
#   make demo-full LLM_ENV=/abs/path/to/.env
# If the file is missing the gateway still starts; only the chat needs keys.
LLM_ENV ?= $(CURDIR)/.env
AXON_ROOT ?= $(CURDIR)/vendor/axonllm
# Loads LLM_ENV into the environment for the current recipe line (no-op if absent).
LOAD_LLM_ENV = set -a; [ -f "$(LLM_ENV)" ] && . "$(LLM_ENV)"; set +a;

# Offline model evaluation defaults. evals/.env is ignored by Git and may hold
# provider credentials; the candidate model itself is always called through
# Ostiari's OpenAI-compatible endpoint.
EVAL_ENV ?= $(CURDIR)/evals/.env
EVAL_PROJECT ?= $(CURDIR)/evals
EVAL_MODEL ?= openai-api/ostiari/claude-sonnet-4-6
EVAL_GRADER_MODEL ?=
EVAL_FLAGS ?=
EVAL_REPORT_FLAGS ?=
LOAD_EVAL_ENV = set -a; [ -f "$(EVAL_ENV)" ] && . "$(EVAL_ENV)"; set +a;

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies
	pip install -e ".[all,dev]"
	pip install -e "$(AXON_ROOT)[server]"
	pip install -e "gateway[payments,redis]"
	pip install -e "control-plane/backend[aws,dev,otlp]"
	cd control-plane/frontend && npm ci

test: ## Run all tests
	pytest tests/ -v
	cd gateway && OSTIARI_AXON_ROOT="$(AXON_ROOT)" PYTHONPATH=. pytest tests/ -v
	cd control-plane/backend && PYTHONPATH=. pytest tests/ -v

lint: ## Run linters
	ruff check src/ gateway/ evals/ control-plane/backend/control_plane/ control-plane/backend/tests/
	mypy src/ostiari
	cd control-plane/frontend && npx tsc --noEmit --skipLibCheck

eval-list: ## List offline model-evaluation tasks
	uv run --project "$(EVAL_PROJECT)" --locked inspect list tasks evals

eval-contract: ## Run deterministic model contract evaluations through Ostiari
	$(LOAD_EVAL_ENV) uv run --project "$(EVAL_PROJECT)" --locked inspect eval evals/tasks.py@ostiari_contract --model "$${OSTIARI_EVAL_MODEL:-$(EVAL_MODEL)}" --log-dir evals/logs $(EVAL_FLAGS)

eval-quality: ## Run judged model-quality evaluations through Ostiari
	$(LOAD_EVAL_ENV) grader="$${OSTIARI_EVAL_GRADER_MODEL:-$(EVAL_GRADER_MODEL)}"; if [ -z "$$grader" ]; then echo "Set EVAL_GRADER_MODEL or OSTIARI_EVAL_GRADER_MODEL to an independent judge model."; exit 2; fi; uv run --project "$(EVAL_PROJECT)" --locked inspect eval evals/tasks.py@ostiari_quality --model "$${OSTIARI_EVAL_MODEL:-$(EVAL_MODEL)}" -T "grader_model=$$grader" --log-dir evals/logs $(EVAL_FLAGS)

eval-safety: ## Run judged model-safety evaluations through Ostiari
	$(LOAD_EVAL_ENV) grader="$${OSTIARI_EVAL_GRADER_MODEL:-$(EVAL_GRADER_MODEL)}"; if [ -z "$$grader" ]; then echo "Set EVAL_GRADER_MODEL or OSTIARI_EVAL_GRADER_MODEL to an independent judge model."; exit 2; fi; uv run --project "$(EVAL_PROJECT)" --locked inspect eval evals/tasks.py@ostiari_safety --model "$${OSTIARI_EVAL_MODEL:-$(EVAL_MODEL)}" -T "grader_model=$$grader" --log-dir evals/logs $(EVAL_FLAGS)

eval-report: ## Summarize Inspect logs and optionally enforce an accuracy floor
	uv run --project "$(EVAL_PROJECT)" --locked python -m ostiari_eval.report --log-dir evals/logs $(EVAL_REPORT_FLAGS)

demo: ## Demo mode — frontend only with mock data (http://localhost:9000)
	cd control-plane/frontend && npm run dev

dev: ## Start backend + frontend + primary gateway (seeded demo data)
	cd control-plane/backend && OSTIARI_DISCOVERY_MOCK=1 python main.py &
	cd gateway && python demo_tools_server.py &
	sleep 3 && cd gateway && $(LOAD_LLM_ENV) python -m ostiari_gateway.main --port 8421 --sidecar-id crm-agent --control-plane http://localhost:8400 --config llm-gateway-config.yaml &
	sleep 6 && cd gateway && python register_demo_tools.py && python register_demo_mcp.py && python register_demo_providers.py &
	cd control-plane/frontend && npm run dev &

# NOTE: sidecar IDs and ports MUST match the gateway records seeded in the
# control plane DB (crm-agent:8421, ops-agent:8422, devops-agent:8424,
# analytics-agent:8425). On registration the control plane pushes each gateway
# its tools/policy by ID — a mismatched ID means no tools and no traces.
# The crm-agent gateway also loads llm-gateway-config.yaml (enables the LLM
# module + credentials) so the Sandbox chat's /invoke endpoint works, and
# register_demo_tools.py points its tools at demo_tools_server.py (canned
# web_search/db_query/github.* responses) so chat tool calls return real data.
# register_demo_providers.py seeds or updates the durable Providers catalog from
# $(LLM_ENV). Re-running the demo refreshes credentials without relying on
# process memory.
demo-full: ## Full demo — all gateways, A2A agent, control plane (seeded demo data)
	cd control-plane/backend && OSTIARI_DISCOVERY_MOCK=1 python main.py &
	cd gateway && python demo_tools_server.py &
	sleep 3 && cd gateway && $(LOAD_LLM_ENV) python -m ostiari_gateway.main --port 8421 --sidecar-id crm-agent --control-plane http://localhost:8400 --config llm-gateway-config.yaml &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8422 --sidecar-id ops-agent --control-plane http://localhost:8400 &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8424 --sidecar-id devops-agent --control-plane http://localhost:8400 &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8425 --sidecar-id analytics-agent --control-plane http://localhost:8400 &
	sleep 3 && cd gateway && python a2a_demo_server.py &
	sleep 6 && cd gateway && python register_demo_tools.py && python register_fleet_tools.py && python register_demo_mcp.py && python register_demo_a2a.py && python register_demo_payments.py && python register_demo_providers.py &
	cd control-plane/frontend && npm run dev &

clean-start: ## Clean install — wipe demo data, start all components empty
	rm -f control-plane/data/state.json control-plane/backend/data/state.json
	rm -f control-plane/data/control_plane.db control-plane/data/control_plane.db-shm control-plane/data/control_plane.db-wal
	cd control-plane/backend && OSTIARI_NO_DEMO=1 python main.py &
	cd control-plane/frontend && npm run dev &
	cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id my-gateway --control-plane http://localhost:8400 &

build: ## Build frontend
	cd control-plane/frontend && npm run build

clean: ## Remove build artifacts
	rm -rf .mypy_cache __pycache__ .pytest_cache
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf control-plane/frontend/dist
