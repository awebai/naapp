from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from aweb_naapp.ops import render
from aweb_naapp.ops.render import OpsError, ProductionConfig, RenderClient

SERVICE_ID = "srv-folio0000000000000000"
LIVE_COMMIT = "a" * 40
LIVE_DEPLOY = "dep-live0001"
EARLIER_COMMIT = "b" * 40
EARLIER_DEPLOY = "dep-earlier01"


@dataclass
class FakeRender:
    """The Render API and the deployed service, served over real HTTP on loopback."""

    service: dict[str, Any]
    deploys: list[dict[str, Any]]
    health_status: int = 200
    # Health answers before the service settles: (status, payload) pairs served
    # in order, then the live answer. A payload of bytes is sent as it is.
    health_not_yet: list[tuple[int, Any]] = field(default_factory=list)
    health_extra: dict[str, Any] = field(default_factory=dict)
    health_service: str = "folio"
    health_redirect: str | None = None
    health_headers: dict[str, str] = field(default_factory=dict)
    # Cloudflare on the public zone answers its browser-signature 403 (error 1010)
    # to Python's default User-Agent (naapp/library/docs/production-operations.md).
    edge_refuses_default_python_agent: bool = False
    health_agents: list[str] = field(default_factory=list)
    api_agents: list[str] = field(default_factory=list)
    api_authorizations: list[str] = field(default_factory=list)
    # Render answering every API request with this status (an error).
    api_status: int | None = None
    # Called after a POST is answered, and on every poll of a deploy.
    after_post: Any = None
    on_poll: Any = None
    # The health body is cut short of its Content-Length.
    health_truncated: bool = False
    # Bytes sent in place of any HTTP response at all (a broken status line).
    health_raw: bytes | None = None
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    polls_until_live: int = 1
    _counter: int = 0

    def handle(
        self, method: str, path: str, body: Any, user_agent: str = "", authorization: str = ""
    ) -> tuple[int, Any]:
        self.requests.append((method, path, body))
        if path.startswith("/v1/"):
            self.api_agents.append(user_agent)
            self.api_authorizations.append(authorization)
            if self.api_status is not None:
                return self.api_status, {"message": "denied"}
        base = f"/v1/services/{SERVICE_ID}"
        if method == "GET" and path == "/health":
            self.health_agents.append(user_agent)
            if self.edge_refuses_default_python_agent and user_agent.startswith("Python-urllib/"):
                return 403, {"error": 1010}
            if self.health_redirect is not None:
                self.health_headers = {"Location": self.health_redirect}
                return 302, {}
            if self.health_not_yet:
                return self.health_not_yet.pop(0)
            return self.health_status, self.health_payload()
        if method == "GET" and path == base:
            return 200, self.service
        if method == "GET" and path.startswith(f"{base}/deploys?"):
            return 200, [{"deploy": deploy, "cursor": "x"} for deploy in self.deploys]
        if method == "GET" and path.startswith(f"{base}/deploys/"):
            deploy = self._find(path.rsplit("/", 1)[1])
            if deploy is None:
                return 404, {}
            if self.on_poll is not None:
                self.on_poll(deploy)
            if deploy["status"] == "build_in_progress":
                deploy["_polls"] = deploy.get("_polls", 0) + 1
                if deploy["_polls"] >= self.polls_until_live:
                    for other in self.deploys:
                        if other["status"] == "live":
                            other["status"] = "deactivated"
                    deploy["status"] = "live"
            return 200, deploy
        if method == "POST" and path == f"{base}/deploys":
            created = self._create(str(body["commitId"]))
            if self.after_post is not None:
                self.after_post()
            return 201, created
        if method == "POST" and path == f"{base}/rollback":
            target = self._find(str(body["deployId"]))
            if target is None:
                return 404, {}
            created = self._create(target["commit"]["id"])
            if self.after_post is not None:
                self.after_post()
            return 201, created
        return 404, {}

    def health_payload(self) -> dict[str, Any]:
        """What the running service answers: the live deploy's build."""
        live = next((deploy for deploy in self.deploys if deploy["status"] == "live"), None)
        sha = live["commit"]["id"] if live else None
        return {"status": "ok", "service": self.health_service, "build": {"git_sha": sha}, **self.health_extra}

    def _find(self, deploy_id: str) -> dict[str, Any] | None:
        return next((deploy for deploy in self.deploys if deploy["id"] == deploy_id), None)

    def _create(self, commit: str) -> dict[str, Any]:
        self._counter += 1
        deploy = {"id": f"dep-new{self._counter:04d}", "status": "build_in_progress", "commit": {"id": commit}}
        self.deploys.insert(0, deploy)
        return deploy

    def posts(self) -> list[tuple[str, Any]]:
        return [(path, body) for method, path, body in self.requests if method == "POST"]


