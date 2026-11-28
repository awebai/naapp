from __future__ import annotations

import re
from pathlib import Path

import pytest
from aweb_naapp.ops.jobs import JobAllowlist
from aweb_naapp.ops.render import ProductionConfig, canonical_git_repo

_TASKS_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _TASKS_ROOT.parents[1]
_MANIFEST = _TASKS_ROOT / "ops" / "render-production.json"


def _config() -> ProductionConfig:
    return ProductionConfig.load(_MANIFEST)


def test_the_production_manifest_is_valid_and_builds_tasks_from_the_repository_root() -> None:
    config = _config()

    assert canonical_git_repo(config.repo) == "github.com/awebai/naapp"
    assert config.root_dir == ""
    assert config.docker_context == "."
    assert (_REPO_ROOT / config.dockerfile_path).resolve() == _TASKS_ROOT / "Dockerfile"


def test_the_render_template_builds_the_way_the_manifest_does() -> None:
    config = _config()
    template = (_TASKS_ROOT / "render.yaml").read_text(encoding="utf-8")

    def value(key: str) -> str:
        match = re.search(rf"^\s+{key}:\s*(\S*)\s*$", template, re.MULTILINE)
        assert match, f"render.yaml has no {key}"
        return match.group(1).strip("'\"")

    assert value("dockerfilePath") == config.dockerfile_path
    assert value("dockerContext") == config.docker_context
    assert value("rootDir") == config.root_dir
    assert value("healthCheckPath") == config.health_path


_MAKEFILE = (_TASKS_ROOT / "Makefile").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "command", ["status", "deploy", "wait", "verify", "rollback", "suspend", "resume", "job"]
)
def test_each_production_operation_is_a_make_target_running_the_shared_tool(command: str) -> None:
    recipe = re.search(rf"^prod-{command}:\n((?:\t.*\n)+)", _MAKEFILE, re.MULTILINE)
    assert recipe, f"Makefile has no prod-{command} target"
    assert re.search(
        rf"uv run python -m aweb_naapp\.ops --config \$\(PROD_CONFIG\) {command}\b", recipe.group(1)
    ), recipe.group(1)


def _image_python() -> str:
    """The interpreter the image installs the app into; a bare python is the base image's."""
    dockerfile = (_TASKS_ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"UV_PROJECT_ENVIRONMENT=(\S+)", dockerfile)
    assert match, "Dockerfile sets no UV_PROJECT_ENVIRONMENT"
    return f"{match.group(1)}/bin/python"


def test_the_one_off_jobs_are_exactly_the_reviewed_board_move_help() -> None:
    allowlist = JobAllowlist.load(_TASKS_ROOT / "ops" / "one-off-jobs.json")

    assert sorted(allowlist.jobs) == ["board-move-help"]
    assert allowlist.argv("board-move-help", {}) == [
        _image_python(), "-m", "aweb_tasks.board_move", "--help",
    ]
