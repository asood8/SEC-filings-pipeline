.DEFAULT_GOAL := help
.PHONY: help setup sync up down ps logs llm-up llm-down migrate api lint fmt test web-install web web-types web-check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-11s %s\n", $$1, $$2}'

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
EMBEDDING_MODEL ?= nomic-embed-text:v1.5

llm-up: ## Start the optional local LLM (Ollama) and pull the default LLM and embedding models
	docker compose --profile llm up -d --wait ollama
	docker compose exec ollama ollama pull $(LLM_MODEL)
	docker compose exec ollama ollama pull $(EMBEDDING_MODEL)

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

web-install: ## Install the website's dependencies (Node.js 24, see web/.nvmrc)
	npm --prefix web ci

web: ## Run the website with hot reload on http://localhost:3000 (start the API with `make api`)
	npm --prefix web run dev

web-types: ## Regenerate the website's API types from the API's OpenAPI schema
	@mkdir -p web/node_modules/.tmp
	uv run --no-dev --package filings-api python -c 'import json; from filings_api.app import create_app; print(json.dumps(create_app().openapi()))' > web/node_modules/.tmp/openapi.json
	npm --prefix web run gen:api

web-check: ## Lint, type-check, test, and build the website
	npm --prefix web run lint
	npm --prefix web run typecheck
	npm --prefix web test
	npm --prefix web run build
