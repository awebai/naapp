from __future__ import annotations

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
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    polls_until_live: int = 1
    _counter: int = 0

    def handle(self, method: str, path: str, body: Any, user_agent: str = "") -> tuple[int, Any]:
        self.requests.append((method, path, body))
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
            if deploy["status"] == "build_in_progress":
                deploy["_polls"] = deploy.get("_polls", 0) + 1
                if deploy["_polls"] >= self.polls_until_live:
                    for other in self.deploys:
                        if other["status"] == "live":
                            other["status"] = "deactivated"
                    deploy["status"] = "live"
            return 200, deploy
        if method == "POST" and path == f"{base}/deploys":
            return 201, self._create(str(body["commitId"]))
        if method == "POST" and path == f"{base}/rollback":
            target = self._find(str(body["deployId"]))
            if target is None:
                return 404, {}
            return 201, self._create(target["commit"]["id"])
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
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload = state.handle(
                self.command, self.path, body, self.headers.get("User-Agent", "")
            )
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if self.path == "/health":
                for name, value in state.health_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

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
        if key not in {"APPLY", "CONFIRM_SERVICE_ID", "FROM_REPOSITORY", "CONFIRM_FROM_REPOSITORY"}
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


def test_cli_reports_a_missing_key_file_as_one_error(tmp_path: Path) -> None:
    result = _cli("--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), "status")
    assert result.returncode == 2
    assert result.stderr.startswith("ERROR: Render env file not found")
