# ================================================================
# Betpreneur Backend - Makefile
# ================================================================

# All docker commands run against deploy/compose.yaml, whose build context
# is the repo root.
COMPOSE = docker compose --project-directory . -f deploy/compose.yaml

# Colors
GREEN = \033[0;32m
YELLOW = \033[0;33m
BLUE = \033[0;34m
NC = \033[0m # No Color

.PHONY: help build up down logs restart migrate createsuperuser shell prod dev clean

# ------------------------------
# Help
# ------------------------------
help:
	@echo "$(BLUE)Betpreneur Backend - Docker Commands$(NC)"
	@echo ""
	@echo "  $(GREEN)make build$(NC)          Build Docker images"
	@echo "  $(GREEN)make up$(NC)              Start all services"
	@echo "  $(GREEN)make down$(NC)             Stop all services"
	@echo "  $(GREEN)make restart$(NC)           Restart all services"
	@echo "  $(GREEN)make logs$(NC)             View logs"
	@echo "  $(GREEN)make logs-f$(NC)           View logs (follow)"
	@echo "  $(GREEN)make migrate$(NC)          Run migrations"
	@echo "  $(GREEN)make createsuperuser$(NC) Create superuser"
	@echo "  $(GREEN)make shell$(NC)          Django shell"
	@echo "  $(GREEN)make prod$(NC)            Start production mode"
	@echo "  $(GREEN)make dev$(NC)             Start development mode"
	@echo "  $(GREEN)make clean$(NC)           Clean up containers and volumes"

# ------------------------------
# Docker Compose Shortcuts
# ------------------------------
build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

logs:
	$(COMPOSE) logs

logs-f:
	$(COMPOSE) logs -f

# ------------------------------
# Management Commands
# ------------------------------
migrate:
	$(COMPOSE) exec backend python manage.py migrate

makemigrations:
	$(COMPOSE) exec backend python manage.py makemigrations

createsuperuser:
	$(COMPOSE) exec backend python manage.py createsuperuser

shell:
	$(COMPOSE) exec backend python manage.py shell

collectstatic:
	$(COMPOSE) exec backend python manage.py collectstatic --noinput

test:
	$(COMPOSE) exec backend python manage.py test

# ------------------------------
# Development
# ------------------------------
dev: down
	@echo "$(YELLOW)Starting development server...$(NC)"
	$(COMPOSE) up -d db
	@sleep 2
	$(COMPOSE) exec backend python manage.py migrate
	$(COMPOSE) up -d backend

# ------------------------------
# Production
# ------------------------------
prod: down
	@echo "$(YELLOW)Starting production server...$(NC)"
	$(COMPOSE) --profile prod up -d

# ------------------------------
# Cleanup
# ------------------------------
clean:
	$(COMPOSE) down -v
	rm -rf staticfiles media
	@echo "$(YELLOW)Cleaned up!$(NC)"
# ------------------------------
# Refactor verification gate
# ------------------------------
# NOTE: .env points DB_HOST at a remote database. Every target below forces a
# throwaway sqlite file so local tooling can never reach it.
DJ_GUARD = DB_ENGINE=django.db.backends.sqlite3 DB_NAME=$(CURDIR)/.tooling.sqlite3 \
           DB_HOST= DB_USER= DB_PASSWORD= DB_PORT=
DJ = $(DJ_GUARD) .venv/bin/python manage.py

.PHONY: verify verify-schema verify-api verify-migrations verify-imports verify-lint verify-tests

## Full gate — run this at the end of every work package.
verify: verify-schema verify-api verify-migrations verify-imports verify-lint verify-tests
	@echo ""
	@echo "GATE PASSED"

## Schema built from migrations must match the base ref exactly.
verify-schema:
	@./scripts/verify_schema.sh

## The public HTTP API must not move. Frozen for the whole refactor.
verify-api:
	@./scripts/verify_api.sh

## Model definitions must match migration state — no un-generated changes.
verify-migrations:
	@echo "Checking migration state…"
	@$(DJ) makemigrations --check --dry-run

## Module boundaries: layer order, domain purity, integration isolation.
verify-imports:
	@echo "Checking import contracts…"
	@.venv/bin/lint-imports

## Lint. Pyflakes-level findings (undefined names, unused imports) are errors;
## inherited style debt is listed in pyproject's ignore list.
verify-lint:
	@echo "Linting…"
	@.venv/bin/ruff check betpreneur config

## Full test suite (sqlite cannot clone in parallel under forkserver).
verify-tests:
	@echo "Running tests…"
	@$(DJ) test
