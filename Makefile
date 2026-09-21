.PHONY: install run infra sandbox test lint

install:
	python -m pip install -e ".[dev]"

run:
	uvicorn minion.api:app --host 0.0.0.0 --port 8000 --reload

infra:
	docker compose up -d postgres redis

sandbox:
	docker build -f Dockerfile.sandbox -t minion-sandbox:latest .

test:
	pytest -q

lint:
	ruff check src tests
	python -m compileall -q src
