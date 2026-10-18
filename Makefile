# Convenience targets. Run `make help` for a list.
SHELL := /bin/bash
PY := backend/.venv/bin/python

.PHONY: help setup env db migrate seed reseed backend frontend dev test test-backend test-frontend eval lint e2e up down clean

help:            ## Show this help
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env:             ## Create .env with generated secrets (no-op if it exists)
	./scripts/setup-env.sh

setup: env       ## Install backend + frontend dependencies
	python3 -m venv backend/.venv && backend/.venv/bin/pip install -q -r backend/requirements-dev.txt
	cd frontend && npm ci

db:              ## Start only PostgreSQL (port 5433)
	docker compose up -d db

migrate:         ## Apply database migrations
	cd backend && .venv/bin/alembic upgrade head

seed:            ## Seed the catalogue (idempotent)
	cd backend && .venv/bin/python -m app.seed

reseed:          ## Restore seed prices/stock (e.g. after demo orders)
	cd backend && .venv/bin/python -m app.seed --reset

backend:         ## Run the API with auto-reload on :8000
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

frontend:        ## Run the Vite dev server on :5173
	cd frontend && npm run dev

test: test-backend test-frontend eval  ## Run all automated tests

test-backend:    ## Backend unit + integration tests (SQLite)
	cd backend && .venv/bin/pytest

test-frontend:   ## Frontend unit tests
	cd frontend && npm test

eval:            ## Run the 77-scenario evaluation suite
	$(PY) evaluation/run_eval.py --strict

lint:            ## Lint + type-check everything
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy app
	cd frontend && npm run lint && npm run typecheck

e2e:             ## Playwright tests (needs the stack running on :5173)
	cd frontend && npm run e2e

up:              ## Build and start the full Docker stack (http://localhost:8080)
	docker compose up --build -d

down:            ## Stop the Docker stack (keeps data)
	docker compose down

clean:           ## Stop the stack AND delete its database volume
	docker compose down -v
