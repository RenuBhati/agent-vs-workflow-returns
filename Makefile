.PHONY: db-up db-down migrate seed serve test lint

db-up:      ## start Postgres and wait until it accepts connections
	docker compose up -d --wait db

db-down:
	docker compose down

test:
	uv run pytest -q

lint:
	uv run ruff check .

migrate:    ## apply migrations to the dev database
	uv run alembic upgrade head

seed:       ## reset the dev database to the v1 fixture
	uv run python -m app.fixtures two_shirts_v1

serve:      ## demo page at http://localhost:8000 (needs Ollama running)
	ALLOW_RESET=true uv run uvicorn app.api:app --port 8000
