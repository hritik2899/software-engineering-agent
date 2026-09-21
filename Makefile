.PHONY: install run infra sandbox migrate smoke test lint verify

install:
	python -m pip install -e ".[dev]"

run:
	uvicorn minion.api:app --host 0.0.0.0 --port 8000 --reload

infra:
	docker compose up -d postgres redis

sandbox:
	docker build -f Dockerfile.sandbox -t minion-sandbox:latest .

migrate:
	alembic upgrade head

smoke:
	python scripts/smoke_test.py

test:
	pytest -q

lint:
	ruff check src tests scripts migrations
	python -m compileall -q src scripts migrations

verify: lint test smoke
