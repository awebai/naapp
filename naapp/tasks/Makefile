.PHONY: test lint e2e

test:
	uv run pytest -q -m "not e2e"

e2e:
	TASKS_E2E=1 uv run pytest -q -m e2e tests/test_e2e.py

lint:
	uv run ruff check .
	uv run mypy src board_move
