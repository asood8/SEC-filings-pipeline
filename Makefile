.PHONY: sync lint fmt test

sync: ## Install all workspace packages and dev tools into .venv
	uv sync

lint: ## ruff (lint + format check) and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt: ## Auto-fix lint issues and format code
	uv run ruff check --fix .
	uv run ruff format .

test: ## Run all tests
	uv run pytest
