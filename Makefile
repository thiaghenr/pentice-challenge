.PHONY: install db dev test cov lint format typecheck migrate check serve-rules up down logs

PORT ?= 8000
RULES_PORT ?= 8001

install:
	uv sync

# Postgres only (docker compose), for `make dev` / `make test` outside Docker.
db:
	docker compose up -d --wait db

dev: db migrate
	uv run uvicorn pentice_quiz.main:create_app --factory --reload --port $(PORT)

test: db
	uv run pytest

cov: db
	uv run pytest --cov --cov-report=term-missing

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy src

migrate:
	uv run alembic upgrade head

check: lint typecheck test

# Serves data/house-rules over HTTP so HOUSE_RULES_URL can point at it locally.
serve-rules:
	uv run python -m http.server $(RULES_PORT) --directory data/house-rules

# Docker: Postgres on :5432, API on :8000 and the house-rules server on :8001 (needs a .env, see .env.example).
up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api
