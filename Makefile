SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTEST_UNIT := -m "not integration and not e2e"

.PHONY: help setup up down reset-db psql infra-check api web web-build openapi migrate revision downgrade lint typecheck test test-int check fmt

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install python + js dependencies and git hooks
	uv sync
	pnpm install
	pre-commit install --install-hooks

COMPOSE := docker compose -f infra/docker-compose.yml $(if $(wildcard .env),--env-file .env,)
PG_USER ?= $(or $(SENSEI_PG_USER),sensei)

up: ## Start local infra (postgres, redis, s3) and wait until healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop local infra (keeps data)
	$(COMPOSE) down

reset-db: ## Delete the local postgres volume (asks first)
	@read -p "Delete local postgres data? [y/N] " ans && [ "$$ans" = "y" ]
	$(COMPOSE) down
	docker volume rm repo-sensei_pgdata || true

psql: ## Open psql on the dev database
	$(COMPOSE) exec postgres psql -U $(PG_USER) -d sensei

infra-check: ## Verify extensions, redis and buckets
	uv run python scripts/check_infra.py

api: ## Run the API with auto-reload on :8000
	uv run uvicorn sensei_api.app:create_app --factory --reload --port 8000

web: ## Run the web app with hot reload on :3000 (proxies /api to :8000)
	pnpm --filter web dev

web-build: ## Production build of the web app
	pnpm --filter web build

openapi: ## Export the OpenAPI schema for the web client
	uv run python scripts/export_openapi.py

ALEMBIC := uv run alembic -c packages/core/alembic.ini

migrate: ## Apply all database migrations
	$(ALEMBIC) upgrade head

NEXT_REV = $(shell printf "%04d" $$(( $$(ls packages/core/migrations/versions/[0-9][0-9][0-9][0-9]_*.py | wc -l) + 1 )))

revision: ## Create a migration: make revision m="add users"
	$(ALEMBIC) revision --autogenerate --rev-id $(NEXT_REV) -m "$(m)"

downgrade: ## Roll back one migration
	$(ALEMBIC) downgrade -1

lint: ## Lint and format-check python and web
	uv run ruff check .
	uv run ruff format --check .
	pnpm fmt:check
	pnpm --filter web lint

typecheck: ## Static type checks
	uv run pyright
	pnpm --filter web typecheck

test: ## Unit tests
	uv run pytest $(PYTEST_UNIT) -q

test-int: ## Integration tests (needs `make up`)
	uv run pytest -m integration -q

check: lint typecheck test ## Lint, typecheck and unit tests (run before every commit)

fmt: ## Auto-format everything
	uv run ruff check --fix .
	uv run ruff format .
	pnpm fmt
	pnpm --filter web exec eslint --fix .
