"""A member creates, claims, locks and closes through the real ``aw tasks``,
against the real AWID registry and aweb server, with the app's production
image behind TLS. Run by ``make e2e-tasks``; the unit run deselects it.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from aweb_naapp_testing.compose_lane import (
    DEFAULT_SUBNET,
    REGISTRY_ORIGIN,
    TEAM_SERVER_ORIGIN,
    ComposeLane,
    await_awid_txt,
    docker,
)
from awid.did import did_from_public_key
from nacl.signing import SigningKey

from aweb_tasks import MANIFEST_PATH

pytestmark = pytest.mark.e2e

APP_DIR = Path(__file__).resolve().parents[1]
DOMAIN = "tasks-e2e.test"
TEAM = "builders"
TEAM_ID = f"{TEAM}:{DOMAIN}"
# The hosted origin, which the lane's DNS and CA serve: the app keeps its
# hosted id, so its command is ``aw tasks``.
HOST = "tasks.aweb.ai"
API = f"https://{HOST}"
DATABASE = "tasksapp"


def first_json(text: str):
    """The first JSON value in a command's output, an object or a list; Docker
    may print hints after it."""
    start = min(i for i in (text.find("{"), text.find("[")) if i >= 0)
    return json.JSONDecoder().raw_decode(text[start:])[0]


def aw(machine: str, *args: str, home: str | None = None):
    """aw --json in a machine, without a terminal; its JSON answer. ``home``
    acts as another identity home in that machine, such as a worker's grant."""
    env = [] if home is None else [f"--env=AWEB_IDENTITY_HOME={home}"]
    result = docker("exec", *env, machine, "aw", "--json", *args, timeout=300)
    assert result.returncode == 0, f"aw {' '.join(args)}\n{result.stdout}\n{result.stderr}"
    return first_json(result.stdout)


def aw_refused(machine: str, *args: str, home: str | None = None) -> str:
    """aw --json expecting a refusal; the whole output, for its code."""
    env = [] if home is None else [f"--env=AWEB_IDENTITY_HOME={home}"]
    result = docker("exec", *env, machine, "aw", "--json", *args, timeout=300)
    assert result.returncode != 0, result.stdout
    return result.stdout + result.stderr


def admit_app_member(lane: ComposeLane, machine: str) -> None:
    """The app as an ordinary member of the team, with its own key: the
    owner's add-member, and the member's own fetch of its certificate. Its
    credential, keys and database URL are the files compose.tasks.yaml names."""
    key = SigningKey.generate()
    added = aw(machine, "id", "team", "add-member", "--team", TEAM, "--namespace", DOMAIN,
               "--did", did_from_public_key(bytes(key.verify_key)), "--name", "tasks-service", "--global")
    home = lane.runtime / "tasks-member"
    home.mkdir(mode=0o700)
    pem = ("-----BEGIN ED25519 PRIVATE KEY-----\n" + base64.b64encode(bytes(key)).decode()
           + "\n-----END ED25519 PRIVATE KEY-----\n")
    lane.redact(pem)
    (home / "signing.key").write_text(pem)
    (home / "signing.key").chmod(0o600)
    fetched = aw(machine, "--identity-home", f"/runtime/{home.name}", "id", "team", "fetch-cert",
                 "--namespace", DOMAIN, "--team", TEAM, "--cert-id", added["certificate_id"],
                 "--registry", REGISTRY_ORIGIN)
    files = lane.runtime / "tasks"
    files.mkdir(mode=0o755)
    files.chmod(0o755)  # mkdir's mode is masked by the launching umask
    (files / "awid-member-key").write_text(pem)
    shutil.copyfile(home / fetched["cert_path"], files / "awid-member-certificate")
    for name in ("emit", "grant-read"):
        value = secrets.token_hex(32)
        lane.redact(value)
        (files / name).write_text(value + "\n")
    (files / "database-url").write_text(f"postgresql://tasks:disposable@postgres:5432/{DATABASE}\n")
    for path in files.iterdir():
        path.chmod(0o644)


