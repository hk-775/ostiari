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

dev: ## Start backend + frontend + one gateway (seeded demo data)
	cd control-plane/backend && python main.py &
	cd control-plane/frontend && npm run dev &
	cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id dev-gateway --control-plane http://localhost:8400 &

demo-full: ## Full demo — all gateways, A2A agent, control plane (seeded demo data)
	cd control-plane/backend && python main.py &
	cd control-plane/frontend && npm run dev &
	cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id dev-gateway --control-plane http://localhost:8400 &
	cd gateway && python -m ostiari_gateway.main --port 8422 --sidecar-id ops-gateway --control-plane http://localhost:8400 &
	cd gateway && python -m ostiari_gateway.main --port 8423 --sidecar-id devops-gateway --control-plane http://localhost:8400 &
	cd gateway && python -m ostiari_gateway.main --port 8424 --sidecar-id analytics-gateway --control-plane http://localhost:8400 &
	cd gateway && python -m ostiari_gateway.main --port 8425 --sidecar-id crm-gateway --control-plane http://localhost:8400 --config example-config.yaml &
	cd gateway && python a2a_demo_server.py &

clean-start: ## Clean install — wipe demo data, start all components empty
	rm -f control-plane/backend/data/state.json
	rm -f ostiari.db ostiari.db.lock
	cd control-plane/backend && python main.py &
	cd control-plane/frontend && npm run dev &
	cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id my-gateway --control-plane http://localhost:8400 &

build: ## Build frontend
	cd control-plane/frontend && npm run build

clean: ## Remove build artifacts
	rm -rf .mypy_cache __pycache__ .pytest_cache
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf control-plane/frontend/dist
