"""Teams on a ComposeLane, founded and driven with the real aw in a lane machine.

One namespace serves a lane: a machine prepares its controller key, the lane's
DNS publishes the ``_awid`` TXT record, and the registry verifies it. Each team
is then founded in that namespace, in its own workspace directory under the
machine's home, with one member. A team reaches the lane's coordination server
as an operator's team would: ``aw workspace connect``, ``aw id team
set-server``, the server operator's ``aweb install-admin grant`` and ``aw
plugin install``.

The machine mounts the lane's runtime at ``/runtime``, so every workspace is
also a directory on the host. Public requests go from the same machine, through
the lane's DNS and TLS front (:meth:`ComposeLane.http`).
"""

from __future__ import annotations

import json as json_module
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPMethod
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from awid.signing import load_signing_key_pem

from aweb_naapp_testing.compose_lane import (
    REGISTRY_ORIGIN,
    TEAM_SERVER_ORIGIN,
    ComposeLane,
    await_awid_txt,
    docker,
    machine_dir,
)


def public_request(
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[str, bytes | None, list[tuple[str, str]]]:
    """The URL, body and headers a request with httpx's ``params`` and
    ``json`` sends."""
    sent = list((headers or {}).items())
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    if json is None:
        return url, None, sent
    body = json_module.dumps(json, separators=(",", ":")).encode()
    return url, body, [*sent, ("Content-Type", "application/json")]


@dataclass(frozen=True)
class LaneClient:
    """Requests from a lane machine, as a caller with no credential sends them."""

    lane: ComposeLane
    machine: str

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        url, body, sent = public_request(url, params=params, json=json, headers=headers)
        return self.lane.http(self.machine, method, url, body=body, headers=sent, timeout=timeout)

    def get(self, url: str, **options: Any) -> httpx.Response:
        return self.request("GET", url, **options)

    def post(self, url: str, **options: Any) -> httpx.Response:
        return self.request("POST", url, **options)

    def put(self, url: str, **options: Any) -> httpx.Response:
        return self.request("PUT", url, **options)


@dataclass(frozen=True)
class LaneApp:
    """An app's deployment on the lane: its API origin, its app id, and a
    client on the lane's network."""

    origin: str
    app_id: str
    http: LaneClient


@dataclass(frozen=True)
class LaneWorkspace:
    """A directory in a lane machine where aw runs, and the same directory on the host."""

    lane: ComposeLane
    machine: str
    path: str
    host: Path


@dataclass(frozen=True)
class LaneNamespace:
    """The lane's namespace, controlled from ``home``'s machine."""

    lane: ComposeLane
    machine: str
    home: str
    domain: str
    registry: str = REGISTRY_ORIGIN

    def workspace(self, name: str) -> LaneWorkspace:
        """A new, empty workspace directory under the machine's home."""
        relative = Path(self.home) / "workspaces" / name
        host = self.lane.runtime / relative
        host.mkdir(parents=True)
        return LaneWorkspace(self.lane, self.machine, f"/runtime/{relative}", host)


@dataclass(frozen=True)
class LaneTeam:
    workspace: LaneWorkspace
    namespace: str
    team: str
    team_id: str
    alias: str
    address: str
    did_key: str
    certificate_id: str


def _aw_exec(
    workspace: LaneWorkspace,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    settings = {"AWEB_URL": TEAM_SERVER_ORIGIN, "NO_COLOR": "1", **(env or {})}
    return docker(
        "exec",
        "-w",
        workspace.path,
        *(f"--env={name}={value}" for name, value in settings.items()),
        workspace.machine,
        "aw",
        *args,
    )


def run_aw(
    workspace: LaneWorkspace, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``aw --json`` in ``workspace``, which must succeed."""
    result = _aw_exec(workspace, "--json", *args, env=env)
    workspace.lane.checked(result, f"aw --json {' '.join(args)}")
    return result


def run_aw_json(
    workspace: LaneWorkspace, *args: str, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    result = run_aw(workspace, *args, env=env)
    try:
        payload = json_module.loads(result.stdout)
    except json_module.JSONDecodeError as exc:
        raise AssertionError(
            f"aw command did not emit JSON: {result.stdout}\n{result.stderr}"
        ) from exc
    assert isinstance(payload, dict)
    return payload


def aw_request(
    team: LaneTeam, method: HTTPMethod, url: str, *, body: str | None = None
) -> subprocess.CompletedProcess[str]:
    """``aw id request`` with the team member's certificate, as it returned."""
    args = ["id", "request", method, url, "--team-auth", "--raw"]
    if body is not None:
        args.extend(["--body", body])
    return _aw_exec(team.workspace, *args)


def assert_aw_success(result: subprocess.CompletedProcess[str], *, context: str) -> str:
    assert result.returncode == 0, (
        f"aw id request failed: {context}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}\n"
    )
    return result.stdout


def assert_aw_status(
    result: subprocess.CompletedProcess[str], status: int, *, context: str
) -> None:
    assert result.returncode != 0, (
        f"expected HTTP {status} failure for {context}, got success: {result.stdout}"
    )
    assert f"HTTP {status}" in result.stderr, (
        f"expected HTTP {status} for {context}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\n"
    )


def aw_json(result: subprocess.CompletedProcess[str], *, context: str) -> Any:
    stdout = assert_aw_success(result, context=context)
    try:
        return json_module.loads(stdout)
    except json_module.JSONDecodeError as exc:
        raise AssertionError(f"invalid JSON for {context}: {stdout}") from exc


def prepare_namespace(lane: ComposeLane, machine: str, *, home: str, domain: str) -> LaneNamespace:
    """The namespace's controller key on ``machine``, published in the lane's DNS."""
    namespace = LaneNamespace(lane, machine, home, domain)
    controller = run_aw_json(
        LaneWorkspace(lane, machine, machine_dir(home), lane.runtime / home),
        "id",
        "namespace",
        "prepare-controller",
        "--domain",
        domain,
        "--registry",
        namespace.registry,
    )
    lane.publish_awid_txt(domain, controller["controller_did"], registry=namespace.registry)
    await_awid_txt(machine, domain, controller["controller_did"])
    return namespace


def _write_workspace_binding(
    workspace: LaneWorkspace, *, team_id: str, alias: str, cert_path: str
) -> None:
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (workspace.host / ".aw" / "workspace.yaml").write_text(
        f"""aweb_url: {TEAM_SERVER_ORIGIN}
memberships:
    - team_id: {team_id}
      alias: {alias}
      workspace_id: {uuid.uuid4()}
      cert_path: {cert_path}
      joined_at: \"{now}\"
human_name: e2e
agent_type: agent
workspace_path: {workspace.path}
updated_at: \"{now}\"
""",
        encoding="utf-8",
    )


def found_team(
    namespace: LaneNamespace,
    *,
    alias: str = "alice",
    env: Mapping[str, str] | None = None,
) -> LaneTeam:
    """A new team in ``namespace`` with ``alias`` as its one member, bound to a
    workspace of its own. The team has no server and installs nothing.
    ``env`` adds to the environment each aw command runs in."""
    unique = uuid.uuid4().hex[:12]
    team = f"team-{unique}"
    alias = f"{alias}-{unique}"
    domain = namespace.domain
    registry = namespace.registry
    address = f"{domain}/{alias}"
    workspace = namespace.workspace(team)

    run_aw(workspace, "id", "create", "--domain", domain, "--name", alias,
           "--registry", registry, env=env)
    run_aw(workspace, "id", "team", "create", "--namespace", domain, "--name", team,
           "--registry", registry, env=env)
    added = run_aw_json(workspace, "id", "team", "add-member", "--namespace", domain,
                        "--team", team, "--member", address, env=env)
    certificate_id = str(added["certificate_id"])
    fetched = run_aw_json(workspace, "id", "team", "fetch-cert", "--namespace", domain,
                          "--team", team, "--cert-id", certificate_id, "--registry", registry,
                          env=env)
    team_id = f"{team}:{domain}"
    run_aw(workspace, "id", "team", "switch", team_id, env=env)
    _write_workspace_binding(
        workspace, team_id=team_id, alias=alias, cert_path=str(fetched["cert_path"])
    )
    shown = run_aw_json(workspace, "id", "cert", "show", env=env)
    return LaneTeam(
        workspace=workspace,
        namespace=domain,
        team=team,
        team_id=team_id,
        alias=alias,
        address=address,
        did_key=str(shown["member_did_key"]),
        certificate_id=certificate_id,
    )


def connect_team(team: LaneTeam) -> None:
    """Connects ``team`` to the lane's coordination server, publishes that
    server as the team's and makes the member an install admin: everything an
    install needs."""
    workspace = team.workspace
    run_aw(workspace, "workspace", "connect", "--service", TEAM_SERVER_ORIGIN,
           "--team", team.team_id)
    published = run_aw_json(workspace, "id", "team", "set-server", "--team", team.team,
                            "--namespace", team.namespace, "--url", TEAM_SERVER_ORIGIN,
                            "--registry", REGISTRY_ORIGIN)
    assert published["status"] == "published", published
    assert published["server_url"] == TEAM_SERVER_ORIGIN, published
    workspace.lane.checked(
        workspace.lane.compose(
            "exec", "-T", "aweb", "aweb", "install-admin", "grant", "--team-id", team.team_id,
            "--principal-did", team.did_key, "--role", "owner",
        ),
        f"grant {team.team_id}'s member installation",
    )


def install_app(
    team: LaneTeam, app: LaneApp, *, grant_scopes: Sequence[str] = ()
) -> dict[str, Any]:
    """Installs the app for ``team`` through ``aw plugin install`` and checks the
    server reports it active; ``grant_scopes`` narrows the grant."""
    args = ["plugin", "install", app.origin]
    for scope in grant_scopes:
        args.extend(["--grant-scope", scope])
    installed = run_aw_json(team.workspace, *args)
    assert installed["approved"], installed
    apps = aw_json(
        aw_request(
            team,
            HTTPMethod.GET,
            f"{TEAM_SERVER_ORIGIN}/v1/apps/installed?team_id={team.team_id}",
        ),
        context=f"installed {app.app_id}",
    )
    assert any(
        entry["app_id"] == app.app_id
        and entry["state"] == "active"
        and entry["origin"] == app.origin
        and entry["installation_id"]
        for entry in apps["apps"]
    ), apps
    return installed


def uninstall_app(team: LaneTeam, app: LaneApp) -> None:
    run_aw(team.workspace, "plugin", "uninstall", app.app_id)


def team_key_seed(namespace: LaneNamespace, team: LaneTeam) -> bytes:
    """The 32-byte seed of the team controller key ``aw id team create`` stored for ``team``."""
    home = namespace.lane.runtime / namespace.home
    found = sorted(home.rglob(f"team-keys/{team.namespace}/{team.team}.key"))
    assert len(found) == 1, f"expected one team key for {team.team_id} under {home}, found {found}"
    return load_signing_key_pem(found[0].read_bytes())