@pytest.fixture
def lane(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Iterator[ComposeLane]:
    if os.environ.get("TASKS_E2E") != "1":
        pytest.fail("run by make e2e-tasks, which sets TASKS_E2E=1")
    lane = ComposeLane(
        "tasks-e2e", tmp_path_factory.mktemp("tasks-e2e"), APP_DIR / "docker-compose.e2e.yml",
        env={
            "NAAPP_E2E_POSTGRES_USER": "tasks", "NAAPP_E2E_POSTGRES_DB": "tasks",
            # compose.tasks.yaml's settings, for this lane.
            "TASKS_APP_ID": "tasks", "TASKS_PUBLIC_ORIGIN": API, "AWID_REGISTRY_URL": REGISTRY_ORIGIN,
            "TEAM_ID": TEAM_ID, "TASKS_APP_EVENTS_ORIGIN": TEAM_SERVER_ORIGIN,
        },
        subnet=DEFAULT_SUBNET,
    )
    lane.prepare(
        domain=DOMAIN, upstreams={HOST: "tasks:8765"}, app_origins=(API,),
        controller_did="did:key:z6MkpendingpendingpendingpendingpendingpendingP",
    )
    try:
        lane.start_core()
        created = lane.compose("exec", "-T", "postgres", "psql", "-U", "tasks", "-d", "tasks", "-v",
                               "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {DATABASE}")
        assert created.returncode == 0, created.stdout + created.stderr
        yield lane
    finally:
        request.node.add_report_section("teardown", "lane Compose", lane.close())


def found_team(lane: ComposeLane, machine: str) -> dict:
    """The namespace, its team and one agent member, connected to aweb: the
    member also owns the team's installations."""
    prepared = aw(machine, "id", "namespace", "prepare-controller", "--domain", DOMAIN, "--registry", REGISTRY_ORIGIN)
    lane.publish_awid_txt(DOMAIN, prepared["controller_did"])
    await_awid_txt(machine, DOMAIN, prepared["controller_did"])
    aw(machine, "id", "team", "create", "--name", TEAM, "--namespace", DOMAIN, "--registry", REGISTRY_ORIGIN)
    member = aw(machine, "id", "create", "--name", "alice", "--domain", DOMAIN, "--root-parent",
                "--registry", REGISTRY_ORIGIN)
    invite = aw(machine, "id", "team", "invite", "--team", TEAM, "--namespace", DOMAIN, "--member-global")
    lane.redact(invite["token"])
    aw(machine, "id", "team", "accept-invite", invite["token"], "--name", "alice", "--global")
    aw(machine, "id", "team", "set-server", "--team", TEAM, "--namespace", DOMAIN, "--url", TEAM_SERVER_ORIGIN,
       "--registry", REGISTRY_ORIGIN)
    aw(machine, "init", "--url", TEAM_SERVER_ORIGIN, "--name", "alice", "--do-not-touch-agents-md")
    granted = lane.compose("exec", "-T", "aweb", "aweb", "install-admin", "grant", "--team-id", TEAM_ID,
                           "--principal-did", member["did_key"], "--role", "owner")
    assert granted.returncode == 0, granted.stdout + granted.stderr
    return member


def delivered_events(lane: ComposeLane, count: int) -> list[str]:
    """The app's events once ``count`` are delivered to aweb; at most 60 s."""
    for _ in range(120):
        states = lane.query("tasks", DATABASE, "SELECT state FROM outbox_deliveries ORDER BY event_id").split()
        if states.count("delivered") >= count:
            return states
        time.sleep(0.5)
    raise AssertionError(f"the app's events were not delivered: {states}")


def test_a_member_creates_claims_locks_and_closes_through_aw_tasks(lane: ComposeLane) -> None:
    released_aw = os.environ.get("NAAPP_AW_BINARY")
    assert released_aw, "set NAAPP_AW_BINARY to the pinned Linux aw release"
    machine = lane.machine("alice", aw_binary=Path(released_aw))
    member = found_team(lane, machine)
    # The app starts once it is a member of the team it serves.
    admit_app_member(lane, machine)
    lane.up("tasks")
    installed = aw(machine, "plugin", "install", API)
    assert installed.get("approved"), installed

    # aw takes an array parameter as JSON.
    task = aw(machine, "tasks", "create", "--title", "Ship the Tasks app", "--labels", '["release"]')
    assert (task["number"], task["status"], task["creator"]["alias"]) == (1, "open", "alice")

    claimed = aw(machine, "tasks", "claim", "--number", "1")
    # A global identity holds as its registry-verified did:aw.
    assert claimed["claim"]["holder"] == member["did_aw"], claimed
    active = aw(machine, "tasks", "active", "--holder", member["did_aw"])
    assert [t["number"] for t in active] == [1], active

    locked = aw(machine, "tasks", "lock", "--resource", "naapp/main", "--reason", "release")
    assert locked["lock"]["holder"] == member["did_aw"], locked
    assert "not_overridable" in aw_refused(machine, "tasks", "break", "--resource", "naapp/main",
                                           "--revision", str(locked["revision"]))
    aw(machine, "tasks", "unlock", "--resource", "naapp/main")

    closed = aw(machine, "tasks", "close", "--number", "1")
    assert (closed["status"], closed["claim"], closed["closer"]["alias"]) == ("closed", None, "alice")

    # aweb accepted the app's events: created, claimed, locked, unlocked,
    # the claim the close ended, and the close.
    states = delivered_events(lane, 6)
    assert set(states) == {"delivered"}, states


def ordinary_tools() -> list[str]:
    """Every tool the manifest does not put under tasks.override."""
    tools = json.loads(MANIFEST_PATH.read_bytes())["tools"]
    return [tool["name"] for tool in tools if tool["scopes"] != ["tasks.override"]]


def serve_custody(machine: str) -> None:
    """The resident's custody service, which signs its workers' app calls; at most 30 s to answer."""
    started = docker("exec", "-d", machine, "aw", "custody", "serve")
    assert started.returncode == 0, started.stdout + started.stderr
    for _ in range(60):
        if docker("exec", machine, "aw", "--json", "custody", "status", timeout=30).returncode == 0:
            return
        time.sleep(0.5)
    raise AssertionError("the resident's custody service never answered")


def test_a_worker_granted_only_the_ordinary_tools_acts_as_its_resident(lane: ComposeLane) -> None:
    released_aw = os.environ.get("NAAPP_AW_BINARY")
    assert released_aw, "set NAAPP_AW_BINARY to the pinned Linux aw release"
    machine = lane.machine("alice", aw_binary=Path(released_aw))
    member = found_team(lane, machine)
    admit_app_member(lane, machine)
    lane.up("tasks")
    assert aw(machine, "plugin", "install", API).get("approved")

    # The installation grants all three scopes; the worker's grant names only
    # the ordinary tools.
    serve_custody(machine)
    worker = "/runtime/alice/worker"
    selection = [arg for tool in ordinary_tools() for arg in ("--app-tool", f"tasks:{tool}")]
    minted = aw(machine, "id", "grant", "mint", "--bundle", "normal-agent", "--ttl", "1h",
                "--custody-socket", "auto", "--out", worker, *selection)
    (delegated,) = [app for app in minted["apps"] if app["app_id"] == "tasks"]
    assert sorted(delegated["tools"]) == sorted(ordinary_tools()), minted

    # The worker's ordinary calls are its resident's: it holds as the resident.
    aw(machine, "tasks", "create", "--title", "Delegated", home=worker)
    claimed = aw(machine, "tasks", "claim", "--number", "1", home=worker)
    assert claimed["claim"]["holder"] == member["did_aw"], claimed
    revision = str(claimed["claim"]["revision"])

    # The resident's own override reaches the app, which judges it: its own claim.
    assert "not_overridable" in aw_refused(machine, "tasks", "takeover", "--number", "1", "--revision", revision)
    # The worker's override tools are refused before any request reaches the app.
    for override in (
        ("takeover", "--number", "1", "--revision", revision),
        ("close-claimed", "--number", "1", "--revision", revision),
        ("break", "--resource", "naapp/main", "--revision", "1"),
    ):
        assert "app_tool_denied" in aw_refused(machine, "tasks", *override, home=worker), override
    shown = aw(machine, "tasks", "show", "--number", "1", home=worker)
    assert (shown["status"], shown["claim"]["holder"]) == ("open", member["did_aw"])
