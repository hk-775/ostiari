.PHONY: install test lint dev demo demo-full clean-start build clean help

# LLM credentials for the Sandbox chat (/invoke). Path is resolved from the
# gateway/ directory (recipes cd there first). Override with an absolute path:
#   make demo-full LLM_ENV=/abs/path/to/.env
# If the file is missing the gateway still starts; only the chat needs keys.
LLM_ENV ?= ../../AxonLLM/.env
# Loads LLM_ENV into the environment for the current recipe line (no-op if absent).
LOAD_LLM_ENV = set -a; [ -f "$(LLM_ENV)" ] && . "$(LLM_ENV)"; set +a;

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies
	pip install -e ".[dev]"
	pip install -e gateway/
	cd control-plane/frontend && npm install

test: ## Run all tests
	pytest tests/ -v
	cd gateway && PYTHONPATH=. pytest tests/ -v
	cd control-plane/backend && PYTHONPATH=. pytest tests/ -v

lint: ## Run linters
	ruff check src/ gateway/ control-plane/backend/control_plane/ control-plane/backend/tests/
	mypy src/ostiari
	cd control-plane/frontend && npx tsc --noEmit --skipLibCheck

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
