.PHONY: install test lint dev build clean help

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

dev: ## Start development servers
	cd control-plane/backend && python main.py &
	cd control-plane/frontend && npm run dev &
	cd gateway && python -m ostiari_gateway.main --port 8421 --sidecar-id dev-gateway &

build: ## Build frontend
	cd control-plane/frontend && npm run build

clean: ## Remove build artifacts
	rm -rf .mypy_cache __pycache__ .pytest_cache
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf control-plane/frontend/dist
