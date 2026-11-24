# The checks CI runs, for any runner: the workflow in .github/workflows/ci.yml
# calls only these targets. PACKAGES narrows a run to the packages a change
# affects (scripts/affected_packages.py); by default it is every package.

PACKAGES ?= naapp-lib naapp/folio naapp/library naapp/logs naapp/secrets naapp/secrets/runner naapp/secrets/values

# The suites run behind the egress guard, which refuses network access beyond
# loopback and the reserved test names.
TEST_EGRESS_RUNNER ?= python3 $(abspath scripts/test-egress/native.py) --

# The AWID release production runs, locked, for the suites that test against
# it; synced before a suite goes offline.
PRODUCTION_AWID_PROJECT := scripts/test-egress/production-awid

# The aw release the e2e journeys run, installed by scripts/install-aw.sh.
AW_DIR ?= $(abspath .tools/bin)

SCRIPT_TESTS := test_affected_packages test_served_manifest_digests test_secrets_shards

.PHONY: affected sync lint test test-scripts production-awid-sync browsers aw \
	check-library-aatk check-secrets-shards e2e-folio e2e-library e2e-secrets

# The packages the change from BASE to HEAD affects, as a JSON list.
affected:
	@test -n "$(BASE)" || { echo "BASE is required" >&2; exit 2; }
	@git diff --name-only "$(BASE)"...HEAD | python3 scripts/affected_packages.py

production-awid-sync:
	uv sync --frozen --project $(PRODUCTION_AWID_PROJECT)

# Installs each package's locked dependencies, for a runner that then goes offline.
sync:
	set -e; for package in $(PACKAGES); do \
		uv sync --frozen --project $$package; \
	done

# UV_FROZEN keeps each run on its committed lock, and a lock the run changed
# fails it.
lint:
	set -e; for package in $(PACKAGES); do \
		echo "=== lint $$package ==="; \
		UV_FROZEN=1 $(MAKE) -C $$package lint; \
		git diff --exit-code -- $$package/uv.lock; \
	done

test: production-awid-sync
	set -e; for package in $(PACKAGES); do \
		echo "=== test $$package ==="; \
		uv sync --frozen --project $$package; \
		UV_FROZEN=1 UV_OFFLINE=1 PYTHONPYCACHEPREFIX=/tmp/pycache \
			NAAPP_REQUIRE_TEST_DATABASE=1 LIBRARY_REQUIRE_TEST_DATABASE=1 $(TEST_EGRESS_RUNNER) $(MAKE) -C $$package test; \
		git diff --exit-code -- $$package/uv.lock; \
	done

# The repository's own scripts: package selection, manifest digests, shards
# and the test infrastructure, in the runtime's locked environment, which has
# the pytest the shard check runs.
test-scripts:
	cd scripts && uv run --frozen --project ../naapp-lib python -m unittest -q $(SCRIPT_TESTS)
	cd scripts/test-egress && python3 -m unittest -q test_native test_pgdg_key

# The Chromium each package's browser tests drive, through its own lock.
browsers:
	set -e; for package in $(PACKAGES); do \
		if uv run --project $$package --frozen --no-sync python -c 'import playwright' 2>/dev/null; then \
			(cd $$package && uv run --frozen --no-sync playwright install --with-deps --only-shell chromium); \
		fi; \
	done

aw:
	scripts/install-aw.sh $(AW_DIR)

check-library-aatk:
	$(MAKE) -C naapp/library aatk-spec-check

# The real-stack journeys. They start Docker containers.
e2e-folio:
	PATH="$(AW_DIR):$$PATH" bash scripts/check-naapp-e2e-aw.sh
	PATH="$(AW_DIR):$$PATH" $(MAKE) -C naapp/folio e2e

e2e-library:
	PATH="$(AW_DIR):$$PATH" bash scripts/check-naapp-e2e-aw.sh
	PATH="$(AW_DIR):$$PATH" $(MAKE) -C naapp/library e2e

e2e-secrets:
	$(MAKE) -C naapp/secrets e2e

# Each Secrets test module is in exactly one shard of candidate-gate/secrets-shards.json.
check-secrets-shards:
	uv sync --frozen --project naapp/secrets
	$(MAKE) -C naapp/secrets check-shards
