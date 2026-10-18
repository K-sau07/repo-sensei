SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTEST_UNIT := -m "not integration and not e2e"

.PHONY: help setup up down reset-db psql infra-check api openapi lint typecheck test test-int check fmt

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install python + js dependencies and git hooks
	uv sync
	pnpm install
	pre-commit install --install-hooks

COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env

up: ## Start local infra (postgres, redis, minio) and wait until healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop local infra (keeps data)
	$(COMPOSE) down

reset-db: ## Delete the local postgres volume (asks first)
	@read -p "Delete local postgres data? [y/N] " ans && [ "$$ans" = "y" ]
	$(COMPOSE) down
	docker volume rm repo-sensei_pgdata || true

psql: ## Open psql on the dev database
	$(COMPOSE) exec postgres psql -U sensei -d sensei

infra-check: ## Verify extensions, redis and buckets
	uv run python scripts/check_infra.py

api: ## Run the API with auto-reload on :8000
	uv run uvicorn sensei_api.app:create_app --factory --reload --port 8000

openapi: ## Export the OpenAPI schema for the web client
	uv run python scripts/export_openapi.py

lint: ## Lint and format-check python and web
	uv run ruff check .
	uv run ruff format --check .
	pnpm fmt:check

typecheck: ## Static type checks
	uv run pyright

test: ## Unit tests
	uv run pytest $(PYTEST_UNIT) -q

test-int: ## Integration tests (needs `make up`)
	uv run pytest -m integration -q

check: lint typecheck test ## Lint, typecheck and unit tests (run before every commit)

fmt: ## Auto-format everything
	uv run ruff check --fix .
	uv run ruff format .
	pnpm fmt
