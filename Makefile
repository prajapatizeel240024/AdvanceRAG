SHELL := /bin/bash
VENV  := .venv
PY    := $(VENV)/bin/python
PIP   := uv pip
DB    := travel_rag
export PYTHONPATH := backend

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

# -- setup -------------------------------------------------------------------

.PHONY: setup
setup: ## Create the venv, install backend + frontend deps
	@command -v uv >/dev/null || { echo "uv not found: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	uv venv --python 3.14 $(VENV)
	VIRTUAL_ENV=$(VENV) $(PIP) install -r requirements.txt
	cd web && pnpm install

.PHONY: db-up
db-up: ## Start Postgres and make sure pgvector is available
	@brew services start postgresql@16 >/dev/null 2>&1 || true
	@until pg_isready -q; do sleep 1; done
	@psql -d postgres -tAc "SELECT 1" >/dev/null
	@echo "postgres ready"

.PHONY: migrate
migrate: ## Apply migrations (keeps existing data)
	./scripts/migrate.sh

.PHONY: db-reset
db-reset: ## Drop and rebuild the database from migrations
	./scripts/migrate.sh --reset

.PHONY: seed
seed: ## Seed the default tenant/user and register the YAML config bundle
	$(PY) scripts/seed.py

# -- ingestion ---------------------------------------------------------------

.PHONY: estimate
estimate: ## Price ingesting the corpus WITHOUT spending anything (no API keys needed)
	$(PY) scripts/ingest.py --estimate-only

.PHONY: ingest
ingest: ## Estimate, then ingest the corpus (needs GEMINI_API_KEY, and ANTHROPIC_API_KEY if contextualising)
	$(PY) scripts/ingest.py

# -- running -----------------------------------------------------------------

.PHONY: api
api: ## Run the FastAPI backend on :8000
	$(VENV)/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

.PHONY: web
web: ## Run the Vite dev server on :5173
	cd web && pnpm dev

.PHONY: dev
dev: ## Run backend and frontend together
	@$(MAKE) -j2 api web

# -- quality -----------------------------------------------------------------

.PHONY: test
test: ## Run the Python tests and the SQL assertions
	$(VENV)/bin/pytest -q tests
	psql -d $(DB) -v ON_ERROR_STOP=1 -f scripts/verify_rls.sql
	psql -d $(DB) -v ON_ERROR_STOP=1 -f scripts/verify_retrieval.sql
	psql -d $(DB) -v ON_ERROR_STOP=1 -f scripts/verify_cost.sql

.PHONY: eval
eval: ## Run the 30-question evaluation set against the live system
	$(PY) scripts/evaluate.py

.PHONY: typecheck
typecheck: ## Typecheck the frontend
	cd web && npx tsc --noEmit

.PHONY: clean
clean: ## Remove build artefacts
	rm -rf web/dist .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
