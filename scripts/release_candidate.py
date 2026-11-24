#!/usr/bin/env python3
"""Run this repository's complete release checks and create requested local tags."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from release_versions import (
    VersionRefusal as CandidateRefusal,
    require_open_lane,
    require_runner_values,
    tag_parts,
    valid_version,
)

MANIFESTS = {
    "aweb-naapp-v": "naapp-lib/pyproject.toml",
    "secrets-v": "naapp/secrets/pyproject.toml",
    "logs-v": "naapp/logs/pyproject.toml",
    "folio-v": "naapp/folio/pyproject.toml",
    "library-v": "naapp/library/pyproject.toml",
    "aweb-secrets-runner-v": "naapp/secrets/runner/pyproject.toml",
}
# Packages a tag lane publishes together with its manifest's, at the same version.
LOCKSTEP = {
    "aweb-naapp-v": ["naapp-lib/testing/pyproject.toml"],
    "aweb-secrets-runner-v": ["naapp/secrets/values/pyproject.toml"],
    "secrets-v": ["naapp/secrets/e2e/pyproject.toml"],
}


def run(
    root: Path,
    *argv: str,
    capture: bool = True,
    env: dict[str, str] | None = None,
    timeout: int = 7200,
) -> str:
    completed = subprocess.run(
        argv,
        cwd=root,
        text=True,
        capture_output=capture,
        env=env,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise CandidateRefusal(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip() if capture else ""


def manifest_version(root: Path, path: str) -> str:
    with (root / path).open("rb") as handle:
        value = tomllib.load(handle)["project"]["version"]
    if not valid_version(value):
        raise CandidateRefusal(f"{path} has invalid version {value!r}")
    return value


def validate_tags(root: Path, tags: list[str]) -> tuple[str, dict[str, str]]:
    if not tags:
        raise CandidateRefusal("at least one release tag is required")
    if len(tags) != len(set(tags)):
        raise CandidateRefusal("release tags contain a duplicate")
    if run(root, "git", "status", "--porcelain", "--untracked-files=all"):
        raise CandidateRefusal("the candidate checkout is dirty or has untracked files")
    source_sha = run(root, "git", "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise CandidateRefusal("git did not return a full source SHA")
    versions: dict[str, str] = {}
    for tag in tags:
        prefix, version = tag_parts(tag)
        require_open_lane(prefix)
        if prefix in versions:
            raise CandidateRefusal(f"more than one {prefix} tag was requested")
        versions[prefix] = version
        for manifest in [MANIFESTS[prefix], *LOCKSTEP.get(prefix, [])]:
            if manifest_version(root, manifest) != version:
                raise CandidateRefusal(
                    f"{tag} does not match {manifest} version {manifest_version(root, manifest)}"
                )
        if prefix == "aweb-secrets-runner-v":
            require_runner_values(root, source_sha)
        if run(root, "git", "tag", "--list", tag):
            raise CandidateRefusal(f"local tag {tag} already exists; tags are never moved")
    return source_sha, versions


def create_candidate(root: Path, tags: list[str]) -> str:
    run(root, "git", "fetch", "--quiet", "origin", "main", "+refs/tags/*:refs/tags/*")
    source_sha, _versions = validate_tags(root, tags)
    run(root, "git", "merge-base", "--is-ancestor", source_sha, "origin/main")
    # Every package and journey, whatever narrowing the caller's environment carries.
    run(root, "make", "release-checks", capture=False)
    if run(root, "git", "rev-parse", "HEAD") != source_sha:
        raise CandidateRefusal("checkout HEAD changed while the release checks ran")
    if run(root, "git", "status", "--porcelain", "--untracked-files=all"):
        raise CandidateRefusal("the release checks left the candidate checkout dirty")
    for tag in tags:
        message = f"naapp {tag}\n\nRelease checks passed for {source_sha}."
        run(root, "git", "tag", "-a", tag, source_sha, "-m", message)
    print(f"candidate PASSED: {source_sha}")
    for tag in tags:
        print(f"  {tag}")
    print("publish each tag separately:")
    for tag in tags:
        print(f"  git push origin refs/tags/{tag}")
    return source_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tags", nargs="+")
    arguments = parser.parse_args(argv)
    try:
        create_candidate(Path.cwd().resolve(), arguments.tags)
    except (
        CandidateRefusal,
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"candidate refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