@pytest.fixture()
def fake() -> Iterator[tuple[FakeRender, str]]:
    state = FakeRender(service={}, deploys=[])

    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            if self.path == "/health" and state.health_raw is not None:
                state.health_agents.append(self.headers.get("User-Agent", ""))
                self.wfile.write(state.health_raw)
                self.close_connection = True
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload = state.handle(
                self.command,
                self.path,
                body,
                self.headers.get("User-Agent", ""),
                self.headers.get("Authorization", ""),
            )
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            truncated = self.path == "/health" and state.health_truncated
            self.send_header("Content-Length", str(len(data) + (100 if truncated else 0)))
            if self.path == "/health":
                for name, value in state.health_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data[: len(data) // 2] if truncated else data)
            if truncated:
                self.close_connection = True

        do_GET = _respond
        do_POST = _respond

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    yield state, origin
    server.shutdown()
    server.server_close()


def _config(origin: str) -> ProductionConfig:
    return ProductionConfig(
        service_id=SERVICE_ID,
        service_name="folio",
        app_id="folio",
        region="oregon",
        repo="https://github.com/awebai/aweb",
        branch="main",
        root_dir="",
        dockerfile_path="./naapp/folio/Dockerfile",
        docker_context=".",
        origin_url=origin,
        public_url=origin,
        health_path="/health",
    )


def _matching_service(config: ProductionConfig) -> dict[str, Any]:
    return {
        "id": config.service_id,
        "name": config.service_name,
        "repo": config.repo,
        "branch": config.branch,
        "rootDir": config.root_dir,
        "autoDeploy": "no",
        "suspended": "not_suspended",
        "serviceDetails": {
            "region": config.region,
            "url": config.origin_url,
            "envSpecificDetails": {
                "dockerfilePath": config.dockerfile_path,
                "dockerContext": config.docker_context,
            },
        },
    }


def _ready(fake: tuple[FakeRender, str]) -> tuple[FakeRender, ProductionConfig, RenderClient]:
    state, origin = fake
    config = _config(origin)
    state.service = _matching_service(config)
    state.deploys = [
        {"id": LIVE_DEPLOY, "status": "live", "commit": {"id": LIVE_COMMIT}},
        {"id": EARLIER_DEPLOY, "status": "deactivated", "commit": {"id": EARLIER_COMMIT}},
    ]
    return state, config, RenderClient("test-key", base_url=f"{origin}/v1")


def _write_config(tmp_path: Path, **overrides: str) -> Path:
    raw = {
        "service_id": SERVICE_ID,
        "service_name": "folio",
        "app_id": "folio",
        "region": "oregon",
        "repo": "https://github.com/awebai/aweb",
        "branch": "main",
        "root_dir": "",
        "dockerfile_path": "./naapp/folio/Dockerfile",
        "docker_context": ".",
        "origin_url": "https://folio.onrender.com",
        "public_url": "https://folio.aweb.ai",
        "health_path": "/health",
    }
    raw.update(overrides)
    path = tmp_path / "render-production.json"
    path.write_text(json.dumps(raw))
    return path


def test_config_accepts_the_repository_root_as_root_dir(tmp_path: Path) -> None:
    config = ProductionConfig.load(_write_config(tmp_path))
    assert config.root_dir == ""
    assert config.dockerfile_path == "./naapp/folio/Dockerfile"


@pytest.mark.parametrize(
    "overrides",
    [
        {"service_id": "folio"},
        {"origin_url": "http://folio.onrender.com"},
        {"public_url": "http://folio.aweb.ai"},
        {"health_path": "health"},
        {"root_dir": "/naapp/folio"},
        {"root_dir": "../elsewhere"},
        {"dockerfile_path": "../Dockerfile"},
        {"docker_context": "/"},
    ],
)
def test_config_refuses_an_unsafe_value(tmp_path: Path, overrides: dict[str, str]) -> None:
    with pytest.raises(OpsError):
        ProductionConfig.load(_write_config(tmp_path, **overrides))


def test_config_refuses_unknown_or_missing_fields(tmp_path: Path) -> None:
    path = _write_config(tmp_path, extra="x")
    with pytest.raises(OpsError):
        ProductionConfig.load(path)
    raw = json.loads(path.read_text())
    del raw["extra"], raw["docker_context"]
    path.write_text(json.dumps(raw))
    with pytest.raises(OpsError):
        ProductionConfig.load(path)


def test_status_reports_the_live_deploy_of_the_pinned_service(fake) -> None:
    state, config, client = _ready(fake)
    result = render.status(client, config)
    assert result["live_deploy"]["id"] == LIVE_DEPLOY
    assert result["live_deploy"]["commit"] == LIVE_COMMIT
    assert state.posts() == []


@pytest.mark.parametrize(
    ("path", "value", "named"),
    [
        (("rootDir",), "naapp/folio", "rootDir"),
        (("serviceDetails", "envSpecificDetails", "dockerfilePath"), "./Dockerfile", "dockerfilePath"),
        (("serviceDetails", "envSpecificDetails", "dockerContext"), "./naapp/folio", "dockerContext"),
        (("autoDeploy",), "yes", "autoDeploy"),
        (("serviceDetails", "region"), "virginia", "region"),
    ],
)
def test_status_fails_closed_when_the_service_differs_from_the_manifest(fake, path, value, named) -> None:
    state, config, client = _ready(fake)
    target = state.service
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(OpsError, match=named):
        render.status(client, config)


def test_status_treats_an_absent_root_dir_as_the_repository_root(fake) -> None:
    state, config, client = _ready(fake)
    del state.service["rootDir"]
    assert render.status(client, config)["live_deploy"]["id"] == LIVE_DEPLOY


def test_status_fails_closed_on_a_null_root_dir(fake) -> None:
    state, config, client = _ready(fake)
    state.service["rootDir"] = None
    with pytest.raises(OpsError, match="rootDir"):
        render.status(client, config)


def test_status_refuses_while_another_deploy_is_in_progress(fake) -> None:
    state, config, client = _ready(fake)
    state.deploys.insert(0, {"id": "dep-busy0001", "status": "build_in_progress", "commit": {"id": "c" * 40}})
    with pytest.raises(OpsError, match="active"):
        render.status(client, config)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture()
def checkout(tmp_path: Path) -> tuple[Path, str]:
    """A real clone whose origin is a bare repository standing in for the remote."""
    remote = tmp_path / "aweb.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(remote), str(work))
    _git(work, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "--allow-empty", "-m", "c")
    _git(work, "push", "origin", "HEAD:main")
    _git(work, "fetch", "origin")
    return work, _git(work, "rev-parse", "HEAD")


def _commit(work: Path, message: str) -> str:
    _git(work, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "--allow-empty", "-m", message)
    return _git(work, "rev-parse", "HEAD")


def _push(work: Path, ref: str = "main") -> None:
    _git(work, "push", "origin", f"HEAD:{ref}")


def _live(state: FakeRender, commit: str) -> None:
    """The service's live deploy is ``commit``, as LIVE_DEPLOY."""
    state.deploys[0]["commit"] = {"id": commit}


def _deploy(client, config, work, commit, live, **kwargs):
    return render.deploy(
        client, config, commit=commit, rollback_deploy_id=LIVE_DEPLOY, rollback_commit=live,
        repo_root=work, timeout_seconds=10, interval_seconds=0, **kwargs,
    )


@pytest.fixture()
def history(fake, checkout):
    """live <- approved <- tip on the configured remote's main, with ``live`` live."""
    state, config, client = _ready(fake)
    work, live = checkout
    config = replace(config, repo=str(work.parent / "aweb.git"))
    state.service["repo"] = config.repo
    approved = _commit(work, "approved")
    tip = _commit(work, "tip")
    _push(work)
    _live(state, live)
    return state, config, client, work, live, approved, tip


def test_deploy_ships_the_branch_tip_and_waits_for_it_live(history) -> None:
    state, config, client, work, live, _approved, tip = history

    result = _deploy(client, config, work, tip, live)

    assert state.posts() == [(f"/v1/services/{SERVICE_ID}/deploys", {"clearCache": "clear", "commitId": tip})]
    assert result["deploy"]["commit"] == tip
    assert result["deploy"]["status"] == "live"
    assert result["rollback"]["id"] == LIVE_DEPLOY


def test_deploy_reads_the_named_repository_whatever_git_namespace_it_inherits(history, monkeypatch) -> None:
    state, config, client, work, live, _approved, tip = history
    monkeypatch.setenv("GIT_NAMESPACE", "elsewhere")

    result = _deploy(client, config, work, tip, live)

    assert result["deploy"]["commit"] == tip


def test_deploy_ships_an_approved_commit_the_branch_has_moved_past(history) -> None:
    state, config, client, work, live, approved, _tip = history

    result = _deploy(client, config, work, approved, live)

    assert state.posts() == [(f"/v1/services/{SERVICE_ID}/deploys", {"clearCache": "clear", "commitId": approved})]
    assert result["deploy"]["commit"] == approved


def test_deploy_reads_the_branch_from_the_remote_not_a_stale_local_fetch(history, tmp_path) -> None:
    state, config, client, work, live, _approved, _tip = history
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(work.parent / "aweb.git"), str(other))
    newer = _commit(other, "newer")
    _push(other)
    # work has not fetched: its origin/main does not know newer, the remote does.
    assert _git(work, "rev-parse", "origin/main") != newer

    result = _deploy(client, config, work, newer, live)

    assert result["deploy"]["commit"] == newer


