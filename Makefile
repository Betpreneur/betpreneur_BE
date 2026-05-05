# ================================================================
# Betpreneur Backend - Makefile
# ================================================================

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
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs

logs-f:
	docker compose logs -f

# ------------------------------
# Management Commands
# ------------------------------
migrate:
	docker compose exec backend python manage.py migrate

makemigrations:
	docker compose exec backend python manage.py makemigrations

createsuperuser:
	docker compose exec backend python manage.py createsuperuser

shell:
	docker compose exec backend python manage.py shell

collectstatic:
	docker compose exec backend python manage.py collectstatic --noinput

test:
	docker compose exec backend python manage.py test

# ------------------------------
# Development
# ------------------------------
dev: down
	@echo "$(YELLOW)Starting development server...$(NC)"
	docker compose up -d db
	@sleep 2
	docker compose exec backend python manage.py migrate
	docker compose up -d backend

# ------------------------------
# Production
# ------------------------------
prod: down
	@echo "$(YELLOW)Starting production server...$(NC)"
	docker compose --profile prod up -d

# ------------------------------
# Cleanup
# ------------------------------
clean:
	docker compose down -v
	rm -rf staticfiles media
	@echo "$(YELLOW)Cleaned up!$(NC)"