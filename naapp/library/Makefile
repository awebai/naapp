.PHONY: test test-server lint compile run e2e e2e-up e2e-down api-serve api-stop \
	prod-tools-prepare prod-shelf-pin prod-ops-test prod-public-identity prod-status prod-deploy prod-wait prod-verify prod-rollback prod-recovery \
	prod-gate-candidate prod-gate-recovery

API_PORT ?= 8765
# A local .env configures the dev server only when it exists; Library itself reads
# no .env file.
LOCAL_ENV_FILE := $(if $(wildcard .env),--env-file .env)
export NAAPP_E2E_PROJECT ?= library-e2e
PROD_CONFIG ?= ops/render-production.json
RENDER_ENV_FILE ?= $(HOME)/.aweb-render/env
PROD_COMMIT ?=
PROD_DEPLOY_ID ?=
CURRENT_DEPLOY_ID ?=
CURRENT_COMMIT ?=
ROLLBACK_DEPLOY_ID ?=
ROLLBACK_COMMIT ?=
CONFIRM_SERVICE_ID ?=
LIBRARY_GATE_TOOL_ROOT ?=
AW_SOURCE_HOME ?=
EXPECTED_PROFILE_REF ?=
EXPECTED_PROFILE_VERSION ?=
EXPECTED_PROFILE_DIGEST ?=
APPLY ?= 0
PROD_EVIDENCE_DIR ?=

# Export operational values instead of interpolating them into recipes. This keeps
# operator-provided IDs and paths out of shell parsing; Python performs exact validation.
export PROD_CONFIG RENDER_ENV_FILE PROD_COMMIT PROD_DEPLOY_ID CURRENT_DEPLOY_ID CURRENT_COMMIT
export ROLLBACK_DEPLOY_ID
export ROLLBACK_COMMIT CONFIRM_SERVICE_ID AW_SOURCE_HOME EXPECTED_PROFILE_VERSION
export EXPECTED_PROFILE_REF EXPECTED_PROFILE_DIGEST APPLY PROD_EVIDENCE_DIR LIBRARY_GATE_TOOL_ROOT

test:
	uv run pytest -q -m "not e2e"

test-server:
	uv run pytest -q -m "not e2e"

lint:
	uv run ruff check .
	uv run mypy src

compile:
	uv run python -m compileall -q src tests

run:
	uv run $(LOCAL_ENV_FILE) python -m uvicorn library.api:app --host 127.0.0.1 --port $(API_PORT) --reload

e2e:
	set -e; \
	trap 'docker compose -p $(NAAPP_E2E_PROJECT) -f docker-compose.e2e.yml down -v --remove-orphans' EXIT; \
	docker compose -p $(NAAPP_E2E_PROJECT) -f docker-compose.e2e.yml down -v --remove-orphans >/dev/null 2>&1 || true; \
	docker compose -p $(NAAPP_E2E_PROJECT) -f docker-compose.e2e.yml up --build -d; \
	LIBRARY_E2E=1 uv run pytest -q -m e2e

e2e-up:
	docker compose -p $(NAAPP_E2E_PROJECT) -f docker-compose.e2e.yml up --build -d

e2e-down:
	docker compose -p $(NAAPP_E2E_PROJECT) -f docker-compose.e2e.yml down -v --remove-orphans

api-serve:
	@[ ! -f .api.pid ] || { echo "api server already running (pid $$(cat .api.pid)); make api-stop first"; exit 1; }
	@nohup uv run $(LOCAL_ENV_FILE) python -m uvicorn library.api:app --host 127.0.0.1 --port $(API_PORT) > .api.log 2>&1 & echo $$! > .api.pid
	@echo "api serving at http://127.0.0.1:$(API_PORT)/ (pid $$(cat .api.pid))"

api-stop:
	@[ -f .api.pid ] && { kill $$(cat .api.pid) 2>/dev/null || true; rm -f .api.pid; echo "api server stopped"; } || echo "api server not running"

# Production Render operations are intentionally Make-only. Never replace these
# targets with inline curl/shell mutations in a release session.
prod-tools-prepare:
	uv run python scripts/prepare_gate_tools.py

prod-shelf-pin:
	uv run python scripts/library_prod_gate.py shelf-pin

prod-ops-test:
	uv run pytest -q tests/test_library_prod_gate.py tests/test_prepare_gate_tools.py

# Render operations run naapp-lib's shared tool (aweb_naapp.ops), with Library's
# config; its own checks are the library_prod_gate.py steps after verify.
prod-public-identity:
	uv run python -m aweb_naapp.ops public-identity

prod-status:
	uv run python -m aweb_naapp.ops status

prod-deploy:
	uv run python -m aweb_naapp.ops deploy --repo-root ../..

prod-wait:
	uv run python -m aweb_naapp.ops wait

prod-verify:
	uv run python -m aweb_naapp.ops verify
	uv run python scripts/library_prod_gate.py candidate

prod-rollback:
	uv run python -m aweb_naapp.ops rollback

# This recovery fingerprint is specific to the 0c490a362 rollback artifact. Future
# releases must add and review their own exact recovery gate rather than reusing it.
prod-recovery:
	$(MAKE) prod-rollback
	$(MAKE) prod-gate-recovery

prod-gate-candidate:
	uv run python scripts/library_prod_gate.py candidate

prod-gate-recovery:
	uv run python scripts/library_prod_gate.py runtime-managed-0c490a362
