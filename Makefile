.DEFAULT_GOAL := help
.PHONY: help setup sync up down ps logs migrate lint fmt test

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-7s %s\n", $$1, $$2}'

setup: sync ## First-time setup: install dependencies and git hooks
	uv run pre-commit install

sync: ## Install all workspace packages and dev tools into .venv
	uv sync

.env:
	cp .env.example .env

up: .env ## Start the local stack and wait until every service is healthy
	docker compose up -d --wait --wait-timeout 180

down: ## Stop the local stack (data volumes are kept)
	docker compose down

ps: ## Show local stack status
	docker compose ps

logs: ## Follow local stack logs
	docker compose logs -f

migrate: ## Apply database migrations to DATABASE_URL
	uv run alembic upgrade head

lint: ## ruff (lint + format check) and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt: ## Auto-fix lint issues and format code
	uv run ruff check --fix .
	uv run ruff format .

test: ## Run all tests
	uv run pytest
