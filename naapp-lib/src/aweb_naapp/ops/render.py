"""Production operations for a naapp hosted on Render, pinned by its ops manifest.

Every command first checks the live Render service against the app's
``ops/render-production.json`` and fails closed on any difference. Deploys ship
one exact commit that is the tip of the configured remote branch. Rollback
redeploys an earlier deploy's artifact through Render's rollback; it never
rebuilds an old commit, which would build under the service's current settings.

Standard library only. Nothing here is imported by a naapp at runtime.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import MISSING, dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

API_BASE = "https://api.render.com/v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DEPLOY_RE = re.compile(r"^dep-[a-z0-9]+$")
SERVICE_RE = re.compile(r"^srv-[a-z0-9]+$")
IN_PROGRESS_STATUSES = {
    "created",
    "queued",
    "build_in_progress",
    "update_in_progress",
    "pre_deploy_in_progress",
}
FAILURE_STATUSES = {"build_failed", "update_failed", "pre_deploy_failed", "canceled"}
TERMINAL_STATUSES = {"live", "deactivated", *FAILURE_STATUSES}
KNOWN_STATUSES = {*IN_PROGRESS_STATUSES, *TERMINAL_STATUSES}
ROLLBACK_ARTIFACT_STATUSES = {"live", "deactivated"}
HEALTH_TIMEOUT_SECONDS = 20.0
# A deploy is ready when both URLs answer its build. Until then a service that is
# still starting, or still serving the previous build, is asked again.
HEALTH_READINESS_SECONDS = 90.0
HEALTH_RETRY_SECONDS = 5.0
HEALTH_BODY_LIMIT = 16_384
NOT_YET_HEALTH_STATUSES = {404, 408, 425, 429, 500, 502, 503, 504}
# The deploy check names itself: Cloudflare on a public zone refuses Python's default
# User-Agent with its browser-signature 403, which would hide a healthy service and
# make a real outage look the same.
HEALTH_USER_AGENT = "aweb-naapp-deploy-check/1.0"
# Git variables that would point git at a repository other than the one named.
GIT_REPOSITORY_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"}
)


class OpsError(RuntimeError):
    pass


def _relative_inside_repository(value: str, *, allow_root: bool) -> bool:
    if value in {"", "."}:
        return allow_root
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


@dataclass(frozen=True)
class ProductionConfig:
    service_id: str
    service_name: str
    region: str
    repo: str
    branch: str
    root_dir: str
    dockerfile_path: str
    docker_context: str
    origin_url: str
    public_url: str
    health_path: str
    # What the app's health reports as its ``service`` (naapp-lib's runtime health
    # answers the app id).
    app_id: str
    # Render's service slug, as the running deploy reports it (RENDER_SERVICE_NAME).
    service_slug: str | None = None

    @classmethod
    def load(cls, path: Path) -> ProductionConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpsError(f"invalid production config: {path}") from exc
        required = {item.name for item in fields(cls) if item.default is MISSING}
        optional = {item.name for item in fields(cls)} - required
        if not isinstance(raw, dict) or not required <= set(raw) <= required | optional:
            raise OpsError(
                f"production config must have the fields {sorted(required)}"
                f" and may have {sorted(optional)}"
            )
        if not all(isinstance(value, str) for value in raw.values()):
            raise OpsError("production config values must be strings")
        config = cls(**raw)
        if not SERVICE_RE.fullmatch(config.service_id):
            raise OpsError("production config has an invalid Render service ID")
        if not config.origin_url.startswith("https://") or not config.public_url.startswith(
            "https://"
        ):
            raise OpsError("production URLs must use https")
        if not config.health_path.startswith("/"):
            raise OpsError("health_path must be absolute")
        if not _relative_inside_repository(config.root_dir, allow_root=True):
            raise OpsError("root_dir must be the repository root or a relative path inside it")
        if not _relative_inside_repository(config.dockerfile_path, allow_root=False):
            raise OpsError("dockerfile_path must be a relative path inside the repository")
        if not _relative_inside_repository(config.docker_context, allow_root=True):
            raise OpsError("docker_context must be a relative path inside the repository")
        return config


def load_api_key(path: Path) -> str:
    try:
        if path.is_symlink():
            raise OpsError("Render env file must not be a symlink")
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError as exc:
        raise OpsError(f"Render env file not found: {path}") from exc
    if mode & 0o077:
        raise OpsError(f"Render env file must not be group/world accessible (mode {mode:03o})")
    values = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if line.startswith("RENDER_API_KEY="):
            values.append(line.split("=", 1)[1].strip().strip("'\""))
    if len(values) != 1 or not values[0]:
        raise OpsError("Render env file must contain exactly one nonempty RENDER_API_KEY")
    return values[0]


class RenderClient:
    def __init__(self, api_key: str, *, base_url: str = API_BASE, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            method=method,
            data=data,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise OpsError(f"Render API {method} {path} failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise OpsError(f"Render API {method} {path} failed: {type(exc).__name__}") from exc

    def service(self, service_id: str) -> dict[str, Any]:
        return _unwrap(self.request("GET", f"/services/{service_id}"), "service")

    def deploys(self, service_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        response = self.request("GET", f"/services/{service_id}/deploys?limit={limit}")
        return [_unwrap(item, "deploy") for item in response]

    def deploy_by_id(self, service_id: str, deploy_id: str) -> dict[str, Any]:
        return _unwrap(self.request("GET", f"/services/{service_id}/deploys/{deploy_id}"), "deploy")

    def deploy_commit(self, service_id: str, commit: str) -> dict[str, Any]:
        return _unwrap(
            self.request(
                "POST",
                f"/services/{service_id}/deploys",
                {"clearCache": "clear", "commitId": commit},
            ),
            "deploy",
        )

    def rollback(self, service_id: str, deploy_id: str) -> dict[str, Any]:
        return _unwrap(
            self.request("POST", f"/services/{service_id}/rollback", {"deployId": deploy_id}),
            "deploy",
        )


def _unwrap(value: Any, key: str) -> Any:
    if isinstance(value, dict) and key in value:
        return value[key]
    return value


def deploy_commit(deploy: dict[str, Any]) -> str:
    commit = deploy.get("commit") or {}
    return str(commit.get("id") or deploy.get("commitId") or "")


def safe_deploy(deploy: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": deploy.get("id"),
        "status": deploy.get("status"),
        "commit": deploy_commit(deploy),
        "created_at": deploy.get("createdAt"),
        "finished_at": deploy.get("finishedAt"),
    }


_ABSENT = object()


def validate_service(service: dict[str, Any], config: ProductionConfig) -> None:
    details = service.get("serviceDetails") or {}
    docker = details.get("envSpecificDetails") or {}
    # An absent rootDir and an empty one both mean the repository root; null does not.
    root_dir = service.get("rootDir", _ABSENT)
    observed = {
        "id": service.get("id"),
        "name": service.get("name"),
        "region": details.get("region"),
        "repo": service.get("repo"),
        "branch": service.get("branch"),
        "rootDir": "" if root_dir is _ABSENT else root_dir,
        "dockerfilePath": docker.get("dockerfilePath"),
        "dockerContext": docker.get("dockerContext"),
        "url": details.get("url"),
        "suspended": service.get("suspended"),
        "autoDeploy": service.get("autoDeploy"),
    }
    expected = {
        "id": config.service_id,
        "name": config.service_name,
        "region": config.region,
        "repo": config.repo,
        "branch": config.branch,
        "rootDir": config.root_dir,
        "dockerfilePath": config.dockerfile_path,
        "dockerContext": config.docker_context,
        "url": config.origin_url,
        "suspended": "not_suspended",
        "autoDeploy": "no",
    }
    mismatches = [
        f"{key} (observed {observed[key]!r}, expected {expected[key]!r})"
        for key in expected
        if observed[key] != expected[key]
    ]
    if mismatches:
        raise OpsError("Render service does not match production config: " + "; ".join(mismatches))


def require_commit(value: str, field: str = "commit") -> str:
    if not COMMIT_RE.fullmatch(value):
        raise OpsError(f"{field} must be a full lowercase 40-character Git SHA")
    return value


def require_deploy_id(value: str, field: str = "deploy ID") -> str:
    if not DEPLOY_RE.fullmatch(value):
        raise OpsError(f"{field} is invalid: Render deploy IDs look like dep-...")
    return value


def canonical_git_repo(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("git@github.com:"):
        normalized = f"github.com/{normalized.removeprefix('git@github.com:')}"
    elif "://" in normalized:
        normalized = normalized.split("://", 1)[1]
    if normalized.startswith("ssh.github.com:443/"):
        normalized = "github.com/" + normalized.removeprefix("ssh.github.com:443/")
    if normalized.startswith("git@github.com/"):
        normalized = "github.com/" + normalized.removeprefix("git@github.com/")
    return normalized.removesuffix(".git").rstrip("/")


def _run_git(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run git against the repository named in args, never an inherited Git context."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_REPOSITORY_ENV}
    return subprocess.run(["git", *args], env=env, **kwargs)


def _git_ok(repo_root: Path, *args: str) -> bool:
    return (
        _run_git(
            ["-C", str(repo_root), *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode
        == 0
    )


def _fetched_tip(repo_root: Path, url: str, branch: str, *, which: str) -> str:
    """``branch`` of ``url`` as the remote has it now, with its history fetched."""
    remote_ref = f"refs/heads/{branch}"
    try:
        _run_git(
            ["-C", str(repo_root), "fetch", "--quiet", "--no-tags", url, remote_ref],
            check=True,
            capture_output=True,
        )
        return _run_git(
            ["-C", str(repo_root), "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise OpsError(f"failed to read {remote_ref} from the {which} remote") from exc


def _on_branch(repo_root: Path, commit: str, tip: str) -> bool:
    return _git_ok(repo_root, "merge-base", "--is-ancestor", commit, tip)


def verify_git_target(
    repo_root: Path,
    commit: str,
    *,
    expected_repo: str,
    branch: str,
    live_commit: str,
    from_repository: str | None = None,
) -> None:
    """Require ``commit`` to be on the configured branch, read from the remote
    itself now, and strictly ahead of the live commit: a deploy never moves
    production backwards, which is rollback's job, with a built artifact.

    A first deploy after the service moved repositories names the repository it
    moved from: the live commit must then be absent from the configured branch
    and on the previous repository's branch, the only case where the forward
    check does not apply."""
    require_commit(commit)
    require_commit(live_commit, "live commit")
    try:
        origin_url = _run_git(
            ["-C", str(repo_root), "remote", "get-url", "origin"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise OpsError("failed to read the local origin remote") from exc
    if canonical_git_repo(origin_url) != canonical_git_repo(expected_repo):
        raise OpsError("local origin remote is not the configured production repository")
    tip = _fetched_tip(repo_root, origin_url, branch, which="configured")
    if not _on_branch(repo_root, commit, tip):
        raise OpsError(f"approved target {commit} is not on {branch} of the configured repository")
    live_here = _on_branch(repo_root, live_commit, tip)
    if from_repository is not None:
        if live_here:
            raise OpsError(
                f"live commit {live_commit} is in the configured repository: nothing moved"
            )
        previous_tip = _fetched_tip(repo_root, from_repository, branch, which="previous")
        if not _on_branch(repo_root, live_commit, previous_tip):
            raise OpsError(
                f"live commit {live_commit} is not on {branch} of the previous repository"
            )
        return
    if not live_here:
        raise OpsError(
            f"live commit {live_commit} is not on {branch} of the configured repository; a first "
            "deploy after the service moved repositories names it with --from-repository"
        )
    if commit == live_commit or not _on_branch(repo_root, live_commit, commit):
        raise OpsError(f"approved target {commit} is not ahead of the live commit {live_commit}")


def current_live(deploys: list[dict[str, Any]]) -> dict[str, Any]:
    unknown = [deploy for deploy in deploys if deploy.get("status") not in KNOWN_STATUSES]
    if unknown:
        raise OpsError(
            f"refusing unknown Render deploy state {unknown[0].get('status')!r}: "
            f"{unknown[0].get('id')}"
        )
    active = [deploy for deploy in deploys if deploy.get("status") in IN_PROGRESS_STATUSES]
    if active:
        raise OpsError(f"another deploy is active: {active[0].get('id')}")
    live = [deploy for deploy in deploys if deploy.get("status") == "live"]
    if len(live) != 1:
        raise OpsError(f"expected exactly one live deploy, found {len(live)}")
    return live[0]


def require_deploy(deploy: dict[str, Any], *, deploy_id: str, commit: str) -> None:
    if deploy.get("id") != deploy_id or deploy_commit(deploy) != commit:
        raise OpsError("deploy ID/commit does not match the approved artifact")


def require_rollback_artifact(deploy: dict[str, Any], *, deploy_id: str, commit: str) -> None:
    require_deploy(deploy, deploy_id=deploy_id, commit=commit)
    if deploy.get("status") not in ROLLBACK_ARTIFACT_STATUSES:
        raise OpsError(f"rollback artifact is not known-good: {deploy.get('status')}")


def wait_for_deploy(
    client: RenderClient,
    config: ProductionConfig,
    deploy_id: str,
    expected_commit: str,
    *,
    timeout_seconds: float = 900,
    interval_seconds: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        deploy = client.deploy_by_id(config.service_id, deploy_id)
        if deploy_commit(deploy) != expected_commit:
            raise OpsError("Render deploy commit changed while waiting")
        status = str(deploy.get("status") or "")
        if status == "live":
            return deploy
        if status in FAILURE_STATUSES or status == "deactivated":
            raise OpsError(f"Render deploy entered failure state: {status}")
        if status not in IN_PROGRESS_STATUSES:
            raise OpsError(f"Render deploy entered unknown state: {status!r}")
        sleep(interval_seconds)
    raise OpsError(f"timed out waiting for Render deploy {deploy_id}")


class _HealthNotYet(OpsError):
    """A health answer that may still become this deploy's: asked again until ready."""


