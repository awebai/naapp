#!/usr/bin/env python3
"""Tag a commit of main that a fully green CI run tested, with the requested local tags."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from release_versions import (
    VersionRefusal as CandidateRefusal,
    manifest_version_at,
    require_runner_values,
    tag_parts,
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


# The workflow whose runs qualify a commit, and its single required check.
CI_WORKFLOW = "ci.yml"
RESULT_JOB = "naapp CI"


def validate_tags(root: Path, tags: list[str], source: str = "HEAD") -> tuple[str, dict[str, str]]:
    if not tags:
        raise CandidateRefusal("at least one release tag is required")
    if len(tags) != len(set(tags)):
        raise CandidateRefusal("release tags contain a duplicate")
    source_sha = run(root, "git", "rev-parse", "--verify", "--quiet", f"{source}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise CandidateRefusal("git did not return a full source SHA")
    versions: dict[str, str] = {}
    for tag in tags:
        prefix, version = tag_parts(tag)
        if prefix in versions:
            raise CandidateRefusal(f"more than one {prefix} tag was requested")
        versions[prefix] = version
        for manifest in [MANIFESTS[prefix], *LOCKSTEP.get(prefix, [])]:
            if manifest_version_at(root, source_sha, manifest) != version:
                raise CandidateRefusal(
                    f"{tag} does not match {manifest} version "
                    f"{manifest_version_at(root, source_sha, manifest)}"
                )
        if prefix == "aweb-secrets-runner-v":
            require_runner_values(root, source_sha)
        if run(root, "git", "tag", "--list", tag):
            raise CandidateRefusal(f"local tag {tag} already exists; tags are never moved")
    return source_sha, versions


def ci_runs(root: Path, source_sha: str) -> list[dict]:
    """Every run of the CI workflow on ``source_sha``, with its jobs, read through gh."""
    repository = run(root, "gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    listed = json.loads(run(
        root, "gh", "api", f"repos/{repository}/actions/workflows/{CI_WORKFLOW}/runs?head_sha={source_sha}&per_page=100"
    ))
    runs = []
    for item in listed["workflow_runs"]:
        jobs = json.loads(run(root, "gh", "api", f"repos/{repository}/actions/runs/{item['id']}/jobs?per_page=100"))
        runs.append({
            **{key: item[key] for key in
               ("id", "head_sha", "event", "head_branch", "status", "conclusion", "html_url")},
            "jobs": [{"name": job["name"], "conclusion": job["conclusion"]} for job in jobs["jobs"]],
        })
    return runs


def qualifying_run(runs: list[dict], source_sha: str) -> dict:
    """The run that qualifies ``source_sha``: a completed CI run of exactly that
    commit, pushed to main or started by hand (both test every package), in which
    every job succeeded, the required check among them. A skipped job is a
    package the run did not test, so it does not qualify."""
    reasons = []
    for candidate in runs:
        if candidate["head_sha"] != source_sha:
            continue
        label = f"run {candidate['id']}"
        event, branch = candidate["event"], candidate["head_branch"]
        if not (event == "push" and branch == "main") and event != "workflow_dispatch":
            reasons.append(f"{label}: a {event} run on {branch} does not test every package")
        elif candidate["status"] != "completed" or candidate["conclusion"] != "success":
            reasons.append(f"{label}: {candidate['status']} {candidate['conclusion'] or ''}".rstrip())
        elif unsuccessful := [f"{job['name']} {job['conclusion']}" for job in candidate["jobs"]
                              if job["conclusion"] != "success"]:
            reasons.append(f"{label}: " + ", ".join(unsuccessful))
        elif RESULT_JOB not in {job["name"] for job in candidate["jobs"]}:
            reasons.append(f"{label}: no {RESULT_JOB} job")
        else:
            return candidate
    detail = "".join(f"\n  {reason}" for reason in reasons) or ": no CI run of it"
    raise CandidateRefusal(f"no CI run qualifies {source_sha}{detail}")


def create_candidate(
    root: Path,
    tags: list[str],
    source: str = "HEAD",
    runs: Callable[[Path, str], list[dict]] = ci_runs,
) -> str:
    run(root, "git", "fetch", "--quiet", "origin", "main", "+refs/tags/*:refs/tags/*")
    source_sha, _versions = validate_tags(root, tags, source)
    on_main = subprocess.run(["git", "merge-base", "--is-ancestor", source_sha, "origin/main"],
                             cwd=root, capture_output=True, check=False)
    if on_main.returncode:
        raise CandidateRefusal(f"{source_sha} is not on origin/main")
    qualified = qualifying_run(runs(root, source_sha), source_sha)
    for tag in tags:
        message = f"naapp {tag}\n\nCI run {qualified['html_url']} passed every job for {source_sha}."
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
    parser.add_argument("--source", default="HEAD", help="the commit of main to tag (default HEAD)")
    parser.add_argument("tags", nargs="+")
    arguments = parser.parse_args(argv)
    try:
        create_candidate(Path.cwd().resolve(), arguments.tags, arguments.source)
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
