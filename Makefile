SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTEST_UNIT := -m "not integration and not e2e"

.PHONY: help setup lint typecheck test test-int check fmt

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install python + js dependencies and git hooks
	uv sync
	pnpm install
	pre-commit install --install-hooks

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
