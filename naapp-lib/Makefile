# The candidate gate covers each naapp package through `make lint` and
# `make test` (the root Makefile's NAAPP_PACKAGES).

.PHONY: lint test

lint:
	uv run ruff check .
	uv run mypy src

# NAAPP_LIB_TESTS narrows a run to one shard of the suite; by default it runs all of it.
test:
	uv run pytest -q $(NAAPP_LIB_TESTS)
