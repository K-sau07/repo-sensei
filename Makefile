SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTEST_UNIT := -m "not integration and not e2e"

.PHONY: help setup up down reset-db psql infra-check api worker beat dev webhooks web web-build storybook storybook-build e2e e2e-real sandbox-image openapi migrate revision downgrade lint typecheck test test-int check fmt eval golden-validate

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install python + js dependencies, test browser and git hooks
	uv sync
	pnpm install
	pnpm --filter web exec playwright install chromium chromium-headless-shell
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

# Every queue in sensei_core.jobs.Queue (a test keeps the two in sync). Production runs separate
# workers per queue group; in dev one worker takes them all.
WORKER_QUEUES := default,webhooks,ingest,parse,embed,llm,graph,agent,sandbox,maintenance

worker: ## Run a worker for every queue
	uv run celery -A sensei_workers.app worker --loglevel=INFO --queues=$(WORKER_QUEUES) --concurrency=2

beat: ## Run the periodic task scheduler
	@mkdir -p .cache
	uv run celery -A sensei_workers.app beat --loglevel=INFO --schedule=.cache/celerybeat-schedule

dev: ## Run api, worker, beat and web together (Procfile.dev)
	uv run honcho -f Procfile.dev start

# Webhooks reach a laptop through a smee.io channel: create one at https://smee.io, set it as the
# GitHub App's webhook URL and as SMEE_URL (environment or .env). smee re-encodes the JSON, so a
# payload with unusual characters can fail the signature check; a byte-exact tunnel such as
# `cloudflared tunnel --url http://localhost:8000` avoids that.
SMEE_URL ?= $(shell sed -n 's/^SMEE_URL=//p' .env 2>/dev/null)

webhooks: ## Forward GitHub webhooks from SMEE_URL to the local API
	@test -n "$(SMEE_URL)" || { echo "Set SMEE_URL (a https://smee.io channel) in .env first"; exit 1; }
	pnpm dlx smee-client@5 --url "$(SMEE_URL)" --target http://localhost:8000/api/v1/webhooks/github

web: ## Run the web app with hot reload on :3000 (proxies /api to :8000)
	pnpm --filter web dev

web-build: ## Production build of the web app
	pnpm --filter web build

storybook: ## Run Storybook on :6006
	pnpm --filter web storybook

storybook-build: ## Build the static Storybook into apps/web/storybook-static
	pnpm --filter web storybook:build

e2e: ## Browser tests of the web app against the mock API (no backend needed)
	pnpm --filter web e2e

e2e-real: ## Browser tests against the real API and a GitHub OAuth stub (needs `make up`)
	uv run python scripts/e2e/prepare_db.py
	pnpm --filter web e2e:real

SANDBOX_PYTHONS := 3.13 3.14 3.12

sandbox-image: ## Build the sandbox's Python runner images (one per Python version)
	for py in $(SANDBOX_PYTHONS); do \
		docker build -f sandbox/images/python-runner.Dockerfile --build-arg PYTHON_VERSION=$$py \
			-t repo-sensei/python-runner:$$py sandbox/images || exit 1; \
	done

openapi: ## Export the OpenAPI schema and regenerate the web client types
	uv run python scripts/export_openapi.py
	pnpm --filter web gen:api

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

test: ## Unit tests (python, web units and stories with accessibility checks)
	uv run pytest $(PYTEST_UNIT) -q
	pnpm --filter web test

test-int: ## Integration tests (needs `make up`)
	uv run pytest -m integration -q

check: lint typecheck test ## Lint, typecheck and unit tests (run before every commit)

eval: ## Run a golden suite: make eval suite=<name> retriever=<name> [repo=<id>] [k=10,20,50,100] [args="--no-store …"]
	@test -n "$(suite)" -a -n "$(retriever)" || { echo "usage: make eval suite=<name> retriever=<name>"; exit 1; }
	uv run sensei-eval run --suite "$(suite)" --retriever "$(retriever)" $(if $(repo),--repo "$(repo)") $(if $(k),--k "$(k)") $(args)

golden-validate: ## Check the private golden suites against their pinned commits (skips without them)
	uv run python scripts/label_helper.py validate --fetch

fmt: ## Auto-format everything
	uv run ruff check --fix .
	uv run ruff format .
	pnpm fmt
	pnpm --filter web exec eslint --fix .