def test_deploy_refuses_a_commit_only_a_stale_local_ref_names(history) -> None:
    state, config, client, work, live, _approved, _tip = history
    unpushed = _commit(work, "never pushed")
    _git(work, "update-ref", "refs/remotes/origin/main", unpushed)
    with pytest.raises(OpsError, match="not on main"):
        _deploy(client, config, work, unpushed, live)
    assert state.posts() == []


def test_deploy_refuses_a_commit_that_is_not_on_the_configured_branch(history) -> None:
    state, config, client, work, live, approved, _tip = history
    _git(work, "checkout", "-q", "-b", "side", approved)
    side = _commit(work, "side")
    _push(work, "side")
    with pytest.raises(OpsError, match="not on main"):
        _deploy(client, config, work, side, live)
    assert state.posts() == []


@pytest.mark.parametrize("which", ["live", "behind"])
def test_deploy_never_moves_production_backwards(history, which) -> None:
    state, config, client, work, live, approved, _tip = history
    # behind: approved is live, and the deploy would go back to its parent.
    commit, live_now = (live, live) if which == "live" else (live, approved)
    _live(state, live_now)
    with pytest.raises(OpsError, match="not ahead of the live commit"):
        _deploy(client, config, work, commit, live_now)
    assert state.posts() == []


@pytest.fixture()
def previous(history, tmp_path):
    """The repository the service deployed from before it moved, holding the live commit."""
    remote = tmp_path / "previous.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "previous"
    _git(tmp_path, "clone", str(remote), str(work))
    live = _commit(work, "deployed from the previous repository")
    _push(work)
    return str(remote), live


def test_a_first_deploy_after_a_repository_move_names_the_previous_repository(history, previous) -> None:
    state, config, client, work, _live_here, _approved, tip = history
    previous_repo, previous_live = previous
    _live(state, previous_live)

    result = _deploy(client, config, work, tip, previous_live, from_repository=previous_repo)

    assert result["deploy"]["commit"] == tip


def test_without_the_previous_repository_a_live_commit_from_elsewhere_refuses(history, previous) -> None:
    state, config, client, work, _live_here, _approved, tip = history
    _previous_repo, previous_live = previous
    _live(state, previous_live)
    with pytest.raises(OpsError, match="--from-repository"):
        _deploy(client, config, work, tip, previous_live)
    assert state.posts() == []


def test_naming_a_previous_repository_does_not_skip_the_forward_check_within_one(history, previous) -> None:
    state, config, client, work, live, _approved, _tip = history
    previous_repo, _previous_live = previous
    # The live commit is in the configured repository, so nothing moved.
    with pytest.raises(OpsError, match="is in the configured repository"):
        _deploy(client, config, work, live, live, from_repository=previous_repo)
    assert state.posts() == []


def test_a_live_commit_in_neither_repository_refuses(history, previous) -> None:
    state, config, client, work, _live_here, _approved, tip = history
    previous_repo, _previous_live = previous
    _live(state, "e" * 40)
    with pytest.raises(OpsError, match="not on main of the previous repository"):
        _deploy(client, config, work, tip, "e" * 40, from_repository=previous_repo)
    assert state.posts() == []


def test_a_repository_move_needs_its_own_confirmation() -> None:
    render.confirm_repository_change(None, "")
    render.confirm_repository_change(
        "https://github.com/awebai/aweb", "git@github.com:awebai/aweb.git"
    )
    with pytest.raises(OpsError, match="confirmation"):
        render.confirm_repository_change("https://github.com/awebai/aweb", "")
    with pytest.raises(OpsError, match="confirmation"):
        render.confirm_repository_change(None, "https://github.com/awebai/aweb")


