from __future__ import annotations

import json
import shlex
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from aweb_naapp.ops import __main__ as ops_cli
from aweb_naapp.ops import render
from aweb_naapp.ops.jobs import JobAllowlist
from aweb_naapp.ops.render import OpsError, ProductionConfig, RenderClient

SERVICE_ID = "srv-folio0000000000000000"
LIVE_COMMIT = "a" * 40
LIVE_DEPLOY = "dep-live0001"
EARLIER_COMMIT = "b" * 40
EARLIER_DEPLOY = "dep-earlier01"
OWNER_ID = "tea-owner000000000000"
JOB_ID = "job-" + "0" * 20


@dataclass
class RawBody:
    content_type: str
    data: bytes


@dataclass
class FakeRender:
    """The Render API and the deployed service, served over real HTTP on loopback."""

    service: dict[str, Any]
    deploys: list[dict[str, Any]]
    health_status: int = 200
    # Cloudflare on the public zone answers its browser-signature 403 (error 1010)
    # to Python's default User-Agent (naapp/library/docs/production-operations.md).
    edge_refuses_default_python_agent: bool = False
    health_agents: list[str] = field(default_factory=list)
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    polls_until_live: int = 1
    # While suspended, what the service's URLs answer instead of the app.
    suspended_page: tuple[int, RawBody] = (503, RawBody("text/html", b"<html>Service Suspended</html>"))
    # Service reads of the suspended field before a suspend or resume shows.
    suspend_reads_before_change: int = 0
    never_suspends: bool = False
    resume_redeploys: bool = False
    # What /health serves as the app's build and deployment identity; None is the live deploy.
    app_git_sha: str | None = None
    app_service_id: str = SERVICE_ID
    # A page that answers the health path in place of the app.
    health_page: tuple[int, RawBody] | None = None
    job_create_refusal: tuple[int, dict[str, Any]] | None = None
    job_statuses: list[str] = field(default_factory=lambda: ["pending", "running", "succeeded"])
    job: dict[str, Any] | None = None
    job_logs: list[str] = field(default_factory=lambda: ["usage: board_move [-h]"])
    log_pages: int = 1
    log_reads_before_lines: int = 0
    _counter: int = 0
    _pending_suspend: tuple[str, int] | None = None

    def handle(self, method: str, path: str, body: Any, user_agent: str = "") -> tuple[int, Any]:
        self.requests.append((method, path, body))
        base = f"/v1/services/{SERVICE_ID}"
        if method == "GET" and path == "/health":
            self.health_agents.append(user_agent)
            if self.edge_refuses_default_python_agent and user_agent.startswith("Python-urllib/"):
                return 403, {"error": 1010}
            if self.service.get("suspended") == "suspended":
                return self.suspended_page
            if self.health_page is not None:
                return self.health_page
            return self.health_status, self._app_identity()
        if method == "GET" and path == base:
            self._advance_suspension()
            return 200, self.service
        if method == "POST" and path in {f"{base}/suspend", f"{base}/resume"}:
            target = "suspended" if path.endswith("/suspend") else "not_suspended"
            if not (self.never_suspends and target == "suspended"):
                self._pending_suspend = (target, self.suspend_reads_before_change)
            if target == "not_suspended" and self.resume_redeploys:
                live = next(deploy for deploy in self.deploys if deploy["status"] == "live")
                live["status"] = "deactivated"
                self._counter += 1
                self.deploys.insert(
                    0, {"id": f"dep-new{self._counter:04d}", "status": "live", "commit": dict(live["commit"])}
                )
            return 202, RawBody("application/json", b"")
        if method == "POST" and path == f"{base}/jobs":
            if self.job_create_refusal is not None:
                return self.job_create_refusal
            self.job = {
                "id": JOB_ID, "serviceId": SERVICE_ID, "startCommand": body["startCommand"],
                "planId": "plan-srv-006", "createdAt": "2026-11-28T10:00:00Z", "status": self.job_statuses[0],
            }
            return 201, self.job
        if method == "GET" and path == f"{base}/jobs/{JOB_ID}" and self.job is not None:
            if self.job["status"] != "canceled" and self.job_statuses:
                self.job["status"] = self.job_statuses.pop(0)
            return 200, self.job
        if method == "POST" and path == f"{base}/jobs/{JOB_ID}/cancel" and self.job is not None:
            self.job["status"] = "canceled"
            return 200, self.job
        if method == "GET" and path.startswith("/v1/logs?"):
            return 200, self._logs(path)
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

    def _advance_suspension(self) -> None:
        if self._pending_suspend is None:
            return
        target, remaining = self._pending_suspend
        if remaining > 0:
            self._pending_suspend = (target, remaining - 1)
            return
        self.service["suspended"] = target
        self._pending_suspend = None

    def _app_identity(self) -> dict[str, Any]:
        live = next((deploy for deploy in self.deploys if deploy["status"] == "live"), None)
        sha = self.app_git_sha or (live["commit"]["id"] if live else None)
        return {
            "status": "ok",
            "service": "folio",
            "build": {"git_sha": sha},
            "deployment": {"service_id": self.app_service_id, "commit": sha},
        }

    def _logs(self, path: str) -> dict[str, Any]:
        query = parse_qs(urlsplit(path).query)
        assert query["ownerId"] == [OWNER_ID]
        assert query["resource"] == [JOB_ID]
        if self.log_reads_before_lines > 0:
            self.log_reads_before_lines -= 1
            return {"hasMore": False, "nextStartTime": None, "nextEndTime": None, "logs": []}
        page = int(query.get("startTime", ["0"])[0] == "page-2")
        lines = self.job_logs if self.log_pages == 1 else self.job_logs[page::2]
        more = self.log_pages > 1 and page == 0
        return {
            "hasMore": more,
            "nextStartTime": "page-2" if more else None,
            "nextEndTime": "page-2-end" if more else None,
            "logs": [
                {"id": f"log-{page}-{index}", "message": line, "timestamp": f"2026-11-28T10:00:0{index}Z", "labels": []}
                for index, line in enumerate(lines)
            ],
        }

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
            if isinstance(payload, RawBody):
                content_type, data = payload.content_type, payload.data
            else:
                content_type, data = "application/json", json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
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
        "ownerId": OWNER_ID,
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
    assert result["health"] == {config.origin_url + "/health": 200}
    assert state.posts() == []