class _NoRedirects(HTTPRedirectHandler):
    """Health is read at the exact URL: a redirect is not that surface answering."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _health_answer(url: str) -> tuple[int, bytes]:
    request = Request(
        url, method="GET", headers={"Accept": "application/json", "User-Agent": HEALTH_USER_AGENT}
    )
    try:
        with build_opener(_NoRedirects()).open(request, timeout=HEALTH_TIMEOUT_SECONDS) as response:
            return response.status, response.read(HEALTH_BODY_LIMIT + 1)
    except HTTPError as exc:
        return exc.code, b""
    except (URLError, TimeoutError, OSError) as exc:
        raise _HealthNotYet(f"health request to {url} failed: {type(exc).__name__}") from exc


def _health_identity(config: ProductionConfig, url: str, commit: str) -> dict[str, str]:
    """The service and build ``url`` answers, when they are this deploy's."""
    status, body = _health_answer(url)
    if status in NOT_YET_HEALTH_STATUSES:
        raise _HealthNotYet(f"health at {url} answered HTTP {status}")
    if status != 200:
        raise OpsError(f"health at {url} answered HTTP {status}")
    if len(body) > HEALTH_BODY_LIMIT:
        raise OpsError(f"health at {url} answered more than {HEALTH_BODY_LIMIT} bytes")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _HealthNotYet(f"health at {url} did not answer JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise _HealthNotYet(f"health at {url} is not ok")
    if payload.get("service") != config.app_id:
        raise OpsError(f"health at {url} is not {config.app_id}'s")
    build = payload.get("build")
    sha = build.get("git_sha") if isinstance(build, dict) else None
    if not isinstance(sha, str) or not COMMIT_RE.fullmatch(sha):
        raise OpsError(f"health at {url} reports no build identity")
    if sha != commit:
        raise _HealthNotYet(f"health at {url} answers build {sha}, not {commit}")
    deployment = payload.get("deployment")
    if deployment is not None and (
        not isinstance(deployment, dict) or deployment.get("commit") != sha
    ):
        raise OpsError(f"health at {url} names a deployment of another build")
    return {"service": payload["service"], "git_sha": sha}


DEPLOYMENT_IDENTITY_FIELDS = (
    "service_id",
    "service_name",
    "hostname",
    "origin_url",
    "repo",
    "branch",
    "commit",
)


def public_identity(config: ProductionConfig) -> dict[str, Any]:
    """The running deploy as its public health URL reports it, with no Render key:
    Render's deployment block must name the pinned service, slug, host, repository
    and branch, and the build the health reports."""
    url = f"{config.public_url.rstrip('/')}{config.health_path}"
    status, body = _health_answer(url)
    if status != 200:
        raise OpsError(f"public identity at {url} answered HTTP {status}")
    if len(body) > HEALTH_BODY_LIMIT:
        raise OpsError(f"public identity at {url} answered more than {HEALTH_BODY_LIMIT} bytes")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpsError(f"public identity at {url} did not answer JSON") from exc
    deployment = payload.get("deployment") if isinstance(payload, dict) else None
    if not isinstance(deployment, dict) or set(deployment) != set(DEPLOYMENT_IDENTITY_FIELDS):
        raise OpsError(f"public identity at {url} has no Render deployment block")
    if not all(isinstance(value, str) and value for value in deployment.values()):
        raise OpsError("public identity deployment fields must be nonempty strings")
    build = payload.get("build")
    if not isinstance(build, dict) or deployment["commit"] != build.get("git_sha"):
        raise OpsError("public identity deployment commit is not the build its health reports")
    expected = {
        "service_id": config.service_id,
        "hostname": urlsplit(config.origin_url).hostname or "",
        "origin_url": config.origin_url.rstrip("/"),
        "repo": canonical_git_repo(config.repo).removeprefix("github.com/"),
        "branch": config.branch,
    }
    if config.service_slug is not None:
        expected["service_name"] = config.service_slug
    for name, value in expected.items():
        if deployment[name] != value:
            raise OpsError(
                f"public identity {name} mismatch: observed {deployment[name]}, expected {value}"
            )
    return {"url": url, "deployment": deployment}


def verify_health_surfaces(
    config: ProductionConfig,
    commit: str,
    *,
    readiness_timeout_seconds: float = HEALTH_READINESS_SECONDS,
    retry_interval_seconds: float = HEALTH_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, dict[str, str]]:
    """Wait until the origin and public health URLs both answer ``commit``'s build.

    A not-yet answer (a transient status, the previous build still serving, a
    body that is not JSON yet) is asked again until the readiness window ends;
    an answer that cannot be this deploy's (another service, no build identity,
    a redirect, an oversized body) fails at once."""
    urls = [
        f"{base.rstrip('/')}{config.health_path}"
        for base in dict.fromkeys((config.origin_url, config.public_url))
    ]
    deadline = monotonic() + readiness_timeout_seconds
    attempts = 0
    while True:
        attempts += 1
        try:
            return {url: _health_identity(config, url, commit) for url in urls}
        except _HealthNotYet as not_yet:
            if monotonic() + retry_interval_seconds > deadline:
                raise OpsError(
                    f"health readiness failed after {readiness_timeout_seconds:g} seconds "
                    f"and {attempts} attempts: {not_yet}"
                ) from not_yet
            sleep(retry_interval_seconds)


def confirm_repository_change(from_repository: str | None, confirm_from_repository: str) -> None:
    """A deploy across a repository move repeats the previous repository to confirm it."""
    named = canonical_git_repo(from_repository) if from_repository else ""
    confirmed = canonical_git_repo(confirm_from_repository) if confirm_from_repository else ""
    if named != confirmed:
        raise OpsError(
            "repository move refused: the previous repository confirmation does not match"
        )


def confirm_apply(config: ProductionConfig, *, apply: bool, confirm_service_id: str) -> None:
    if not apply:
        raise OpsError("mutation refused: pass --apply (the Makefile requires APPLY=1)")
    if confirm_service_id != config.service_id:
        raise OpsError("mutation refused: exact service-ID confirmation does not match")


def status(client: RenderClient, config: ProductionConfig) -> dict[str, Any]:
    service = client.service(config.service_id)
    validate_service(service, config)
    live = current_live(client.deploys(config.service_id))
    return {
        "service": {
            "id": service.get("id"),
            "name": service.get("name"),
            "origin_url": config.origin_url,
            "public_url": config.public_url,
        },
        "live_deploy": safe_deploy(live),
    }


def deploy(
    client: RenderClient,
    config: ProductionConfig,
    *,
    commit: str,
    rollback_deploy_id: str,
    rollback_commit: str,
    repo_root: Path,
    from_repository: str | None = None,
    timeout_seconds: float = 900,
    interval_seconds: float = 10.0,
    readiness_timeout_seconds: float = HEALTH_READINESS_SECONDS,
    retry_interval_seconds: float = HEALTH_RETRY_SECONDS,
) -> dict[str, Any]:
    """Deploy ``commit``; ``rollback_commit`` is the live commit it moves forward from."""
    require_commit(commit)
    require_commit(rollback_commit, "rollback commit")
    require_deploy_id(rollback_deploy_id, "rollback deploy ID")
    validate_service(client.service(config.service_id), config)
    live = current_live(client.deploys(config.service_id))
    require_rollback_artifact(live, deploy_id=rollback_deploy_id, commit=rollback_commit)
    # The rollback artifact is the live deploy, so its commit is the one to move forward from.
    verify_git_target(
        repo_root,
        commit,
        expected_repo=config.repo,
        branch=config.branch,
        live_commit=rollback_commit,
        from_repository=from_repository,
    )
    created = client.deploy_commit(config.service_id, commit)
    deploy_id = require_deploy_id(str(created.get("id") or ""), "created deploy ID")
    if deploy_commit(created) != commit:
        raise OpsError("Render created a deploy for the wrong commit")
    finished = wait_for_deploy(
        client,
        config,
        deploy_id,
        commit,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    require_deploy(
        current_live(client.deploys(config.service_id)), deploy_id=deploy_id, commit=commit
    )
    health = verify_health_surfaces(
        config,
        commit,
        readiness_timeout_seconds=readiness_timeout_seconds,
        retry_interval_seconds=retry_interval_seconds,
    )
    return {"deploy": safe_deploy(finished), "rollback": safe_deploy(live), "health": health}


def wait(
    client: RenderClient,
    config: ProductionConfig,
    *,
    deploy_id: str,
    commit: str,
    timeout_seconds: float = 900,
    interval_seconds: float = 10.0,
) -> dict[str, Any]:
    require_deploy_id(deploy_id)
    require_commit(commit)
    validate_service(client.service(config.service_id), config)
    finished = wait_for_deploy(
        client,
        config,
        deploy_id,
        commit,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    return {"deploy": safe_deploy(finished)}


def verify(
    client: RenderClient,
    config: ProductionConfig,
    *,
    deploy_id: str,
    commit: str,
    readiness_timeout_seconds: float = HEALTH_READINESS_SECONDS,
    retry_interval_seconds: float = HEALTH_RETRY_SECONDS,
) -> dict[str, Any]:
    """Require the deploy to be the live artifact, and both health URLs to answer its build.
    The readiness timing is verify_health_surfaces'."""
    require_deploy_id(deploy_id)
    require_commit(commit)
    validate_service(client.service(config.service_id), config)
    artifact = client.deploy_by_id(config.service_id, deploy_id)
    require_deploy(artifact, deploy_id=deploy_id, commit=commit)
    require_deploy(
        current_live(client.deploys(config.service_id)), deploy_id=deploy_id, commit=commit
    )
    return {
        "deploy": safe_deploy(artifact),
        "health": verify_health_surfaces(
            config,
            commit,
            readiness_timeout_seconds=readiness_timeout_seconds,
            retry_interval_seconds=retry_interval_seconds,
        ),
    }


def rollback(
    client: RenderClient,
    config: ProductionConfig,
    *,
    rollback_deploy_id: str,
    rollback_commit: str,
    current_deploy_id: str,
    current_commit: str,
    timeout_seconds: float = 900,
    interval_seconds: float = 10.0,
    readiness_timeout_seconds: float = HEALTH_READINESS_SECONDS,
    retry_interval_seconds: float = HEALTH_RETRY_SECONDS,
) -> dict[str, Any]:
    """Redeploy an earlier deploy's built artifact. There is deliberately no path that
    deploys a commit here: a commit deploy rebuilds under the current service settings."""
    require_deploy_id(rollback_deploy_id, "rollback deploy ID")
    require_commit(rollback_commit, "rollback commit")
    require_deploy_id(current_deploy_id, "current live deploy ID")
    require_commit(current_commit, "current live commit")
    if current_deploy_id == rollback_deploy_id:
        raise OpsError("current live deploy and rollback artifact must be different")
    validate_service(client.service(config.service_id), config)
    live = current_live(client.deploys(config.service_id))
    require_deploy(live, deploy_id=current_deploy_id, commit=current_commit)
    artifact = client.deploy_by_id(config.service_id, rollback_deploy_id)
    require_rollback_artifact(artifact, deploy_id=rollback_deploy_id, commit=rollback_commit)
    created = client.rollback(config.service_id, rollback_deploy_id)
    created_id = require_deploy_id(str(created.get("id") or ""), "created rollback deploy ID")
    if deploy_commit(created) != rollback_commit:
        raise OpsError("Render created a rollback for the wrong commit")
    finished = wait_for_deploy(
        client,
        config,
        created_id,
        rollback_commit,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    require_deploy(
        current_live(client.deploys(config.service_id)),
        deploy_id=created_id,
        commit=rollback_commit,
    )
    health = verify_health_surfaces(
        config,
        rollback_commit,
        readiness_timeout_seconds=readiness_timeout_seconds,
        retry_interval_seconds=retry_interval_seconds,
    )
    return {"rollback_deploy": safe_deploy(finished), "health": health}