def test_deploy_refuses_a_checkout_whose_origin_is_another_repository(fake, checkout, tmp_path) -> None:
    # The configured repository and the checkout's origin both hold the approved
    # commit as their main tip, so only the origin's identity differs: a deploy
    # from a clone of another repository (aweb, after Folio moved) ships nothing.
    state, config, client = _ready(fake)
    work, commit = checkout
    configured = tmp_path / "naapp.git"
    _git(tmp_path, "clone", "--bare", str(work.parent / "aweb.git"), str(configured))
    config = replace(config, repo=str(configured))
    state.service["repo"] = config.repo
    assert _git(work, "ls-remote", str(configured), "refs/heads/main").split()[0] == commit
    assert _git(work, "remote", "get-url", "origin") != config.repo
    with pytest.raises(OpsError):
        render.deploy(
            client, config, commit=commit, rollback_deploy_id=LIVE_DEPLOY, rollback_commit=LIVE_COMMIT,
            repo_root=work, timeout_seconds=10, interval_seconds=0,
        )
    assert state.posts() == []


def test_deploy_fails_closed_when_the_remote_cannot_be_read(fake, checkout) -> None:
    import shutil

    state, config, client = _ready(fake)
    work, commit = checkout
    remote = work.parent / "aweb.git"
    config = replace(config, repo=str(remote))
    state.service["repo"] = config.repo
    shutil.rmtree(remote)
    with pytest.raises(OpsError, match="configured remote"):
        render.deploy(
            client, config, commit=commit, rollback_deploy_id=LIVE_DEPLOY, rollback_commit=LIVE_COMMIT,
            repo_root=work, timeout_seconds=10, interval_seconds=0,
        )
    assert state.posts() == []


def test_deploy_refuses_when_the_live_deploy_is_not_the_named_rollback_artifact(fake, checkout) -> None:
    state, config, client = _ready(fake)
    work, commit = checkout
    config = replace(config, repo=str(work.parent / "aweb.git"))
    state.service["repo"] = config.repo
    with pytest.raises(OpsError, match="approved artifact"):
        render.deploy(
            client, config, commit=commit, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
            repo_root=work, timeout_seconds=10, interval_seconds=0,
        )
    assert state.posts() == []


def test_rollback_redeploys_the_earlier_artifact_and_never_rebuilds_a_commit(fake) -> None:
    state, config, client = _ready(fake)

    result = render.rollback(
        client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
        current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT, timeout_seconds=10, interval_seconds=0,
    )

    assert state.posts() == [(f"/v1/services/{SERVICE_ID}/rollback", {"deployId": EARLIER_DEPLOY})]
    assert result["rollback_deploy"]["commit"] == EARLIER_COMMIT
    assert result["rollback_deploy"]["status"] == "live"


@pytest.mark.parametrize(
    "artifact",
    [
        # The Library recovery target 0c490a3627: built under the service's earlier
        # settings (rootDir naapp/library), and it reports its build.
        "built under earlier service settings",
        # Library's aweb-built ca76b9c8 after the service moved to naapp: its commit
        # is in no repository the config names.
        "built from the previous repository",
    ],
)
def test_rollback_to_an_older_artifact_reads_no_git(fake, tmp_path, monkeypatch, artifact) -> None:
    state, config, client = _ready(fake)
    config = replace(config, repo="https://github.com/awebai/naapp")
    state.service["repo"] = config.repo
    # Rollback redeploys a built artifact: no git, no checkout, no legacy path.
    monkeypatch.setenv("PATH", str(tmp_path / "no-git-here"))
    monkeypatch.chdir(tmp_path)

    result = render.rollback(
        client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
        current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT,
        timeout_seconds=10, interval_seconds=0, **FAST,
    )

    assert state.posts() == [(f"/v1/services/{SERVICE_ID}/rollback", {"deployId": EARLIER_DEPLOY})]
    assert result["health"][config.origin_url + "/health"]["git_sha"] == EARLIER_COMMIT


def test_rollback_to_an_artifact_that_reports_no_build_is_refused(fake) -> None:
    # The one case the retired legacy bridge accepted: a pre-build-identity artifact.
    state, config, client = _ready(fake)
    state.health_not_yet = [(200, {"status": "ok", "service": "folio"})] * 1000
    with pytest.raises(OpsError, match="no build identity"):
        render.rollback(
            client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
            current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT,
            timeout_seconds=10, interval_seconds=0, **FAST,
        )


def test_rollback_cannot_be_asked_to_deploy_a_commit(fake) -> None:
    state, config, client = _ready(fake)
    with pytest.raises(OpsError, match="deploy ID"):
        render.rollback(
            client, config, rollback_deploy_id=EARLIER_COMMIT, rollback_commit=EARLIER_COMMIT,
            current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT, timeout_seconds=10, interval_seconds=0,
        )
    assert state.posts() == []


def test_rollback_refuses_an_artifact_whose_commit_differs(fake) -> None:
    state, config, client = _ready(fake)
    with pytest.raises(OpsError, match="approved artifact"):
        render.rollback(
            client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit="e" * 40,
            current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT, timeout_seconds=10, interval_seconds=0,
        )
    assert state.posts() == []


def test_verify_confirms_the_live_commit_and_health(fake) -> None:
    state, config, client = _ready(fake)
    result = render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT)
    assert result["deploy"]["commit"] == LIVE_COMMIT
    assert result["health"] == {config.origin_url + "/health": {"service": "folio", "git_sha": LIVE_COMMIT}}
    assert state.posts() == []


def test_verify_fails_when_a_different_commit_is_live(fake) -> None:
    _state, config, client = _ready(fake)
    with pytest.raises(OpsError, match="approved artifact"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=EARLIER_COMMIT)


def test_verify_fails_when_health_is_not_ok(fake) -> None:
    state, config, client = _ready(fake)
    state.health_status = 503
    with pytest.raises(OpsError, match="health readiness failed"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)


def test_verify_reaches_health_through_an_edge_that_refuses_python_s_default_agent(fake) -> None:
    state, config, client = _ready(fake)
    state.edge_refuses_default_python_agent = True

    result = render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT)

    assert result["health"] == {config.origin_url + "/health": {"service": "folio", "git_sha": LIVE_COMMIT}}
    assert state.health_agents == [render.HEALTH_USER_AGENT]


