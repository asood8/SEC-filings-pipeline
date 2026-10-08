.DEFAULT_GOAL := help
.PHONY: help setup sync up down ps logs llm-up llm-down obs-up obs-down migrate backup-drill api pipeline images lint fmt test audit web-install web web-types web-check loadtest api-loadtest tf-check tf-fmt k8s-tools k8s-check

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-12s %s\n", $$1, $$2}'

setup: sync ## First-time setup: install dependencies and git hooks
	uv run pre-commit install

sync: ## Install all workspace packages and dev tools into .venv
	uv sync

.env:
	cp .env.example .env

up: .env ## Start the local stack and wait until every service is healthy
	docker compose up -d --wait --wait-timeout 180

down: ## Stop the local stack, including the optional LLM and viewer (data volumes are kept)
	docker compose --profile llm --profile observability down

ps: ## Show local stack status
	docker compose --profile llm --profile observability ps

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

obs-up: ## Start the optional local viewer for traces and metrics: Grafana on http://localhost:3001
	docker compose --profile observability up -d --wait --wait-timeout 180 otel-lgtm

obs-down: ## Stop the local viewer (its data is kept)
	docker compose --profile observability stop otel-lgtm

migrate: ## Apply database migrations to DATABASE_URL
	uv run alembic upgrade head

backup-drill: ## Dump the local database, restore it into a throwaway one, and check every table matches
	scripts/backup-drill.sh

api: ## Run the API locally with auto-reload on http://127.0.0.1:8000 (docs at /docs)
	uv run uvicorn --factory filings_api.app:create_app --reload

EVERY ?= 10

pipeline: ## Run every pipeline stage locally until Ctrl+C, with discovery every 10 minutes (EVERY=n)
	uv run python scripts/dev_pipeline.py --every $(EVERY)

SERVICES := api discovery parser summarizer notifier

images: $(SERVICES:%=image-%) ## Build all service images for this machine as filings-<service>:dev (one: make image-api)

image-%:
	docker build -f docker/service.Dockerfile --build-arg PACKAGE=filings-$* -t filings-$*:dev .

lint: ## ruff (lint + format check) and mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt: ## Auto-fix lint issues and format code (reports what it can't fix)
	-uv run ruff check --fix .
	uv run ruff format .

test: ## Run all tests
	uv run pytest

PIP_AUDIT_VERSION := 2.10.1

# pip-audit checks every package version in uv.lock (dev tools too, our own packages excepted)
# against PyPI's vulnerability data, without installing anything. bash's pipefail makes a failed
# export fail the target instead of auditing an empty list.
audit: ## Check the locked Python and website dependencies for known vulnerabilities (needs the internet)
	bash -o pipefail -c 'uv export --frozen --all-packages --all-groups --all-extras --no-emit-workspace \
	  --format requirements.txt --quiet \
	  | uvx pip-audit==$(PIP_AUDIT_VERSION) --disable-pip --require-hashes --progress-spinner off -r /dev/stdin'
	npm --prefix web audit --omit=dev --audit-level=high

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

loadtest: ## Load-test a running API with k6 (start it with `make api-loadtest` in another terminal)
	@mkdir -p loadtest/results
	k6 run --summary-export loadtest/results/summary.json loadtest/api.js

api-loadtest: ## Run the API with the per-client rate limit raised, as `make loadtest` needs
	API_RATE_LIMIT_PER_MINUTE=1000000 API_RATE_LIMIT_BURST=100000 uv run uvicorn --factory filings_api.app:create_app --workers 2

TF_DIR := infra/terraform
TF_ENV := $(TF_DIR)/envs/production
# Providers are large (about 700 MB in all); keep one downloaded copy for every checkout.
export TF_PLUGIN_CACHE_DIR ?= $(HOME)/.terraform.d/plugin-cache

tf-check: ## Check the Terraform code: format, validate, tests on mock providers, free-tier allowlist (no accounts; creates nothing)
	@mkdir -p $(TF_PLUGIN_CACHE_DIR)
	terraform -chdir=$(TF_DIR) fmt -recursive -check -diff
	terraform -chdir=$(TF_ENV) init -backend=false -input=false -lockfile=readonly
	terraform -chdir=$(TF_ENV) validate
	terraform -chdir=$(TF_ENV) test
	$(TF_DIR)/check-free-tier.sh

tf-fmt: ## Format the Terraform code
	terraform -chdir=$(TF_DIR) fmt -recursive

k8s-tools: ## Install pinned, checksum-verified kubectl, kubeconform, and kubeseal into ~/.local/bin
	scripts/install-k8s-tools.sh

k8s-check: ## Render the Kubernetes manifests and validate them, no cluster needed (make k8s-tools first)
	scripts/check-k8s.sh
