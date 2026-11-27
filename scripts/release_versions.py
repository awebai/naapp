"""Release tag spellings and lockstep runner values."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path


SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
PYTHON_VERSION = rf"{SEMVER}(?:rc[1-9][0-9]*)?"
TAG_PATTERN = re.compile(
    rf"^(?:(aweb-naapp-v|aweb-secrets-runner-v)({PYTHON_VERSION})"
    rf"|(secrets-v|logs-v|folio-v|library-v)({SEMVER}))$"
)


class VersionRefusal(RuntimeError):
    pass


def python_version_key(version: str) -> tuple[int, int, int, int, int]:
    """PEP 440 ordering for our strict X.Y.Z[rcN] release subset."""
    if re.fullmatch(PYTHON_VERSION, version) is None:
        raise VersionRefusal(f"unsupported Python release version {version!r}")
    base, separator, serial = version.partition("rc")
    major, minor, patch = map(int, base.split("."))
    return major, minor, patch, 0 if separator else 1, int(serial) if separator else 0


def tag_parts(tag: str) -> tuple[str, str]:
    matched = TAG_PATTERN.fullmatch(tag)
    if not matched:
        raise VersionRefusal(f"unsupported release tag {tag!r}")
    groups = matched.groups()
    return next((groups[i], groups[i + 1]) for i in (0, 2) if groups[i])


def final_version(version: str) -> str | None:
    matched = re.fullmatch(rf"({SEMVER})rc[1-9][0-9]*", version)
    return matched.group(1) if matched else None


def valid_version(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(PYTHON_VERSION, value) is not None


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise VersionRefusal(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _read(root: Path, revision: str, path: str) -> dict:
    raw = _git(root, "show", f"{revision}:{path}")
    return tomllib.loads(raw)


def manifest_version_at(root: Path, revision: str, path: str) -> str:
    value = _read(root, revision, path)["project"]["version"]
    if not valid_version(value):
        raise VersionRefusal(f"{path} at {revision} has invalid version {value!r}")
    return value


def require_runner_values(root: Path, revision: str) -> None:
    """The two published packages are lockstep, including the runner dependency."""
    runner = _read(root, revision, "naapp/secrets/runner/pyproject.toml")["project"]
    values = _read(root, revision, "naapp/secrets/values/pyproject.toml")["project"]
    dependencies = runner.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise VersionRefusal("runner dependencies are invalid")
    selected = [item for item in dependencies if re.match(
        r"(?i)^aweb[-_.]secrets[-_.]values(?=[\s\[<>=!~;@]|$)", item.strip()
    )]
    if selected != [f"aweb-secrets-values=={values['version']}"]:
        raise VersionRefusal("runner must require the exact lockstep values version")
