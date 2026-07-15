.PHONY: install test lint dev demo demo-full clean-start build clean help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies
	pip install -e ".[dev]"
	pip install -e gateway/
	cd control-plane/frontend && npm install

test: ## Run all tests
	pytest tests/ -v
	cd control-plane/frontend && npx vitest run 2>/dev/null || true

lint: ## Run linters
	ruff check src/ gateway/
	cd control-plane/frontend && npx tsc --noEmit --skipLibCheck

demo: ## Demo mode — frontend only with mock data (http://localhost:9000)
	cd control-plane/frontend && npm run dev

dev: ## Start backend + frontend + primary gateway (seeded demo data)
	cd control-plane/backend && python main.py &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id crm-agent --control-plane http://localhost:8400 &
	cd control-plane/frontend && npm run dev &

# NOTE: sidecar IDs and ports MUST match the gateway records seeded in the
# control plane DB (crm-agent:8421, ops-agent:8422, devops-agent:8424,
# analytics-agent:8425). On registration the control plane pushes each gateway
# its tools/policy by ID — a mismatched ID means no tools and no traces.
demo-full: ## Full demo — all gateways, A2A agent, control plane (seeded demo data)
	cd control-plane/backend && python main.py &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id crm-agent --control-plane http://localhost:8400 &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8422 --sidecar-id ops-agent --control-plane http://localhost:8400 &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8424 --sidecar-id devops-agent --control-plane http://localhost:8400 &
	sleep 3 && cd gateway && python -m ostiari_gateway.main --port 8425 --sidecar-id analytics-agent --control-plane http://localhost:8400 &
	sleep 3 && cd gateway && python a2a_demo_server.py &
	cd control-plane/frontend && npm run dev &

clean-start: ## Clean install — wipe demo data, start all components empty
	rm -f control-plane/backend/data/state.json
	rm -f ostiari.db ostiari.db.lock
	cd control-plane/backend && OSTIARI_NO_DEMO=1 python main.py &
	cd control-plane/frontend && npm run dev &
	cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id my-gateway --control-plane http://localhost:8400 &

build: ## Build frontend
	cd control-plane/frontend && npm run build

clean: ## Remove build artifacts
	rm -rf .mypy_cache __pycache__ .pytest_cache
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf control-plane/frontend/dist
