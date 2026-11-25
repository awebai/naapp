from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).parents[1]
_ROOT = Path(__file__).parents[3]


def _workflow() -> dict:
    # Library's CI is the repository's CI, at the repository root: GitHub
    # Actions reads .github/workflows only there, so a workflow under
    # naapp/library/.github/workflows never runs at all.
    path = _ROOT / ".github" / "workflows" / "ci.yml"
    assert path.is_file(), "the repository has no pull-request CI workflow at its root"
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _recipe(target: str) -> str:
    """The recipe of one target of the root Makefile, every CI step's entry point."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n?)+)", makefile, re.MULTILINE)
    assert match, f"the root Makefile has no {target} target"
    return match.group(1)


def _commands(*jobs: str) -> str:
    workflow = _workflow()
    return "\n".join(step.get("run", "") for job in jobs for step in workflow["jobs"][job]["steps"])


def test_library_has_no_buried_workflow_that_cannot_run() -> None:
    """A workflow below the repository root is dead config that reads as live.

    It is not merely inert: a reader who finds it believes Library is covered,
    which is how the coverage gap survived the move.
    """
    buried = _REPO_ROOT / ".github" / "workflows"
    assert not buried.exists(), (
        f"{buried} cannot be reached by GitHub Actions - Library's CI belongs at the repository root"
    )


def test_pytest_uses_locked_dependencies_and_rejects_warnings() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]

    assert pytest_options["pythonpath"] == [".", "src"]
    assert pytest_options["filterwarnings"] == ["error"]
    assert pytest_options["addopts"] == ["--basetemp=.pytest_tmp"]
    assert any(dependency.startswith("httpx2") for dependency in pyproject["dependency-groups"]["dev"])


def test_make_test_uses_the_locked_uv_environment() -> None:
    makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    test_recipe = makefile.split("test:\n", 1)[1].split("\n\n", 1)[0]

    assert "uv run pytest" in test_recipe
    assert "python3 -m pytest" not in test_recipe


def test_ci_provides_postgres_so_database_tests_cannot_skip() -> None:
    assert "LIBRARY_REQUIRE_TEST_DATABASE=1" in _recipe("test")
    assert "postgres" in _workflow()["jobs"]["library"]["services"]
    assert "systemctl restart docker" not in _commands("library", "library-e2e")


def test_ci_break_glass_requires_visible_protection_restoration() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Lint, test, and real-stack e2e" in readme
    assert "temporarily disable" in readme
    assert "re-enable" in readme


_POLICY_SEMANTIC_GROUPS = (
    "the protected gate plus an independent exact-head review is sufficient integration authority",
    "a separate coordinator merge is not required",
    "This policy applies only while all of these conditions hold",
    "the head being merged exactly matches the independently reviewed head",
    "up to date with `main`",
    "change to `main` triggers re-evaluation",
    "the required context remains `Lint, test, and real-stack e2e`",
    "GitHub Actions app with ID `15368`",
    "protection is enforced for administrators, with no bypass",
    "the push to `main` runs the same required check against the integrated commit",
    "push-main run 30437187502",
    "5137334ae2b88c7515c6c080c427fafaf1e71faa",
    "the reviewed head changes",
    "protection is weakened",
    "the required context or app changes",
    "a conflict or manual recombination changes the reviewed result",
    "the change spans repositories",
    "a release tag is being chosen",
    "the deployment boundary moves",
    "fresh review and coordinator routing",
    "authorizes source integration only",
    "does not by itself authorize a release, production mutation, or deployment",
)
_REVISIT_SEMANTIC_GROUPS = (
    "through Jules",
    "only when aweb itself has a protected, green hosted canonical merge-state gate",
    "tracked by `aweb-aatq`",
)


def _assert_gate_policy_contract(readme: str) -> None:
    readme = " ".join(readme.split())
    policy = readme.index("### Gate-authorized integration")
    break_glass = readme.index("The break-glass path")
    revisit = readme.index("Re-evaluate whether this repository-specific policy")
    production = readme.index("## Production operations")

    assert policy < break_glass < revisit < production
    policy_block = readme[policy:break_glass]
    revisit_block = readme[revisit:production]
    for semantic_group in _POLICY_SEMANTIC_GROUPS:
        assert semantic_group in policy_block
    for semantic_group in _REVISIT_SEMANTIC_GROUPS:
        assert semantic_group in revisit_block


def test_ci_documents_when_the_gate_is_integration_authority() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")

    _assert_gate_policy_contract(readme)


@pytest.mark.parametrize("semantic_group", _POLICY_SEMANTIC_GROUPS + _REVISIT_SEMANTIC_GROUPS)
def test_ci_gate_policy_contract_rejects_semantic_weakening(semantic_group: str) -> None:
    readme = " ".join((_REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    assert semantic_group in readme

    with pytest.raises(AssertionError):
        _assert_gate_policy_contract(readme.replace(semantic_group, "", 1))


def test_ci_runs_every_make_gate_on_main_pull_request_and_manual_retry() -> None:
    workflow = _workflow()

    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["on"]["push"] == {"branches": ["main"]}
    commands = _commands("library", "library-e2e")
    for target in ("lint", "test", "e2e-library"):
        assert f"make {target}" in commands
    assert "naapp/library e2e" in _recipe("e2e-library")


def test_ci_runs_the_e2e_with_the_pinned_aw_release() -> None:
    """The e2e runs the aw release scripts/install-aw.sh pins, never a build.

    The journey's real client is a released aw, so the e2e proves Library
    against what users install.
    """
    workflow = _workflow()
    steps = workflow["jobs"]["library-e2e"]["steps"]
    commands = [step.get("run", "") for step in steps]
    assert commands.index("make aw") < commands.index("make e2e-library")
    assert not any(step.get("uses", "").startswith("actions/setup-go@") for step in steps)
    assert "go build" not in "\n".join(commands)
    assert "scripts/install-aw.sh" in _recipe("aw")
    assert "scripts/check-naapp-e2e-aw.sh" in _recipe("e2e-library")


def test_the_e2e_stack_runs_published_awid_by_digest() -> None:
    """The e2e stack's registry is a published awid image pinned by digest.

    awid is aweb's, released from aweb; a tag can move under a run, a digest
    cannot. No other repository is checked out to build it.
    """
    steps = [step for job in _workflow()["jobs"].values() for step in job["steps"]]
    foreign = [
        step for step in steps
        if step.get("uses", "").startswith("actions/checkout@")
        and (step.get("with") or {}).get("repository") not in (None, "", "${{ github.repository }}")
    ]
    assert not foreign, f"found external checkout(s): {[s.get('with', {}).get('repository') for s in foreign]}"

    compose = yaml.safe_load((_REPO_ROOT / "docker-compose.e2e.yml").read_text(encoding="utf-8"))
    image = compose["services"]["awid"].get("image", "")
    assert re.fullmatch(r"ghcr\.io/awebai/awid:[0-9.]+@sha256:[0-9a-f]{64}", image), image
    assert "build" not in compose["services"]["awid"]


def test_ci_also_covers_naapp_lib() -> None:
    """naapp-lib is a dependency of both Folio and Library, with a job of its own."""
    job = _workflow()["jobs"]["naapp-lib"]
    assert job["env"]["PACKAGES"] == "naapp-lib"
    commands = _commands("naapp-lib")
    for target in ("sync", "lint", "test"):
        assert f"make {target}" in commands


def test_ci_runs_naapp_lib_tests_the_way_the_release_gate_does() -> None:
    """naapp-lib's suite runs behind the test egress guard, with Redis.

    The guard forwards the governance browser's RP host to loopback for both
    Chromium and Playwright's driver; the cached-registry tests read REDIS_URL.
    A bare run fails both at setup, so CI would be red for its own reasons
    rather than the code's.
    """
    assert "$(TEST_EGRESS_RUNNER)" in _recipe("test")
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "TEST_EGRESS_RUNNER ?= python3 $(abspath scripts/test-egress/native.py) --" in makefile
    job = _workflow()["jobs"]["naapp-lib"]
    assert "redis" in job["services"]
    assert job["env"]["REDIS_URL"] == "redis://localhost:6379/0"


def test_ci_runs_library_targets_on_library() -> None:
    """Library's jobs run the root targets for naapp/library and nothing else.

    Every step runs make at the repository root, whose lint and test recipes
    enter each package in PACKAGES; a job that ran them elsewhere, or for other
    packages, would leave Library untested while reading as covered.
    """
    workflow = _workflow()
    for name in ("library", "library-e2e"):
        job = workflow["jobs"][name]
        assert job["env"]["PACKAGES"] == "naapp/library"
        assert "'naapp/library'" in job["if"]
        for step in job["steps"]:
            for line in step.get("run", "").splitlines():
                words = line.split()
                if words[:1] == ["make"]:
                    assert "working-directory" not in step, step
                    assert "-C" not in words, step
    for target in ("lint", "test"):
        assert "$(MAKE) -C $$package" in _recipe(target)
