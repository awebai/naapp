"""A member creates, claims, locks and closes through the real ``aw tasks``,
against the real AWID registry and aweb server, with the app's production
image behind TLS. Run by ``make e2e-tasks``; the unit run deselects it.
"""

from __future__ import annotations

import json
import os
import secrets
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


def aw(machine: str, *args: str):
    """aw --json in a machine, without a terminal; its JSON answer."""
    result = docker("exec", machine, "aw", "--json", *args, timeout=300)
    assert result.returncode == 0, f"aw {' '.join(args)}\n{result.stdout}\n{result.stderr}"
    return first_json(result.stdout)


def aw_refused(machine: str, *args: str) -> str:
    """aw --json expecting the app's refusal; the whole output, for its code."""
    result = docker("exec", machine, "aw", "--json", *args, timeout=300)
    assert result.returncode != 0, result.stdout
    return result.stdout + result.stderr


def write_app_files(lane: ComposeLane) -> None:
    keys = lane.runtime / "tasks-keys"
    keys.mkdir(mode=0o755)
    keys.chmod(0o755)  # mkdir's mode is masked by the launching umask
    for name in ("emit", "grant"):
        value = secrets.token_hex(32)
        lane.redact(value)
        (keys / name).write_text(value + "\n")
        (keys / name).chmod(0o644)
    settings = {
        "TASKS_DATABASE_URL": f"postgresql://tasks:disposable@postgres:5432/{DATABASE}",
        "TASKS_PUBLIC_ORIGIN": API,
        "TASKS_AWID_REGISTRY_URL": REGISTRY_ORIGIN,
        "TASKS_AWID_SERVICE_TOKEN": lane.env["NAAPP_E2E_AWID_SERVICE_TOKEN"],
        "TASKS_APP_KEY_FILES": json.dumps(
            {"tasks:emit-1": "/run/tasks-keys/emit", "tasks:grant-read-1": "/run/tasks-keys/grant"}
        ),
        "TASKS_APP_EVENTS_ORIGIN": TEAM_SERVER_ORIGIN,
        "SSL_CERT_FILE": "/runtime/tls/ca.pem",
        "APP_ENV": "development",
    }
    (lane.runtime / "tasks.env").write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
    (lane.runtime / "tasks.env").chmod(0o644)


@pytest.fixture
def lane(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Iterator[ComposeLane]:
    if os.environ.get("TASKS_E2E") != "1":
        pytest.fail("run by make e2e-tasks, which sets TASKS_E2E=1")
    lane = ComposeLane(
        "tasks-e2e", tmp_path_factory.mktemp("tasks-e2e"), APP_DIR / "docker-compose.e2e.yml",
        env={"NAAPP_E2E_POSTGRES_USER": "tasks", "NAAPP_E2E_POSTGRES_DB": "tasks"},
        subnet=DEFAULT_SUBNET,
    )
    lane.prepare(
        domain=DOMAIN, upstreams={HOST: "tasks:8765"}, app_origins=(API,),
        controller_did="did:key:z6MkpendingpendingpendingpendingpendingpendingP",
    )
    write_app_files(lane)
    try:
        lane.start_core()
        created = lane.compose("exec", "-T", "postgres", "psql", "-U", "tasks", "-d", "tasks", "-v",
                               "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {DATABASE}")
        assert created.returncode == 0, created.stdout + created.stderr
        lane.up("tasks")
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
    installed = aw(machine, "plugin", "install", API)
    assert installed.get("approved"), installed

    task = aw(machine, "tasks", "create", "--title", "Ship the Tasks app", "--labels", "release")
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
