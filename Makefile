.PHONY: db-up db-down test lint

db-up:      ## start Postgres and wait until it accepts connections
	docker compose up -d --wait db

db-down:
	docker compose down

test:
	uv run pytest -q

lint:
	uv run ruff check .