def test_a_403_from_health_is_a_failure(fake) -> None:
    state, config, client = _ready(fake)
    state.health_status = 403
    with pytest.raises(OpsError, match="HTTP 403"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT)


FAST = {"readiness_timeout_seconds": 2.0, "retry_interval_seconds": 0.01}


def _both_surfaces(config: ProductionConfig) -> ProductionConfig:
    """The same fake service under two names, as an origin and a public URL."""
    return replace(config, public_url=config.origin_url.replace("127.0.0.1", "localhost"))


def test_verify_requires_the_deployed_build_on_the_origin_and_public_urls(fake) -> None:
    state, config, client = _ready(fake)
    config = _both_surfaces(config)

    result = render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)

    assert result["health"] == {
        config.origin_url + "/health": {"service": "folio", "git_sha": LIVE_COMMIT},
        config.public_url + "/health": {"service": "folio", "git_sha": LIVE_COMMIT},
    }


@pytest.mark.parametrize("not_yet", [503, 404, 429, "other build", "not json"])
def test_health_that_is_not_ready_yet_is_asked_again(fake, not_yet) -> None:
    state, config, client = _ready(fake)
    answer = {
        "other build": (200, {"status": "ok", "service": "folio", "build": {"git_sha": EARLIER_COMMIT}}),
        "not json": (200, b"<html>starting</html>"),
    }.get(not_yet, (not_yet, {"status": "starting"}))
    state.health_not_yet = [answer, answer]

    result = render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)

    assert result["health"][config.origin_url + "/health"]["git_sha"] == LIVE_COMMIT
    assert len(state.health_agents) == 3


def test_health_that_never_becomes_ready_fails_the_verify(fake) -> None:
    state, config, client = _ready(fake)
    state.health_not_yet = [(503, {"status": "unhealthy"})] * 1000
    with pytest.raises(OpsError, match="health readiness failed"):
        render.verify(
            client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT,
            readiness_timeout_seconds=0.2, retry_interval_seconds=0.01,
        )


@pytest.mark.parametrize(
    "fault",
    ["redirect", "other service", "no build", "oversized", "deployment of another commit"],
)
def test_a_health_answer_that_cannot_be_this_deploy_fails_at_once(fake, fault) -> None:
    state, config, client = _ready(fake)
    if fault == "redirect":
        state.health_redirect = "https://elsewhere.example/health"
    elif fault == "other service":
        state.health_service = "library"
    elif fault == "no build":
        state.health_not_yet = [(200, {"status": "ok", "service": "folio"})] * 1000
    elif fault == "oversized":
        state.health_not_yet = [(200, b"{" + b" " * 20_000 + b"}")] * 1000
    else:
        state.health_extra = {"deployment": _deployment(commit=EARLIER_COMMIT)}
    with pytest.raises(OpsError) as refused:
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)
    assert "health readiness failed" not in str(refused.value)
    assert len(state.health_agents) == 1


def test_runtime_health_with_checks_labels_and_deployment_is_accepted(fake) -> None:
    state, config, client = _ready(fake)
    state.health_extra = {
        "checks": {"database": "ok"},
        "details": {},
        "labels": {"tier": "hosted"},
        "deployment": _deployment(commit=LIVE_COMMIT),
    }

    result = render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)

    assert result["health"][config.origin_url + "/health"]["git_sha"] == LIVE_COMMIT


def _deployment(*, commit: str) -> dict[str, str]:
    """The Render deployment block naapp-lib's health serves (runtime/build_identity)."""
    return {
        "service_id": SERVICE_ID,
        "service_name": "folio-tmd1",
        "hostname": "folio-tmd1.onrender.com",
        "origin_url": "https://folio-tmd1.onrender.com",
        "repo": "awebai/naapp",
        "branch": "main",
        "commit": commit,
    }


def _identified(state: FakeRender, config: ProductionConfig, **changes: str) -> ProductionConfig:
    """The fake serves Render's deployment block for its live build; the config pins it."""
    host = urlsplit(config.origin_url).hostname or ""
    block = {**_deployment(commit=LIVE_COMMIT), "service_name": "folio-tmd1", "hostname": host,
             "origin_url": config.origin_url, "repo": "awebai/naapp", **changes}
    state.health_extra = {"deployment": block}
    return replace(config, repo="https://github.com/awebai/naapp", service_slug="folio-tmd1")


def test_public_identity_reads_the_running_deploy_without_a_key(fake) -> None:
    state, config, _client = _ready(fake)
    config = _identified(state, config)

    result = render.public_identity(config)

    assert result["deployment"]["commit"] == LIVE_COMMIT
    assert result["deployment"]["repo"] == "awebai/naapp"
    assert [path for _method, path, _body in state.requests] == ["/health"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_id", "srv-other00000"),
        ("service_name", "folio-other"),
        ("hostname", "elsewhere.example"),
        ("repo", "awebai/aweb"),
        ("branch", "release"),
        ("commit", EARLIER_COMMIT),
    ],
)
def test_public_identity_refuses_a_deploy_that_is_not_the_pinned_one(fake, field, value) -> None:
    state, config, _client = _ready(fake)
    config = _identified(state, config, **{field: value})
    with pytest.raises(OpsError, match=field if field != "commit" else "build"):
        render.public_identity(config)


def test_public_identity_needs_render_s_deployment_block(fake) -> None:
    state, config, _client = _ready(fake)
    with pytest.raises(OpsError, match="deployment"):
        render.public_identity(config)


def test_the_config_may_pin_the_render_slug(tmp_path: Path) -> None:
    assert ProductionConfig.load(_write_config(tmp_path, service_slug="folio-tmd1")).service_slug == "folio-tmd1"
    assert ProductionConfig.load(_write_config(tmp_path)).service_slug is None
    with pytest.raises(OpsError):
        ProductionConfig.load(_write_config(tmp_path, unknown="x"))


