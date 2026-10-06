.DEFAULT_GOAL := help
.PHONY: help setup sync up down ps logs llm-up llm-down migrate api lint fmt test

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

down: ## Stop the local stack, including the optional LLM (data volumes are kept)
	docker compose --profile llm down

ps: ## Show local stack status
	docker compose --profile llm ps

logs: ## Follow local stack logs
	docker compose logs -f

LLM_MODEL ?= llama3.2:3b

llm-up: ## Start the optional local LLM (Ollama) and pull the default model
	docker compose --profile llm up -d --wait ollama
	docker compose exec ollama ollama pull $(LLM_MODEL)

llm-down: ## Stop the local LLM (downloaded models are kept)
	docker compose --profile llm stop ollama

migrate: ## Apply database migrations to DATABASE_URL
	uv run alembic upgrade head

api: ## Run the API locally with auto-reload on http://127.0.0.1:8000 (docs at /docs)
	uv run uvicorn --factory filings_api.app:create_app --reload

lint: ## ruff (lint + format check) and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt: ## Auto-fix lint issues and format code (reports what it can't fix)
	-uv run ruff check --fix .
	uv run ruff format .

test: ## Run all tests
	uv run pytest