def test_verify_fails_when_a_different_commit_is_live(fake) -> None:
    _state, config, client = _ready(fake)
    with pytest.raises(OpsError, match="approved artifact"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=EARLIER_COMMIT)


def test_verify_fails_when_health_is_not_ok(fake) -> None:
    state, config, client = _ready(fake)
    state.health_status = 503
    with pytest.raises(OpsError, match="health"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT)


def test_verify_reaches_health_through_an_edge_that_refuses_python_s_default_agent(fake) -> None:
    state, config, client = _ready(fake)
    state.edge_refuses_default_python_agent = True

    result = render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT)

    assert result["health"] == {config.origin_url + "/health": 200}
    assert state.health_agents == [render.HEALTH_USER_AGENT]


def test_a_403_from_health_is_a_failure(fake) -> None:
    state, config, client = _ready(fake)
    state.health_status = 403
    with pytest.raises(OpsError, match="HTTP 403"):
        render.verify(client, config, deploy_id=LIVE_DEPLOY, commit=LIVE_COMMIT)


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
        if key not in {
            "APPLY", "CONFIRM_SERVICE_ID", "FROM_REPOSITORY", "CONFIRM_FROM_REPOSITORY",
            "PROD_COMMIT", "JOBS_FILE", "JOB_NAME", "JOB_ARGS",
        }
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


