.PHONY: install test lint cov run bench docker clean

install:
	python -m venv .venv && .venv/bin/pip install -e ".[dev,anthropic]"

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests

cov:
	.venv/bin/pytest -q --cov=gateway --cov-report=term-missing

run:
	.venv/bin/uvicorn gateway.main:app --reload --port 8080

bench:
	.venv/bin/python bench/loadtest.py

docker:
	docker compose up --build

clean:
	rm -rf .pytest_cache .coverage .ruff_cache **/__pycache__
