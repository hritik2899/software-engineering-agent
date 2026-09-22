# Minion developer entry points.
# These commands intentionally mirror the CI/runtime setup so local verification
# exercises the same package install, tests and sandbox image used by the service.
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
