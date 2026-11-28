.PHONY: test lint e2e \
	prod-status prod-deploy prod-wait prod-verify prod-rollback

PROD_CONFIG ?= ops/render-production.json

test:
	uv run pytest -q -m "not e2e"

e2e:
	TASKS_E2E=1 uv run pytest -q -m e2e tests/test_e2e.py

lint:
	uv run ruff check .
	uv run mypy src

# Production Render operations are Make-only and run the shared aweb_naapp.ops tool
# against ops/render-production.json. status and verify only read; deploy and
# rollback need APPLY=1 and CONFIRM_SERVICE_ID. Inputs come from the environment:
# PROD_COMMIT, PROD_DEPLOY_ID, ROLLBACK_DEPLOY_ID, ROLLBACK_COMMIT,
# CURRENT_DEPLOY_ID, CURRENT_COMMIT, RENDER_ENV_FILE.
prod-status:
	uv run python -m aweb_naapp.ops --config $(PROD_CONFIG) status

prod-deploy:
	uv run python -m aweb_naapp.ops --config $(PROD_CONFIG) deploy --repo-root ../..

prod-wait:
	uv run python -m aweb_naapp.ops --config $(PROD_CONFIG) wait

prod-verify:
	uv run python -m aweb_naapp.ops --config $(PROD_CONFIG) verify

prod-rollback:
	uv run python -m aweb_naapp.ops --config $(PROD_CONFIG) rollback
