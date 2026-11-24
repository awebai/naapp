# The checks CI runs, for any runner: the workflow in .github/workflows/ci.yml
# calls only these targets. PACKAGES narrows a run to the packages a change
# affects (scripts/affected_packages.py); by default it is every package.

ALL_PACKAGES := naapp-lib naapp/folio naapp/library naapp/logs naapp/secrets naapp/secrets/runner naapp/secrets/values
PACKAGES ?= $(ALL_PACKAGES)

# The suites run behind the egress guard, which refuses network access beyond
# loopback and the reserved test names.
TEST_EGRESS_RUNNER ?= python3 $(abspath scripts/test-egress/native.py) --

# The AWID release production runs, locked, for the suites that test against
# it; synced before a suite goes offline.
PRODUCTION_AWID_PROJECT := scripts/test-egress/production-awid

# The aw release the e2e journeys run, installed by scripts/install-aw.sh.
AW_DIR ?= $(abspath .tools/bin)

SCRIPT_TESTS := test_affected_packages test_ci_result test_ci_workflow test_served_manifest_digests \
	test_secrets_shards test_shipping

.PHONY: affected sync lint test test-scripts production-awid-sync browsers renderer-deps test-renderer aw \
	check-library-aatk check-secrets-shards secrets-aw-tools e2e-folio e2e-library e2e-secrets release-checks \
	release-candidate release-publish

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

# What Secrets' tests run with aw, prepared while the network is still allowed:
# the pinned aw release and the lab's human fixture built against it.
SECRETS_AW_TOOLS := $(if $(filter naapp/secrets,$(PACKAGES)),secrets-aw-tools)

secrets-aw-tools: aw
	scripts/build-lab-human-fixture.sh $(AW_DIR)

# The suites run offline behind the egress guard. AWS_EC2_METADATA_DISABLED
# keeps the AWS SDK from probing a cloud host's metadata service for credentials.
test: production-awid-sync $(SECRETS_AW_TOOLS)
	set -e; for package in $(PACKAGES); do \
		echo "=== test $$package ==="; \
		uv sync --frozen --project $$package; \
		UV_FROZEN=1 UV_OFFLINE=1 PYTHONPYCACHEPREFIX=/tmp/pycache \
			NAAPP_REQUIRE_TEST_DATABASE=1 LIBRARY_REQUIRE_TEST_DATABASE=1 \
			AWS_EC2_METADATA_DISABLED=true \
			NAAPP_AW_BINARY=$(AW_DIR)/aw NAAPP_LAB_HUMAN_FIXTURE=$(AW_DIR)/lab-human-fixture \
			$(TEST_EGRESS_RUNNER) $(MAKE) -C $$package test; \
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

# The page renderer's npm lock and the Chromium of its own Playwright, which
# naapp-lib's store/renderer browser test and the renderer's tests drive.
renderer-deps:
	npm ci --ignore-scripts --no-audit --no-fund --prefix naapp-lib/renderer
	naapp-lib/renderer/node_modules/.bin/playwright install --with-deps --only-shell chromium

# The page renderer's own tests (Node, then Playwright against the gallery).
test-renderer:
	cd naapp-lib/renderer && $(TEST_EGRESS_RUNNER) npm test

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

# Everything CI runs, for every package: scripts/release_candidate.py tags a
# commit only after this passes. It needs PostgreSQL and Redis for the suites
# and Docker for the journeys, as the CI jobs have them.
release-checks:
	$(MAKE) sync PACKAGES="$(ALL_PACKAGES)"
	$(MAKE) lint PACKAGES="$(ALL_PACKAGES)"
	$(MAKE) test-scripts
	$(MAKE) check-library-aatk
	$(MAKE) check-secrets-shards
	$(MAKE) test PACKAGES="$(ALL_PACKAGES)"
	$(MAKE) aw
	$(MAKE) e2e-folio
	$(MAKE) e2e-library
	$(MAKE) e2e-secrets

# Runs release-checks on a clean commit of origin/main, then tags it (docs/release.md).
release-candidate:
	@test -n "$(TAGS)" || { echo "TAGS is required" >&2; exit 2; }
	python3 scripts/release_candidate.py $(TAGS)

release-publish:
	@test -n "$(TAG)" || { echo "TAG is required" >&2; exit 2; }
	python3 scripts/publish_release.py "$(TAG)"