def test_cli_reports_a_missing_key_file_as_one_error(tmp_path: Path) -> None:
    result = _cli("--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), "status")
    assert result.returncode == 2
    assert result.stderr.startswith("ERROR: Render env file not found")


# Suspend and resume.


def _suspended(state: FakeRender) -> None:
    state.service["suspended"] = "suspended"


def test_suspend_reads_back_suspended_and_records_what_the_urls_serve(fake) -> None:
    state, config, client = _ready(fake)
    state.suspend_reads_before_change = 2

    result = render.suspend(client, config, timeout_seconds=10, interval_seconds=0)

    assert state.posts() == [(f"/v1/services/{SERVICE_ID}/suspend", None)]
    assert result["service"]["suspended"] == "suspended"
    assert result["live_deploy"]["id"] == LIVE_DEPLOY
    assert result["while_suspended"] == {
        config.origin_url + "/health": {"status": 503, "content_type": "text/html", "serves_app": False},
    }


def test_a_page_served_with_200_while_suspended_is_never_taken_for_the_app(fake) -> None:
    state, config, client = _ready(fake)
    state.suspended_page = (200, RawBody("application/json", json.dumps({"status": "ok"}).encode()))

    result = render.suspend(client, config, timeout_seconds=10, interval_seconds=0)

    assert result["while_suspended"][config.origin_url + "/health"]["serves_app"] is False


def test_suspend_refuses_a_service_that_differs_from_the_manifest(fake) -> None:
    state, config, client = _ready(fake)
    state.service["autoDeploy"] = "yes"
    with pytest.raises(OpsError, match="autoDeploy"):
        render.suspend(client, config, timeout_seconds=10, interval_seconds=0)
    assert state.posts() == []


def test_suspend_refuses_a_service_that_is_already_suspended(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    with pytest.raises(OpsError, match="suspended"):
        render.suspend(client, config, timeout_seconds=10, interval_seconds=0)
    assert state.posts() == []


def test_suspend_fails_when_the_service_never_reads_back_suspended(fake) -> None:
    state, config, client = _ready(fake)
    state.never_suspends = True
    with pytest.raises(OpsError, match="did not read back 'suspended'"):
        render.suspend(client, config, timeout_seconds=0.2, interval_seconds=0.05)


def test_resume_reads_back_not_suspended_and_the_app_at_the_expected_commit(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    state.suspend_reads_before_change = 1

    result = render.resume(client, config, commit=LIVE_COMMIT, timeout_seconds=10, interval_seconds=0)

    assert state.posts() == [(f"/v1/services/{SERVICE_ID}/resume", None)]
    assert result["service"]["suspended"] == "not_suspended"
    assert result["live_deploy"]["id"] == LIVE_DEPLOY
    assert result["redeployed"] is False
    assert result["new_deploys"] == []
    assert result["app"] == {
        config.origin_url + "/health": {"status": 200, "service_id": SERVICE_ID, "git_sha": LIVE_COMMIT},
    }


def test_resume_records_a_redeploy_when_render_makes_one(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    state.resume_redeploys = True

    result = render.resume(client, config, commit=LIVE_COMMIT, timeout_seconds=10, interval_seconds=0)

    assert result["redeployed"] is True
    assert [deploy["commit"] for deploy in result["new_deploys"]] == [LIVE_COMMIT]
    assert result["live_deploy"]["id"] == result["new_deploys"][0]["id"]


def test_resume_refuses_a_service_that_is_not_suspended(fake) -> None:
    state, config, client = _ready(fake)
    with pytest.raises(OpsError, match="suspended"):
        render.resume(client, config, commit=LIVE_COMMIT, timeout_seconds=10, interval_seconds=0)
    assert state.posts() == []


def test_resume_requires_the_expected_commit_in_full(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    with pytest.raises(OpsError, match="40-character"):
        render.resume(client, config, commit=LIVE_COMMIT[:12], timeout_seconds=10, interval_seconds=0)
    assert state.posts() == []


@pytest.mark.parametrize(
    "page",
    [
        (200, RawBody("text/html", b"<html>Service Suspended</html>")),
        (200, RawBody("application/json", json.dumps({"status": "ok"}).encode())),
        (503, RawBody("application/json", b"{}")),
        (503, RawBody("application/json", json.dumps({
            "build": {"git_sha": LIVE_COMMIT}, "deployment": {"service_id": SERVICE_ID},
        }).encode())),
    ],
    ids=["html-200", "json-without-identity", "503", "503-with-identity"],
)
def test_resume_never_passes_a_page_that_is_not_the_app(fake, page) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    state.health_page = page
    with pytest.raises(OpsError, match="does not serve the app"):
        render.resume(client, config, commit=LIVE_COMMIT, timeout_seconds=0.2, interval_seconds=0.05)


def test_resume_fails_when_the_app_serves_another_service(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    state.app_service_id = "srv-other0000000000000"
    with pytest.raises(OpsError, match="does not serve the app"):
        render.resume(client, config, commit=LIVE_COMMIT, timeout_seconds=0.2, interval_seconds=0.05)


def test_resume_fails_when_the_app_serves_another_commit(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    state.app_git_sha = EARLIER_COMMIT
    with pytest.raises(OpsError, match="git_sha"):
        render.resume(client, config, commit=LIVE_COMMIT, timeout_seconds=0.2, interval_seconds=0.05)


def test_resume_fails_when_render_names_another_live_commit(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    with pytest.raises(OpsError, match="live deploy"):
        render.resume(client, config, commit=EARLIER_COMMIT, timeout_seconds=0.2, interval_seconds=0.05)


# The one-off job allowlist.

HELP_ARGV = ["python", "-m", "aweb_tasks.board_move", "--help"]
LISTING_SHA = "c" * 64
INSTALLATION = "0b5e2a4e-7c1d-4f7a-9a3e-2b8f6c1d9e0a"


def _jobs(tmp_path: Path, entries: Any = None) -> Path:
    path = tmp_path / "one-off-jobs.json"
    path.write_text(json.dumps(entries if entries is not None else {
        "board-move-help": HELP_ARGV,
        "fixture-open": [
            "python", "-m", "fixture", "open",
            "--listing-sha256", {"slot": "listing_sha256", "type": "sha256"},
            "--installation-id", {"slot": "installation_id", "type": "uuid"},
        ],
    }))
    return path


def test_a_listed_job_builds_its_exact_argv(tmp_path: Path) -> None:
    allowlist = JobAllowlist.load(_jobs(tmp_path))
    assert allowlist.argv("board-move-help", {}) == HELP_ARGV


def test_typed_slots_are_filled_in_place(tmp_path: Path) -> None:
    allowlist = JobAllowlist.load(_jobs(tmp_path))
    argv = allowlist.argv("fixture-open", {"listing_sha256": LISTING_SHA, "installation_id": INSTALLATION})
    assert argv == [
        "python", "-m", "fixture", "open", "--listing-sha256", LISTING_SHA, "--installation-id", INSTALLATION,
    ]
    assert shlex.split(render.start_command(argv)) == argv


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("unlisted", {}, "not a listed job"),
        ("board-move-help", {"extra": "x"}, "takes exactly"),
        ("fixture-open", {"listing_sha256": LISTING_SHA}, "takes exactly"),
        ("fixture-open", {"listing_sha256": "C" * 64, "installation_id": INSTALLATION}, "sha256"),
        ("fixture-open", {"listing_sha256": "c" * 63, "installation_id": INSTALLATION}, "sha256"),
        ("fixture-open", {"listing_sha256": LISTING_SHA + "; rm -rf /", "installation_id": INSTALLATION}, "sha256"),
        ("fixture-open", {"listing_sha256": LISTING_SHA, "installation_id": INSTALLATION.upper()}, "uuid"),
        ("fixture-open", {"listing_sha256": LISTING_SHA, "installation_id": "$(id)"}, "uuid"),
    ],
)
def test_a_job_refuses_anything_its_entry_does_not_type(tmp_path: Path, name, args, message) -> None:
    allowlist = JobAllowlist.load(_jobs(tmp_path))
    with pytest.raises(OpsError, match=message):
        allowlist.argv(name, args)


@pytest.mark.parametrize(
    "entries",
    [
        [],
        {"Bad Name": HELP_ARGV},
        {"empty": []},
        {"spaces": ["python", "-m", "x y"]},
        {"shell": ["python", "-c", "import os;os.system('id')"]},
        {"substitution": ["echo", "$(id)"]},
        {"assignment": ["PYTHONPATH=/tmp", "python", "-m", "x"]},
        {"home": ["python", "~/x.py"]},
        {"untyped": ["python", {"slot": "x", "type": "string"}]},
        {"twice": ["python", {"slot": "x", "type": "uuid"}, {"slot": "x", "type": "uuid"}]},
        {"extra-key": ["python", {"slot": "x", "type": "uuid", "default": "y"}]},
        {"number": ["python", 3]},
    ],
)
def test_the_allowlist_file_refuses_an_entry_that_is_not_a_typed_argv(tmp_path: Path, entries) -> None:
    with pytest.raises(OpsError, match="one-off job"):
        JobAllowlist.load(_jobs(tmp_path, entries))


# Running a one-off job.


def _run_job(client, config, argv=HELP_ARGV, **kwargs):
    kwargs.setdefault("timeout_seconds", 10)
    kwargs.setdefault("interval_seconds", 0)
    return render.run_job(client, config, argv, **kwargs)


def test_a_job_runs_to_its_terminal_status_and_returns_its_log(fake) -> None:
    state, config, client = _ready(fake)

    result = _run_job(client, config)

    assert state.posts() == [(f"/v1/services/{SERVICE_ID}/jobs", {"startCommand": "python -m aweb_tasks.board_move --help"})]
    assert result["outcome"] == "succeeded"
    assert result["argv"] == HELP_ARGV
    assert result["start_command"] == "python -m aweb_tasks.board_move --help"
    assert result["service_suspended"] == "not_suspended"
    assert result["job"]["id"] == JOB_ID
    assert result["job"]["status"] == "succeeded"
    assert [line["message"] for line in result["log"]] == ["usage: board_move [-h]"]


def test_a_job_is_started_on_a_suspended_service(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)

    result = _run_job(client, config)

    assert result["service_suspended"] == "suspended"
    assert result["outcome"] == "succeeded"


def test_a_job_refused_at_create_is_recorded_with_render_s_answer(fake) -> None:
    state, config, client = _ready(fake)
    _suspended(state)
    state.job_create_refusal = (400, {"id": "invalid", "message": "service is suspended"})

    result = _run_job(client, config)

    assert result["outcome"] == "refused_at_create"
    assert result["refusal"] == {"http_status": 400, "message": "service is suspended"}
    assert "job" not in result
    assert result["log"] == []


def test_a_job_that_stays_pending_is_canceled_at_the_timeout(fake) -> None:
    state, config, client = _ready(fake)
    state.job_statuses = ["pending"] * 1000

    result = _run_job(client, config, timeout_seconds=0.2, interval_seconds=0.05)

    assert result["outcome"] == "pending_at_timeout"
    assert (f"/v1/services/{SERVICE_ID}/jobs/{JOB_ID}/cancel", None) in state.posts()
    assert result["job"]["status"] == "canceled"


def test_a_job_still_running_at_the_timeout_is_canceled(fake) -> None:
    state, config, client = _ready(fake)
    state.job_statuses = ["running"] * 1000

    result = _run_job(client, config, timeout_seconds=0.2, interval_seconds=0.05)

    assert result["outcome"] == "running_at_timeout"
    assert result["job"]["status"] == "canceled"


def test_a_failed_job_is_reported_as_failed(fake) -> None:
    state, config, client = _ready(fake)
    state.job_statuses = ["pending", "running", "failed"]
    assert _run_job(client, config)["outcome"] == "failed"


def test_a_job_refuses_unknown_job_statuses(fake) -> None:
    state, config, client = _ready(fake)
    state.job_statuses = ["pending", "paused"]
    with pytest.raises(OpsError, match="unknown one-off job status"):
        _run_job(client, config)


def test_a_job_log_is_read_across_pages(fake) -> None:
    state, config, client = _ready(fake)
    state.job_logs = ["one", "two", "three", "four"]
    state.log_pages = 2

    result = _run_job(client, config)

    assert sorted(line["message"] for line in result["log"]) == ["four", "one", "three", "two"]


def test_a_job_log_is_read_again_while_render_has_not_indexed_it(fake) -> None:
    state, config, client = _ready(fake)
    state.log_reads_before_lines = 2

    result = _run_job(client, config)

    assert [line["message"] for line in result["log"]] == ["usage: board_move [-h]"]


def test_a_job_refuses_a_service_that_differs_from_the_manifest(fake) -> None:
    state, config, client = _ready(fake)
    state.service["branch"] = "other"
    with pytest.raises(OpsError, match="branch"):
        _run_job(client, config)
    assert state.posts() == []


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ({"outcome": "succeeded"}, 0),
        ({"outcome": "failed"}, 3),
        ({"outcome": "refused_at_create"}, 3),
        ({"outcome": "pending_at_timeout"}, 3),
        ({"live_deploy": {}}, 0),
    ],
)
def test_only_a_succeeded_job_exits_zero(result, status) -> None:
    assert ops_cli.exit_status(result) == status


@pytest.mark.parametrize("command", ["suspend", "resume", "job"])
def test_cli_refuses_suspend_resume_and_jobs_without_apply_before_reading_any_key(tmp_path: Path, command) -> None:
    jobs = _jobs(tmp_path)
    result = _cli(
        "--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), command,
        env={"JOBS_FILE": str(jobs), "JOB_NAME": "board-move-help", "PROD_COMMIT": LIVE_COMMIT},
    )
    assert result.returncode == 2
    assert "--apply" in result.stderr
    assert result.stdout == ""


def test_cli_refuses_an_unlisted_job_before_reading_any_key(tmp_path: Path) -> None:
    result = _cli(
        "--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), "job",
        env={
            "APPLY": "1", "CONFIRM_SERVICE_ID": SERVICE_ID, "JOBS_FILE": str(_jobs(tmp_path)),
            "JOB_NAME": "rm-everything",
        },
    )
    assert result.returncode == 2
    assert "not a listed job" in result.stderr
    assert result.stdout == ""


def test_cli_takes_job_slots_as_name_value_pairs(tmp_path: Path) -> None:
    result = _cli(
        "--config", str(_write_config(tmp_path)), "--env-file", str(tmp_path / "absent"), "job",
        env={
            "APPLY": "1", "CONFIRM_SERVICE_ID": SERVICE_ID, "JOBS_FILE": str(_jobs(tmp_path)),
            "JOB_NAME": "fixture-open", "JOB_ARGS": f"listing_sha256={LISTING_SHA} installation_id=$(id)",
        },
    )
    assert result.returncode == 2
    assert "uuid" in result.stderr