def _evidence_run(tmp_path: Path, label: str) -> Any:
    from aweb_naapp.ops.evidence import HealthEvidenceRun

    parent = tmp_path / "evidence-parent"
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    return HealthEvidenceRun(
        parent / label, label=label, schema="folio.health-evidence.v1",
        repository_root=Path(__file__).resolve().parents[2], metadata={"case": label},
    )


def _records(run: Any) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(run.path.glob("*.json"))]


def test_verify_records_each_health_probe_and_its_outcome(fake, tmp_path) -> None:
    state, config, client = _ready(fake)
    state.health_not_yet = [(503, {"status": "starting"})]
    run = _evidence_run(tmp_path, "verify-health")

    render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, evidence=run, **FAST)

    records = _records(run)
    assert [r["probe_kind"] for r in records] == [
        "run-manifest", "unauthenticated-health", "unauthenticated-health", "run-outcome"
    ]
    assert {r["schema"] for r in records} == {"folio.health-evidence.v1"}
    assert [r["status"] for r in records[1:3]] == [503, 200]
    assert records[2]["request_headers"]["user-agent"] == render.HEALTH_USER_AGENT
    assert (records[-1]["outcome"], records[-1]["stage"]) == ("passed", "verify")


def test_a_failed_verify_records_its_outcome(fake, tmp_path) -> None:
    state, config, client = _ready(fake)
    state.health_service = "library"
    run = _evidence_run(tmp_path, "verify-health")
    with pytest.raises(OpsError):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, evidence=run, **FAST)
    outcome = _records(run)[-1]
    assert (outcome["probe_kind"], outcome["outcome"], outcome["stage"]) == ("run-outcome", "failed", "verify")
    assert outcome["error_class"] == "OpsError"


def test_a_config_that_requires_evidence_refuses_a_deploy_without_it(history) -> None:
    state, config, client, work, live, _approved, tip = history
    config = replace(config, evidence="required")
    with pytest.raises(OpsError, match="evidence"):
        _deploy(client, config, work, tip, live)
    assert state.posts() == []


def test_render_api_requests_name_the_ops_tool(fake) -> None:
    state, config, client = _ready(fake)
    render.status(client, config)
    assert state.api_agents and set(state.api_agents) == {render.HEALTH_USER_AGENT}


# --- Gaps from the coverage map of Library's retired render_ops tests ---


def test_public_identity_reads_only_the_public_url(fake) -> None:
    state, config, _client = _ready(fake)
    config = _identified(state, _both_surfaces(config), origin_url=config.origin_url)
    config = replace(config, public_url=_both_surfaces(config).public_url)
    render.public_identity(config)
    assert state.health_agents == [render.HEALTH_USER_AGENT]


def test_public_identity_refuses_a_deployment_block_with_an_extra_field(fake) -> None:
    state, config, _client = _ready(fake)
    config = _identified(state, config, region="oregon")
    with pytest.raises(OpsError, match="deployment block"):
        render.public_identity(config)


def test_public_identity_refuses_another_origin(fake) -> None:
    state, config, _client = _ready(fake)
    config = _identified(state, config, origin_url="https://elsewhere.example")
    with pytest.raises(OpsError, match="origin_url"):
        render.public_identity(config)


def test_a_render_api_error_never_carries_the_key(fake) -> None:
    state, config, _client = _ready(fake)
    client = RenderClient("secret-render-key-value", base_url=f"{fake[1]}/v1")
    state.api_status = 403
    with pytest.raises(OpsError) as refused:
        render.status(client, config)
    assert "secret-render-key-value" not in str(refused.value)
    assert state.api_authorizations == ["Bearer secret-render-key-value"]


@pytest.mark.parametrize(
    "build", [{"git_sha": "a" * 39}, {"git_sha": None}, {"git_sha": "A" * 40}], ids=["short", "null", "upper"]
)
def test_a_health_build_identity_that_is_not_a_full_sha_fails_at_once(fake, build) -> None:
    state, config, client = _ready(fake)
    state.health_not_yet = [(200, {"status": "ok", "service": "folio", "build": build})] * 1000
    with pytest.raises(OpsError, match="no build identity"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)
    assert len(state.health_agents) == 1


def test_extra_build_fields_are_tolerated(fake) -> None:
    # Changed by design: the shared check reads build.git_sha and ignores what else
    # a build block reports (Library's own check required exactly {"git_sha"}).
    state, config, client = _ready(fake)
    state.health_not_yet = [
        (200, {"status": "ok", "service": "folio", "build": {"git_sha": LIVE_COMMIT, "built_at": "now"}})
    ]
    render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)


def test_a_403_is_not_asked_again(fake) -> None:
    state, config, client = _ready(fake)
    state.health_status = 403
    with pytest.raises(OpsError, match="HTTP 403"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)
    assert len(state.health_agents) == 1


def test_a_403_s_body_and_allowed_headers_are_kept_as_evidence(fake, tmp_path) -> None:
    state, config, client = _ready(fake)
    state.health_not_yet = [(403, b"error code: 1010")]
    state.health_headers = {"Server": "cloudflare", "CF-Ray": "ray-id", "Set-Cookie": "must-not-persist"}
    run = _evidence_run(tmp_path, "verify-health")
    with pytest.raises(OpsError, match="HTTP 403"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, evidence=run, **FAST)
    probe = _records(run)[1]
    assert (probe["status"], probe["body_preview"]) == (403, "error code: 1010")
    assert "cloudflare" in probe["response_headers"]["server"]
    assert "set-cookie" in probe["omitted_response_header_names"]
    assert "must-not-persist" not in (run.path / "002.json").read_text()


def test_a_health_body_that_is_not_utf8_is_asked_again(fake) -> None:
    state, config, client = _ready(fake)
    state.health_not_yet = [(200, b"\xff\xfe")]
    render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)
    assert len(state.health_agents) == 2


def test_a_health_body_cut_short_fails_as_ops_error(fake) -> None:
    state, config, client = _ready(fake)
    state.health_truncated = True
    with pytest.raises(OpsError):
        render.verify(
            client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT,
            readiness_timeout_seconds=0.2, retry_interval_seconds=0.01,
        )


