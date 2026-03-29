.PHONY: dev test test-fast test-all lint type-check security build clean install

dev:
	uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8420

test:
	python -m pytest tests/ -v --cov=app --cov-report=term-missing

test-fast:
	python -m pytest tests/ -x -q

lint:
	ruff check app/ tests/
	ruff format --check app/ tests/

lint-fix:
	ruff check --fix app/ tests/
	ruff format app/ tests/

type-check:
	mypy app/

security:
	bandit -r app/ -ll

check: lint type-check security test

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

test-all:
	bash scripts/run-all-tests.sh

install:
	pip install -e ".[dev]"
