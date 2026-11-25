"""Library's production operations: its config, its make targets and its runbook,
over naapp-lib's shared Render tool (aweb_naapp.ops)."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
from aweb_naapp.ops.render import ProductionConfig, canonical_git_repo

APP_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = APP_ROOT.parents[1]
PROD_TARGETS = ("prod-public-identity", "prod-status", "prod-deploy", "prod-wait", "prod-verify", "prod-rollback")


def test_production_operations_doc_is_for_shipping_and_recovery() -> None:
    operations_doc = (
        Path(__file__).parents[1] / "docs" / "production-operations.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(operations_doc.split())

    shipping_criterion = (
        "We ship when all tests pass, including the end-to-end tests. Anything anyone wants "
        "to add beyond that goes to Juan, one item at a time, in plain words."
    )
    assert shipping_criterion in normalized
    assert operations_doc.index("We ship when") < operations_doc.index("## Ownership and profile assets")

    for removed_heading in (
        "## AATK verification contract",
        "### AATK matrix worked example",
        "### Exact current-incumbent semantic probe",
        "### Moving the live service onto this build",
    ):
        assert removed_heading not in operations_doc

    verification = operations_doc.split("## Verification surfaces\n", 1)[1].split(
        "## Rollback and recovery", 1
    )[0]
    assert " ".join(verification.split()) == (
        "The isolated stack does not prove production routing, Cloudflare policy, deployed "
        "identity, or shelf state. CI cannot prove the reviewed artifact is the one currently "
        "live or that Cloudflare and Render accept the production client; production smoke "
        "cannot substitute for source-level customer journeys. A paid-provider launch which "
        "cannot run in PR CI is a named protected integration smoke, not an excuse to leave "
        "reproducible server semantics in the deploy gate."
    )

    for retained_heading in (
        "## Required release record",
        "## Targets",
        "## Rollback and recovery",
        "## 2026-07-29 live-transition readiness incident",
    ):
        assert retained_heading in operations_doc


    repo_root = Path(__file__).parents[1]
    render_blueprint = (repo_root / "render.yaml").read_text(encoding="utf-8")
    operations_doc = (repo_root / "docs" / "production-operations.md").read_text(
        encoding="utf-8"
    )
    production = ProductionConfig.load(repo_root / "ops" / "render-production.json")

    assert "NOT the production topology authority" in render_blueprint
    assert "Folio" in render_blueprint
    assert "c621010" in render_blueprint
    assert "ops/render-production.json" in render_blueprint
    assert "Credential-less topology boundary" in operations_doc
    assert (
        "CI cannot prove the reviewed artifact is the one currently live or that Cloudflare "
        "and Render accept the production client; production smoke cannot substitute for "
        "source-level customer journeys."
    ) in operations_doc
    assert "named protected integration smoke" in operations_doc
    for identity_value in (
        production.service_id,
        production.service_name,
        production.region,
        production.origin_url,
        production.public_url,
    ):
        assert f"`{identity_value}`" in operations_doc


def test_the_production_config_deploys_library_from_the_naapp_repository_root() -> None:
    config = ProductionConfig.load(APP_ROOT / "ops" / "render-production.json")
    assert canonical_git_repo(config.repo) == "github.com/awebai/naapp"
    assert (config.root_dir, config.docker_context) == ("", ".")
    assert (REPOSITORY_ROOT / config.dockerfile_path).resolve() == APP_ROOT / "Dockerfile"
    assert (config.app_id, config.service_slug, config.evidence) == ("library", "library-02jf", "required")


def test_library_s_package_metadata_names_the_naapp_repository() -> None:
    urls = tomllib.loads((APP_ROOT / "pyproject.toml").read_text())["project"]["urls"]
    assert urls == {
        "Homepage": "https://github.com/awebai/naapp/tree/main/naapp/library",
        "Repository": "https://github.com/awebai/naapp",
        "Issues": "https://github.com/awebai/naapp/issues",
    }


@pytest.mark.parametrize("target", PROD_TARGETS)
def test_each_production_target_runs_the_shared_tool(target: str) -> None:
    makefile = (APP_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split(f"{target}:\n", 1)[1].split("\n\n", 1)[0]
    assert "python -m aweb_naapp.ops" in recipe
    assert "RENDER_ENV_FILE" not in recipe


@pytest.mark.parametrize("target", PROD_TARGETS)
@pytest.mark.parametrize("variable", ["PROD_CONFIG", "PROD_COMMIT", "PROD_EVIDENCE_DIR"])
def test_a_production_target_never_puts_an_operator_value_into_the_shell(target: str, variable: str) -> None:
    completed = subprocess.run(
        ["make", "-n", target, f"{variable}='; echo INJECTED; #'"],
        cwd=APP_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    assert "INJECTED" not in completed.stdout