def test_a_health_answer_that_is_not_http_is_asked_again_as_an_ops_error(fake) -> None:
    state, config, client = _ready(fake)
    state.health_raw = b"NOT-HTTP garbage\r\n\r\n"
    with pytest.raises(OpsError, match="health readiness failed"):
        render.verify(
            client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT,
            readiness_timeout_seconds=0.2, retry_interval_seconds=0.01,
        )


def test_a_health_body_exactly_at_the_limit_is_read(fake) -> None:
    state, config, client = _ready(fake)
    payload = {"status": "ok", "service": "folio", "build": {"git_sha": LIVE_COMMIT}, "pad": ""}
    payload["pad"] = "x" * (render.HEALTH_BODY_LIMIT - len(json.dumps(payload).encode()))
    assert len(json.dumps(payload).encode()) == render.HEALTH_BODY_LIMIT
    state.health_not_yet = [(200, json.dumps(payload).encode())]
    render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT, **FAST)


def test_retries_stop_when_the_next_would_pass_the_deadline(fake) -> None:
    # Changed by design: the deadline bounds the retries; an answer that is this
    # deploy's is accepted whenever it arrives, and no progress lines are printed
    # (the evidence directory records each probe).
    state, config, client = _ready(fake)
    state.health_not_yet = [(503, {})] * 1000
    now = [0.0]
    with pytest.raises(OpsError, match="after 10 seconds and 6 attempts"):
        render.verify_health_surfaces(
            config, LIVE_COMMIT, readiness_timeout_seconds=10, retry_interval_seconds=2,
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), monotonic=lambda: now[0],
        )
    # Probes at 0, 2, 4, 6, 8 and 10 seconds; the next would pass the deadline.
    assert len(state.health_agents) == 6


def test_status_refuses_an_unknown_deploy_state(fake) -> None:
    state, config, client = _ready(fake)
    state.deploys.insert(0, {"id": "dep-future01", "status": "future_state", "commit": {"id": "c" * 40}})
    with pytest.raises(OpsError, match="unknown Render deploy state"):
        render.status(client, config)


@pytest.mark.parametrize("status", ["created", "queued"])
def test_a_created_or_queued_deploy_counts_as_active(fake, status) -> None:
    state, config, client = _ready(fake)
    state.deploys.insert(0, {"id": "dep-pending01", "status": status, "commit": {"id": "c" * 40}})
    with pytest.raises(OpsError, match="another deploy is active"):
        render.status(client, config)


def test_deploy_refuses_a_health_that_reports_no_build(history) -> None:
    state, config, client, work, live, _approved, tip = history
    state.health_not_yet = [(200, {"status": "ok", "service": "folio"})] * 1000
    with pytest.raises(OpsError, match="no build identity"):
        _deploy(client, config, work, tip, live, **FAST)


def test_deploy_writes_its_run_manifest_before_the_deploy_request(history, tmp_path) -> None:
    state, config, client, work, live, _approved, tip = history
    run = _evidence_run(tmp_path, "deploy-health")
    seen: list[bool] = []
    state.after_post = lambda: seen.append((run.path / "001.json").exists())

    _deploy(client, config, work, tip, live, evidence=run, **FAST)

    assert seen == [True]
    records = _records(run)
    assert records[0]["probe_kind"] == "run-manifest"
    assert (records[-1]["outcome"], records[-1]["stage"]) == ("passed", "deploy")
    assert state.api_authorizations[0] == "Bearer test-key"


def test_rollback_records_its_probes_and_outcome(fake, tmp_path) -> None:
    state, config, client = _ready(fake)
    run = _evidence_run(tmp_path, "rollback-health")
    render.rollback(
        client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
        current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT,
        timeout_seconds=10, interval_seconds=0, evidence=run, **FAST,
    )
    records = _records(run)
    assert "unauthenticated-health" in [r["probe_kind"] for r in records]
    assert (records[-1]["outcome"], records[-1]["stage"]) == ("passed", "rollback")


def test_rollback_refuses_an_artifact_that_is_not_known_good(fake) -> None:
    state, config, client = _ready(fake)
    state.deploys[1]["status"] = "build_failed"
    with pytest.raises(OpsError, match="known-good"):
        render.rollback(
            client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
            current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT,
            timeout_seconds=10, interval_seconds=0, **FAST,
        )
    assert state.posts() == []


def test_rollback_refuses_when_the_live_deploy_is_not_the_named_current(fake) -> None:
    state, config, client = _ready(fake)
    with pytest.raises(OpsError):
        render.rollback(
            client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
            current_deploy_id="dep-other0001", current_commit=LIVE_COMMIT,
            timeout_seconds=10, interval_seconds=0, **FAST,
        )
    assert state.posts() == []


def test_a_competing_deploy_after_the_rollback_request_is_refused(fake) -> None:
    state, config, client = _ready(fake)
    state.polls_until_live = 2
    state.after_post = lambda: state.deploys.insert(
        0, {"id": "dep-rival0001", "status": "build_in_progress", "commit": {"id": "d" * 40}}
    )
    with pytest.raises(OpsError):
        render.rollback(
            client, config, rollback_deploy_id=EARLIER_DEPLOY, rollback_commit=EARLIER_COMMIT,
            current_deploy_id=LIVE_DEPLOY, current_commit=LIVE_COMMIT,
            timeout_seconds=10, interval_seconds=0, **FAST,
        )


def test_wait_returns_an_existing_deploy_once_live(fake) -> None:
    state, config, client = _ready(fake)
    state.deploys.insert(0, {"id": "dep-running01", "status": "build_in_progress", "commit": {"id": "c" * 40}})
    result = render.wait(client, config, deploy_id="dep-running01", commit="c" * 40, timeout_seconds=10, interval_seconds=0)
    assert result["deploy"]["status"] == "live"
    assert state.posts() == []


def test_wait_fails_when_the_deploy_s_commit_changes(fake) -> None:
    state, config, client = _ready(fake)
    state.polls_until_live = 3
    state.deploys.insert(0, {"id": "dep-running01", "status": "build_in_progress", "commit": {"id": "c" * 40}})
    state.on_poll = lambda deploy: deploy.update(commit={"id": "d" * 40})
    with pytest.raises(OpsError):
        render.wait(client, config, deploy_id="dep-running01", commit="c" * 40, timeout_seconds=10, interval_seconds=0)


