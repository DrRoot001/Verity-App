.DEFAULT_GOAL := help
SHELL := /bin/bash

PY := backend/.venv/bin/python
PIP := backend/.venv/bin/pip
PG_BIN := $(shell brew --prefix postgresql@16 2>/dev/null)/bin
PGDATA := .data/postgres
PGPORT := 5432

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Setup ───────────────────────────────────────────────────────────
.PHONY: setup
setup: setup-backend setup-web ## Install all dependencies

.PHONY: setup-backend
setup-backend: ## Create venv and install backend dependencies
	python3.12 -m venv backend/.venv
	$(PIP) install -q --upgrade pip
	cd backend && .venv/bin/pip install -e ".[dev]"

.PHONY: setup-web
setup-web: ## Install web dependencies
	cd web && pnpm install

# ── Local infrastructure ────────────────────────────────────────────
.PHONY: db-init
db-init: ## Initialize a local Postgres cluster
	@test -d $(PGDATA) || $(PG_BIN)/initdb -D $(PGDATA) -U postgres --encoding=UTF8 --locale=C

.PHONY: db-start
db-start: db-init ## Start local Postgres
	@$(PG_BIN)/pg_ctl -D $(PGDATA) -l .data/postgres.log -o "-p $(PGPORT)" start || true
	@until $(PG_BIN)/pg_isready -p $(PGPORT) -q; do sleep 0.5; done
	@echo "postgres ready on :$(PGPORT)"

.PHONY: db-stop
db-stop: ## Stop local Postgres
	@$(PG_BIN)/pg_ctl -D $(PGDATA) stop || true

.PHONY: db-create
db-create: db-start ## Create the verity role and database
	@$(PG_BIN)/psql -p $(PGPORT) -U postgres -d postgres -tc \
		"SELECT 1 FROM pg_roles WHERE rolname='verity'" | grep -q 1 || \
		$(PG_BIN)/psql -p $(PGPORT) -U postgres -d postgres -c \
		"CREATE ROLE verity LOGIN PASSWORD 'verity' SUPERUSER"
	@$(PG_BIN)/psql -p $(PGPORT) -U postgres -d postgres -tc \
		"SELECT 1 FROM pg_database WHERE datname='verity'" | grep -q 1 || \
		$(PG_BIN)/createdb -p $(PGPORT) -U postgres -O verity verity
	@$(PG_BIN)/psql -p $(PGPORT) -U postgres -d verity -c \
		"CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
	@echo "database verity ready"

.PHONY: redis-start
redis-start: ## Start Redis if not already running
	@redis-cli ping >/dev/null 2>&1 || (redis-server --daemonize yes && sleep 1)
	@redis-cli ping

.PHONY: infra
infra: db-create redis-start ## Bring up all local infrastructure

# ── Migrations ──────────────────────────────────────────────────────
.PHONY: migrate
migrate: ## Apply all migrations
	cd backend && .venv/bin/alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: seed
seed: ## Load operational defaults (never creates user accounts)
	cd backend && .venv/bin/python -m verity.apps.cli seed

.PHONY: bootstrap-admin
bootstrap-admin: ## Create first admin: make bootstrap-admin email=... password=...
	cd backend && .venv/bin/python -m verity.apps.cli bootstrap-admin --email "$(email)" --password "$(password)"

# ── Run ─────────────────────────────────────────────────────────────
.PHONY: api
api: ## Run the API with reload
	cd backend && .venv/bin/uvicorn verity.apps.api.app:app --reload --host 127.0.0.1 --port 8000

.PHONY: web
web: ## Run the web app
	cd web && pnpm dev

# ── Desktop (PRD §17, Phase 12) ─────────────────────────────────────
.PHONY: desktop
desktop: ## Run the desktop assistant against the local API
	cd desktop/src-tauri && cargo run --release

.PHONY: desktop-build
desktop-build: ## Build the desktop installer (.dmg + .app)
	cd desktop/src-tauri && cargo tauri build

.PHONY: desktop-test
desktop-test: ## Run the desktop unit tests
	cd desktop/src-tauri && cargo test

# ── Verification ────────────────────────────────────────────────────
.PHONY: lint
lint: ## Lint backend and web
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .
	cd web && pnpm lint

.PHONY: fmt
fmt: ## Auto-format
	cd backend && .venv/bin/ruff check --fix . && .venv/bin/ruff format .
	cd web && pnpm format

.PHONY: types
types: ## Type-check backend and web
	cd backend && .venv/bin/mypy verity
	cd web && pnpm typecheck

.PHONY: arch
arch: ## Enforce architecture boundaries (PRD §22.2)
	cd backend && .venv/bin/lint-imports

.PHONY: test
test: ## Run backend tests
	cd backend && .venv/bin/pytest

.PHONY: test-web
test-web: ## Run web tests
	cd web && pnpm test

.PHONY: verify
verify: lint types arch test test-web ## Full verification gate
	@echo "✓ all checks passed"
