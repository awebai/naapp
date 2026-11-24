.PHONY: lint test

lint:
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest -q