def test_the_key_file_may_not_be_a_symlink(tmp_path: Path) -> None:
    key_file = tmp_path / "env"
    key_file.write_text("RENDER_API_KEY=k\n")
    key_file.chmod(0o600)
    link = tmp_path / "linked"
    link.symlink_to(key_file)
    with pytest.raises(OpsError, match="symlink"):
        render.load_api_key(link)


def test_the_evidence_schema_and_config_digest_come_from_the_config(tmp_path: Path) -> None:
    path = _write_config(tmp_path, app_id="library", evidence="required")
    original = path.read_bytes()
    config = ProductionConfig.load(path)
    path.write_text("{}")
    assert config.source_sha256 == hashlib.sha256(original).hexdigest()
    assert config.evidence == "required"
    with pytest.raises(OpsError, match="evidence"):
        ProductionConfig.load(_write_config(tmp_path, evidence="optional"))


@pytest.mark.parametrize(
    "where", ["relative", "existing", "inside the repository", "through a symlink"]
)
@pytest.mark.parametrize("command", ["deploy", "rollback"])
def test_cli_refuses_a_bad_evidence_directory_before_any_render_call(tmp_path: Path, where, command) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    (parent / "exists").mkdir()
    (tmp_path / "link").symlink_to(parent, target_is_directory=True)
    directory = {
        "relative": "relative/evidence",
        "existing": str(parent / "exists"),
        "inside the repository": str(Path(__file__).resolve().parents[2] / "evidence-run"),
        "through a symlink": str(tmp_path / "link" / "run"),
    }[where]
    result = _cli(
        "--config", str(_write_config(tmp_path, evidence="required")),
        "--env-file", str(tmp_path / "absent"), command,
        env={"PROD_EVIDENCE_DIR": directory, "APPLY": "1", "CONFIRM_SERVICE_ID": SERVICE_ID},
    )
    assert result.returncode == 2
    assert "Render env file" not in result.stderr
    assert not (Path(__file__).resolve().parents[2] / "evidence-run").exists()


def test_wait_stops_on_a_failed_build(fake) -> None:
    state, config, client = _ready(fake)
    state.deploys.insert(0, {"id": "dep-failed01", "status": "build_failed", "commit": {"id": "c" * 40}})
    with pytest.raises(OpsError, match="build_failed"):
        render.wait_for_deploy(client, config, "dep-failed01", "c" * 40, timeout_seconds=10, interval_seconds=0)


def test_mutations_require_apply_and_the_exact_service_id(tmp_path: Path) -> None:
    config = ProductionConfig.load(_write_config(tmp_path))
    with pytest.raises(OpsError, match="--apply"):
        render.confirm_apply(config, apply=False, confirm_service_id=SERVICE_ID)
    with pytest.raises(OpsError, match="service-ID"):
        render.confirm_apply(config, apply=True, confirm_service_id="srv-other")
    render.confirm_apply(config, apply=True, confirm_service_id=SERVICE_ID)


def test_api_key_file_must_be_private(tmp_path: Path) -> None:
    key_file = tmp_path / "env"
    key_file.write_text("RENDER_API_KEY=k\n")
    key_file.chmod(0o644)
    with pytest.raises(OpsError, match="accessible"):
        render.load_api_key(key_file)
    key_file.chmod(0o600)
    assert render.load_api_key(key_file) == "k"


def _cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    import os
    import sys

    clean = {
        key: value
        for key, value in os.environ.items()
        if key not in {"APPLY", "CONFIRM_SERVICE_ID", "FROM_REPOSITORY", "CONFIRM_FROM_REPOSITORY", "PROD_EVIDENCE_DIR"}
    }
    clean.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "aweb_naapp.ops", *args], text=True, capture_output=True, env=clean
    )


@pytest.mark.parametrize("command", ["deploy", "rollback"])
def test_cli_refuses_a_mutation_without_apply_before_reading_any_key(tmp_path: Path, command: str) -> None:
    result = _cli("--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), command)
    assert result.returncode == 2
    assert "--apply" in result.stderr
    assert result.stdout == ""


def test_cli_refuses_an_unconfirmed_repository_move_before_reading_any_key(tmp_path: Path) -> None:
    result = _cli(
        "--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), "deploy",
        env={"APPLY": "1", "CONFIRM_SERVICE_ID": SERVICE_ID, "FROM_REPOSITORY": "https://github.com/awebai/aweb"},
    )
    assert result.returncode == 2
    assert "repository move refused" in result.stderr
    assert result.stdout == ""


def test_cli_public_identity_reads_no_render_key(tmp_path: Path) -> None:
    config = _write_config(tmp_path, public_url="https://127.0.0.1:1")
    result = _cli("--config", str(config), "--env-file", str(tmp_path / "absent"), "public-identity")
    assert result.returncode == 2
    assert "public identity" in result.stderr or "health request" in result.stderr
    assert "Render env file" not in result.stderr


@pytest.mark.parametrize("evidence_dir", ["", "relative/evidence"])
def test_cli_refuses_a_required_evidence_directory_before_reading_any_key(
    tmp_path: Path, evidence_dir: str
) -> None:
    result = _cli(
        "--config", str(_write_config(tmp_path, evidence="required")),
        "--env-file", str(tmp_path / "absent"), "verify",
        env={"PROD_EVIDENCE_DIR": evidence_dir, "PROD_DEPLOY_ID": LIVE_DEPLOY, "PROD_COMMIT": LIVE_COMMIT},
    )
    assert result.returncode == 2
    assert "evidence" in result.stderr
    assert "Render env file" not in result.stderr


def test_cli_reports_a_missing_key_file_as_one_error(tmp_path: Path) -> None:
    result = _cli("--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), "status")
    assert result.returncode == 2
    assert result.stderr.startswith("ERROR: Render env file not found")
