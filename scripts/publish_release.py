#!/usr/bin/env python3
"""Publish one already-tested naapp tag, without release branches or GitHub Actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from release_files import locked_files, package_files, verify_assets
from release_versions import (
    PYPI_LANES,
    VersionRefusal as PublishRefusal,
    final_version,
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


def run(
    root: Path,
    *argv: str,
    capture: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 7200,
) -> str:
    completed = subprocess.run(
        argv,
        cwd=root,
        text=True,
        input=input_text,
        capture_output=capture,
        env=env,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise PublishRefusal(f"{' '.join(argv)} failed: {detail}")
    return completed.stdout.strip() if capture else ""


def tag_identity(root: Path, tag: str) -> tuple[str, str, str]:
    prefix, version = tag_parts(tag)
    try:
        source_sha = run(root, "git", "rev-parse", f"{tag}^{{commit}}", timeout=60)
    except PublishRefusal as error:
        raise PublishRefusal(f"local candidate tag {tag} is absent") from error
    manifests = [MANIFESTS[prefix]] if prefix in MANIFESTS else []
    manifests += [f"{directory}/pyproject.toml" for _, directory in PYPI_LANES.get(prefix, ())]
    for manifest in dict.fromkeys(manifests):
        if manifest_version_at(root, source_sha, manifest) != version:
            raise PublishRefusal(
                f"{tag} does not match {manifest} version {manifest_version_at(root, source_sha, manifest)}"
            )
    if prefix == "aweb-secrets-runner-v":
        require_runner_values(root, source_sha)
    return prefix, version, source_sha


def request_json(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode())
            return response.status, value if isinstance(value, dict) else {}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, {}
        raise PublishRefusal(f"registry observation failed for {url}: HTTP {error.code}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise PublishRefusal(f"registry observation failed for {url}: {error}") from error


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def publish_pypi(root: Path, tag: str, assets: Path) -> None:
    """Upload the release files the annotated tag locks, each package in lane order; never build."""
    prefix, version = tag_parts(tag)
    locked = locked_files(root, tag)
    verify_assets(assets, locked)
    lane = {package: package_files(assets, package, version) for package, _ in PYPI_LANES[prefix]}
    if extra := sorted(set(locked) - {name for files in lane.values() for name in files}):
        raise PublishRefusal(f"{tag} locks files its lane does not ship: {', '.join(extra)}")
    if not os.environ.get("UV_PUBLISH_TOKEN", "").strip():
        raise PublishRefusal("UV_PUBLISH_TOKEN is required")
    exact = ("bash", str(Path(__file__).resolve().parent / "pypi-exact-publish.sh"))
    for package, files in lane.items():
        with tempfile.TemporaryDirectory(prefix="aweb-pypi-") as temporary:
            dist = Path(temporary) / "dist"
            dist.mkdir()
            for name in files:
                shutil.copyfile(assets / name, dist / name)
            options = ("--dist", str(dist), "--package", package, "--version", version)
            run(root, *exact, "inspect-staged", *options)
            status, observed = request_json(f"https://pypi.org/pypi/{package}/{version}/json")
            observed_path = Path(temporary) / "observed.json"
            observed_path.write_text(json.dumps(observed), encoding="utf-8")
            plan = run(root, *exact, "plan-publish", *options,
                       "--observed-status", str(status), "--observed-json", str(observed_path))
            uploads = [dist / name for name in plan.splitlines() if name]
            if uploads:
                run(root, "uv", "publish", *(str(path) for path in uploads), capture=False)
            run(root, *exact, "verify-published", *options)

OCI_APPS = {
    "secrets-v": "secrets",
    "logs-v": "logs",
    "folio-v": "folio",
    "library-v": "library",
}
# GHCR path segments: lowercase alphanumerics joined by single separators.
IMAGE_NAMESPACE = re.compile(r"ghcr\.io(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+")


def image_namespace() -> str:
    """Where the app images are published: NAAPP_IMAGE_NAMESPACE (the release
    workflow sets it to its repository owner), else ghcr.io/awebai."""
    namespace = os.environ.get("NAAPP_IMAGE_NAMESPACE", "ghcr.io/awebai")
    if IMAGE_NAMESPACE.fullmatch(namespace) is None:
        raise PublishRefusal(f"NAAPP_IMAGE_NAMESPACE {namespace!r} is not a ghcr.io/<owner>[/<path>] namespace")
    return namespace


def oci_images(prefix: str) -> tuple[str, ...]:
    """The image repositories published by this tag."""
    return (f"{image_namespace()}/{OCI_APPS[prefix]}",)


def build_oci_archive(
    root: Path, prefix: str, version: str, source_sha: str, archive: Path
) -> None:
    """Build the release bytes once; usable by platform qualification without publishing."""
    app = prefix.removesuffix("-v")
    build = [
        "docker", "buildx", "build", "--file", f"naapp/{app}/Dockerfile",
        "--platform", "linux/amd64,linux/arm64", "--provenance=false", "--sbom=false",
        "--build-arg", f"SOURCE_COMMIT={source_sha}",
        "--label", f"org.opencontainers.image.title=aweb {app}",
        "--label", f"org.opencontainers.image.version={version}",
        "--label", f"org.opencontainers.image.revision={source_sha}",
        "--output", f"type=oci,dest={archive}", ".",
    ]
    run(root, *build, capture=False)


# skopeo's whole answer when GHCR has no repository of that name yet, which is
# the state before an image's first publication.
ABSENT_REPOSITORY = re.compile(
    r'time="[^"]*" level=fatal msg="Error listing repository tags: fetching tags list: name unknown"'
)


def published_tags(root: Path, repository: str, first_publication: bool = False) -> list:
    """The repository's tags. GHCR's "name unknown" means "no tags" only for a
    repository the operator explicitly names as a first publication; the answer
    alone never establishes absence. A named repository that lists tags is not
    a first publication."""
    argv = ("skopeo", "list-tags", f"docker://{repository}")
    completed = subprocess.run(argv, cwd=root, text=True, capture_output=True, timeout=120, check=False)
    if completed.returncode:
        absent = not completed.stdout.strip() and ABSENT_REPOSITORY.fullmatch(completed.stderr.strip())
        if absent and first_publication:
            return []
        if absent:
            raise PublishRefusal(f"{' '.join(argv)}: name unknown; a new repository is published only with "
                                 f"--first-publication {repository}")
        detail = (completed.stderr or completed.stdout or "").strip()
        raise PublishRefusal(f"{' '.join(argv)} failed: {detail}")
    tags = json.loads(completed.stdout).get("Tags")
    if not isinstance(tags, list):
        raise PublishRefusal("GHCR returned a malformed tag listing")
    if first_publication:
        raise PublishRefusal(f"not a first publication: {repository} lists {len(tags)} tags")
    return tags


def moves_latest(version: str, recovery: bool) -> bool:
    """Only a final published by its own tag moves latest.

    An rc publishes its immutable version only. So does a recovery: a
    dispatch that publishes an existing tag's image again, rc or final.
    """
    if recovery:
        print("latest unchanged (recovery)")
        return False
    return final_version(version) is None


def publish_oci(root: Path, prefix: str, version: str, source_sha: str, *, recovery: bool = False,
                first_publication: tuple | list = ()) -> None:
    username = os.environ.get("GHCR_USERNAME", "").strip() or os.environ.get(
        "AC_REGISTRY_USERNAME", ""
    ).strip()
    token = os.environ.get("GHCR_TOKEN", "").strip() or os.environ.get(
        "AC_REGISTRY_TOKEN", ""
    ).strip()
    if not username or not token:
        raise PublishRefusal("GHCR_USERNAME and GHCR_TOKEN are required")
    if not shutil.which("skopeo"):
        raise PublishRefusal("skopeo is required for exact OCI publication")
    repositories = list(oci_images(prefix))
    for repository in first_publication:
        if repository not in repositories:
            raise PublishRefusal(f"{prefix} publishes no {repository}; it publishes {', '.join(repositories)}")
    # One decision for every image of the tag: an rc or a recovery moves no latest.
    version_only = not moves_latest(version, recovery)
    with tempfile.TemporaryDirectory(prefix="aweb-oci-") as temporary:
        # Every image of the tag is built and checked before the registry is
        # touched, so a failed build publishes none of them.
        staged = []
        for position, repository in enumerate(oci_images(prefix)):
            staging = Path(temporary) / str(position)
            staging.mkdir()
            archive = staging / "image.oci.tar"
            identities = staging / "identities.json"
            build_oci_archive(root, prefix, version, source_sha, archive)
            run(
                root, "bash", "scripts/oci-exact-publish.sh", "inspect-staged",
                "--archive", str(archive), "--version", version,
                "--source-sha", source_sha, "--out", str(identities),
            )
            index = json.loads(identities.read_text(encoding="utf-8"))["index"]
            staged.append((repository, archive, index))
        run(
            root, "skopeo", "login", "ghcr.io", "--username", username,
            "--password-stdin", input_text=token + "\n", timeout=120,
        )
        for repository, archive, index in staged:
            publish_staged_image(root, repository, archive, index, version, version_only,
                                 repository in first_publication)
        for repository, archive, index in staged:
            run(
                root, "bash", "scripts/oci-exact-publish.sh", "verify-published",
                "--archive", str(archive), "--version", version,
                "--source-sha", source_sha, "--repository", repository,
                *(["--version-only"] if version_only else []),
            )
            receipt = {"repository": repository, "digest": index, "source_sha": source_sha, "version": version}
            print(f"release-index={json.dumps(receipt, sort_keys=True, separators=(',', ':'))}")


def publish_staged_image(
    root: Path, repository: str, archive: Path, staged: str, version: str, version_only: bool,
    first_publication: bool = False,
) -> None:
    tags = published_tags(root, repository, first_publication)
    for image_tag in ((version,) if version_only else (version, "latest")):
        remote = ""
        if image_tag in tags:
            raw = subprocess.run(
                ["skopeo", "inspect", "--raw", f"docker://{repository}:{image_tag}"],
                cwd=root, capture_output=True, check=False, timeout=120,
            )
            if raw.returncode:
                raise PublishRefusal(f"cannot inspect present OCI tag {image_tag}")
            remote = "sha256:" + hashlib.sha256(raw.stdout).hexdigest()
        if remote == staged:
            continue
        if image_tag != "latest" and remote:
            raise PublishRefusal(
                f"immutable {repository}:{image_tag} is {remote}, staged is {staged}"
            )
        run(
            root, "skopeo", "copy", "--all", f"oci-archive:{archive}",
            f"docker://{repository}:{image_tag}", capture=False,
        )


def publish(root: Path, tag: str, *, only: str | None = None, recovery: bool = False,
            first_publication: tuple | list = (), assets: Path | None = None) -> None:
    prefix, version, source_sha = tag_identity(root, tag)
    if only is not None and not (
        (only == "pypi" and prefix in PYPI_LANES)
        or (only == "oci" and prefix in OCI_APPS)
    ):
        raise PublishRefusal(f"{tag} does not publish to {only}")
    if prefix in PYPI_LANES and only != "oci":
        if assets is None:
            raise PublishRefusal(f"{tag} uploads to PyPI only the release files its tag locks: pass --assets")
        publish_pypi(root, tag, assets)
    if prefix not in OCI_APPS or only == "pypi":
        return
    with tempfile.TemporaryDirectory(prefix="aweb-publish-") as temporary:
        checkout = Path(temporary) / "source"
        run(root, "git", "worktree", "add", "--detach", str(checkout), source_sha, timeout=120)
        try:
            publish_oci(checkout, prefix, version, source_sha, recovery=recovery,
                        first_publication=first_publication)
        finally:
            run(root, "git", "worktree", "remove", "--force", str(checkout), timeout=120)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-tag", action="store_true", help="validate the tag against its manifest versions without publishing")
    parser.add_argument("--only", choices=("pypi", "oci"), help="publish only this artifact lane")
    parser.add_argument("--recovery", action="store_true",
                        help="publish an existing tag's images again; never moves latest")
    parser.add_argument("--first-publication", action="append", default=[], metavar="REPOSITORY",
                        help="an image repository this publication creates; repeat for each")
    parser.add_argument("--assets", type=Path,
                        help="a published release's downloaded files, which PyPI receives unchanged")
    parser.add_argument("tag")
    arguments = parser.parse_args(argv)
    try:
        if arguments.check_tag:
            tag_identity(Path.cwd().resolve(), arguments.tag)
        else:
            publish(Path.cwd().resolve(), arguments.tag, only=arguments.only, recovery=arguments.recovery,
                    first_publication=arguments.first_publication, assets=arguments.assets)
    except (
        PublishRefusal,
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"publish refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
