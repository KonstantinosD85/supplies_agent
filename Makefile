.PHONY: help api mcp agent docker-up docker-down lint format test eval eval-report

# `make` alone shows this list. Each target's `## ...` text is its help line.
help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Local services, each in its own terminal.
api: ## Run the mock API (127.0.0.1:8001)
	uv run mock-api

mcp: ## Run the MCP server (127.0.0.1:8002/mcp)
	uv run mcp-server

agent: ## Run the agent service: UI and /invocations (127.0.0.1:8080)
	uv run agent-service

# Same ports as the local services: stop those first.
docker-up: ## Build and run everything in containers, plus Grafana (localhost:3000)
	docker compose up --build

# Data volumes are kept; `docker compose down -v` deletes them.
docker-down: ## Stop and remove the containers (keeps the data volumes)
	docker compose down

lint: ## Check lint rules and formatting (changes no files)
	uv run ruff check . && uv run ruff format --check .

format: ## Fix what ruff can fix, then format the code
	uv run ruff check --fix . && uv run ruff format .

test: ## Run the test suite (no LLM calls)
	uv run pytest

eval: ## Run all eval cases (needs ANTHROPIC_API_KEY); writes evals/results/latest.json
	uv run python -m evals

# The report is written even if cases failed; exits with the evals' status.
eval-report: ## Run the evals, then write evals/results/latest.html
	uv run python -m evals; status=$$?; uv run python scripts/eval_report.py && exit $$status
