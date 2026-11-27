"""The Tasks app through its HTTP routes, against the real AWID and aweb server.

Requests are signed by real member certificates; installation uses the
server's own pinned-manifest writer with the scopes each case grants. No
verifier, grant source, database or clock-free lease is mocked: the lease
clock is injected only so expiry can be reached without waiting a day.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from aweb.app_manifests import FetchedManifest
from aweb.installations import install_verified_manifest_on
from aweb_naapp.manifest import load_manifest
from aweb_naapp_testing import Member
from awid_service.testing import REGISTRY_URL, SERVICE_TOKEN, dns_naming
from nacl.signing import SigningKey

from aweb_tasks.app import REFUSAL_STATUS, create_app
from aweb_tasks.config import TasksSettings
from aweb_tasks.decisions import Code

from .deployment import TEST_APP_ID, served_manifest, write_key_files

ORIGIN = "https://tasks.example.test"
DOMAIN = "tasks.example"
NAMESPACE_KEY = SigningKey.generate()
ALL_SCOPES = ["tasks.read", "tasks.write", "tasks.override"]


@dataclass
class Clock:
    """The lease clock: real time, moved forward by the tests that need expiry."""

    offset: timedelta = field(default_factory=timedelta)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


@dataclass
class World:
    coordination: Any
    client: httpx.AsyncClient
    clock: Clock
    teams: dict[str, list[Member]]

    async def call(self, member: Member, method: str, path: str, body: Any = None) -> httpx.Response:
        data = b"" if body is None else json.dumps(body).encode()
        headers = member.headers(aud=ORIGIN, method=method, path=path, body=data)
        if body is not None:
            headers["Content-Type"] = "application/json"
        return await self.client.request(method, path, content=data, headers=headers)

    async def ok(self, member: Member, method: str, path: str, body: Any = None) -> Any:
        response = await self.call(member, method, path, body)
        assert response.status_code == 200, response.text
        return response.json()


def team_members(label: str, aliases: list[str]) -> list[Member]:
    team_id = f"{label}-{uuid4().hex[:10]}:{DOMAIN}"
    first = Member(team_id=team_id, certificate_id="c_1", alias=aliases[0])
    rest = [
        Member(team_id=team_id, team_key=first.team_key, certificate_id=f"c_{i}", alias=alias)
        for i, alias in enumerate(aliases[1:], start=2)
    ]
    return [first, *rest]


async def install(server_db: Any, team_id: str, data: bytes, scopes: list[str]) -> None:
    manifest = load_manifest(data)
    fetched = FetchedManifest(
        origin=ORIGIN, url=ORIGIN + "/.well-known/aweb-app.json",
        digest="sha256:" + hashlib.sha256(data).hexdigest(), data=data, manifest=manifest,
    )
    async with server_db.transaction() as tx:
        await install_verified_manifest_on(tx, team_id=team_id, fetched=fetched, granted_scopes=scopes)


@pytest.fixture(name="world")
async def world_fixture(coordination, test_db_factory, tmp_path):
    teams = {
        # The team under test, its neighbour with its own installation, and
        # installations that grant less.
        "ours": (team_members("ours", ["alice", "bob"]), ALL_SCOPES),
        "neighbour": (team_members("neighbour", ["carol"]), ALL_SCOPES),
        "read-only": (team_members("readonly", ["dora"]), ["tasks.read"]),
        "no-override": (team_members("nooverride", ["erin"]), ["tasks.read", "tasks.write"]),
        "uninstalled": (team_members("uninstalled", ["frank"]), None),
    }
    db = await test_db_factory.create_db(suffix="tasks_http")
    key_files = write_key_files(tmp_path)
    settings = TasksSettings(
        _env_file=None, database_url=db.config.get_dsn(), db_schema=db.schema,
        app_id=TEST_APP_ID, public_origin=ORIGIN, awid_registry_url=REGISTRY_URL,
        awid_service_token=SERVICE_TOKEN, app_key_files=key_files,
    )
    server_db = coordination.server.db.get_manager("aweb")
    manifest = served_manifest(key_files, origin=ORIGIN)
    for members, scopes in teams.values():
        for member in members:
            await coordination.add_member(member, namespace_controller=NAMESPACE_KEY)
        await coordination.publish_server(members[0])
        for member in members:
            await coordination.connect(member)
        if scopes is not None:
            await install(server_db, members[0].team_id, manifest, scopes)
    clock = Clock()
    app = create_app(
        settings, awid_transport=coordination.registry.transport,
        awid_txt_resolver=dns_naming(DOMAIN, NAMESPACE_KEY),
        team_server_transport=coordination.server.transport, clock=clock,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            yield World(coordination, client, clock, {name: members for name, (members, _) in teams.items()})


def code(response: httpx.Response) -> str:
    return response.json()["detail"]["code"]


def test_every_refusal_has_a_status() -> None:
    assert set(REFUSAL_STATUS) == set(Code)
    assert REFUSAL_STATUS[Code.NOT_FOUND] == 404
    assert {status for c, status in REFUSAL_STATUS.items() if c is not Code.NOT_FOUND} == {409}


async def test_a_member_creates_shows_and_lists_numbered_tasks(world: World) -> None:
    alice, bob = world.teams["ours"]
    first = await world.ok(alice, "POST", "/v1/tasks", {"title": "Ship it", "labels": ["release"]})
    second = await world.ok(bob, "POST", "/v1/tasks", {"title": "Test it", "type": "bug", "priority": 1})
    assert (first["number"], second["number"]) == (1, 2)
    assert first["creator"] == {
        "team_id": alice.team_id, "did_key": alice.did_key, "did_aw": None,
        "certificate_id": alice.certificate_id, "alias": "alice",
    }
    shown = await world.ok(bob, "GET", "/v1/tasks/1")
    assert (shown["title"], shown["status"], shown["claim"], shown["labels"]) == ("Ship it", "open", None, ["release"])
    bugs = await world.ok(alice, "GET", "/v1/tasks?type=bug")
    assert [t["number"] for t in bugs] == [2]


async def test_a_refusal_answers_the_shared_shape_with_its_status(world: World) -> None:
    alice, bob = world.teams["ours"]
    await world.ok(alice, "POST", "/v1/tasks", {"title": "Mine"})
    await world.ok(alice, "POST", "/v1/tasks/1/claim", {})
    response = await world.call(bob, "POST", "/v1/tasks/1/claim", {})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert set(detail) == {"code", "message"} and detail["code"] == "claimed_by_other"
    missing = await world.call(bob, "GET", "/v1/tasks/99")
    assert (missing.status_code, code(missing)) == (404, "not_found")


async def test_claim_close_and_history(world: World) -> None:
    alice, bob = world.teams["ours"]
    await world.ok(alice, "POST", "/v1/tasks", {"title": "Work"})
    claimed = await world.ok(alice, "POST", "/v1/tasks/1/claim", {})
    assert claimed["claim"]["holder"] == alice.did_key and claimed["claim"]["holder_alias"] == "alice"
    assert [t["number"] for t in await world.ok(bob, "GET", "/v1/active")] == [1]
    assert await world.ok(bob, "GET", "/v1/ready") == []
    # Only closing is protected: another member may still edit and comment.
    await world.ok(bob, "PATCH", "/v1/tasks/1", {"title": "Work, renamed", "status": "blocked"})
    await world.ok(bob, "POST", "/v1/tasks/1/comments", {"body": "found a snag"})
    refused = await world.call(bob, "POST", "/v1/tasks/1/close", {})
    assert (refused.status_code, code(refused)) == (409, "claimed_by_other")
    refused = await world.call(bob, "PATCH", "/v1/tasks/1", {"status": "closed"})
    assert code(refused) == "claimed_by_other"
    closed = await world.ok(alice, "POST", "/v1/tasks/1/close", {})
    assert (closed["status"], closed["claim"], closed["closer"]["alias"]) == ("closed", None, "alice")
    history = await world.ok(bob, "GET", "/v1/tasks/1/history")
    assert [e["kind"] for e in history] == [
        "created", "claimed", "updated", "status", "commented", "claim_ended", "closed",
    ]
    assert history[-2]["detail"]["previous_holder"] == alice.did_key
    assert [c["author"]["alias"] for c in await world.ok(alice, "GET", "/v1/tasks/1/comments")] == ["bob"]


async def test_expiry_frees_a_claim_and_a_late_renewal_is_refused(world: World) -> None:
    alice, bob = world.teams["ours"]
    await world.ok(alice, "POST", "/v1/tasks", {"title": "Work"})
    alices = (await world.ok(alice, "POST", "/v1/tasks/1/claim", {}))["claim"]
    world.clock.offset = timedelta(hours=24)
    # An expired claim leaves the task open again: ready shows it.
    assert [t["number"] for t in await world.ok(bob, "GET", "/v1/ready")] == [1]
    bobs = (await world.ok(bob, "POST", "/v1/tasks/1/claim", {}))["claim"]
    assert bobs["holder"] == bob.did_key and bobs["revision"] == alices["revision"] + 1
    late = await world.call(alice, "POST", "/v1/tasks/1/claim/renew", {"revision": alices["revision"]})
    assert code(late) == "lease_changed"
    late = await world.call(alice, "POST", "/v1/releases", {"number": 1, "revision": alices["revision"]})
    assert code(late) == "lease_changed"
    assert (await world.ok(bob, "GET", "/v1/tasks/1"))["claim"]["holder"] == bob.did_key


async def test_locks_take_renew_break_and_release(world: World) -> None:
    alice, bob = world.teams["ours"]
    taken = await world.ok(bob, "POST", "/v1/locks", {"resource": "repo/main", "reason": "merging"})
    assert taken["lock"]["holder"] == bob.did_key and taken["lock"]["expires_at"]
    refused = await world.call(alice, "POST", "/v1/locks", {"resource": "repo/main"})
    assert (refused.status_code, code(refused)) == (409, "locked_by_other")
    assert code(await world.call(alice, "POST", "/v1/locks/release", {"resource": "repo/main"})) == "not_holder"
    assert [lock["resource"] for lock in await world.ok(alice, "GET", f"/v1/locks?holder={bob.did_key}")] == ["repo/main"]
    renewed = await world.ok(bob, "POST", "/v1/locks/renew", {"resource": "repo/main"})
    assert renewed["revision"] == taken["revision"] + 1 and renewed["reason"] == "merging"
    stale = await world.call(alice, "POST", "/v1/locks/break", {"resource": "repo/main", "revision": taken["revision"]})
    assert code(stale) == "lease_changed"
    broken = await world.ok(alice, "POST", "/v1/locks/break", {"resource": "repo/main", "revision": renewed["revision"]})
    assert broken["lock"] is None
    assert await world.ok(alice, "GET", "/v1/locks") == []


async def test_takeover_transfers_and_close_claimed_ends_every_claim(world: World) -> None:
    alice, bob = world.teams["ours"]
    await world.ok(alice, "POST", "/v1/tasks", {"title": "Epic", "type": "epic"})
    await world.ok(alice, "POST", "/v1/tasks", {"title": "Child", "parent": 1})
    held = (await world.ok(bob, "POST", "/v1/tasks/1/claim", {}))["claim"]
    child = (await world.ok(bob, "POST", "/v1/tasks/2/claim", {}))["claim"]
    assert code(await world.call(bob, "POST", "/v1/tasks/1/takeover", {"revision": held["revision"]})) == "not_overridable"
    taken = await world.ok(alice, "POST", "/v1/tasks/1/takeover", {"revision": held["revision"]})
    assert taken["claim"]["holder"] == alice.did_key
    history = await world.ok(alice, "GET", "/v1/tasks/1/history")
    assert history[-1]["kind"] == "taken_over" and history[-1]["detail"]["previous_holder"] == bob.did_key
    assert code(await world.call(alice, "POST", "/v1/tasks/1/close", {})) == "has_open_children"
    assert code(await world.call(alice, "POST", "/v1/tasks/1/close", {"with_children": True})) == "claimed_by_other"
    closed = await world.ok(alice, "POST", "/v1/tasks/1/close-claimed", {
        "revision": taken["claim"]["revision"], "with_children": True,
    })
    assert closed["status"] == "closed"
    shown = await world.ok(bob, "GET", "/v1/tasks/2")
    assert (shown["status"], shown["claim"]) == ("closed", None)
    ended = (await world.ok(bob, "GET", "/v1/tasks/2/history"))[-2]
    assert ended["kind"] == "claim_ended" and ended["detail"]["previous_holder"] == bob.did_key
    assert ended["detail"]["revision"] == child["revision"] + 1


async def test_dependencies_drive_ready_and_blocked_and_refuse_cycles(world: World) -> None:
    alice, _ = world.teams["ours"]
    for title in ("Build", "Test"):
        await world.ok(alice, "POST", "/v1/tasks", {"title": title})
    shown = await world.ok(alice, "POST", "/v1/tasks/2/dependencies", {"on": 1})
    assert shown["waits_on"] == [1]
    assert code(await world.call(alice, "POST", "/v1/tasks/1/dependencies", {"on": 2})) == "dependency_cycle"
    assert [t["number"] for t in await world.ok(alice, "GET", "/v1/ready")] == [1]
    assert [t["number"] for t in await world.ok(alice, "GET", "/v1/blocked")] == [2]
    await world.ok(alice, "POST", "/v1/tasks/1/close", {})
    assert [t["number"] for t in await world.ok(alice, "GET", "/v1/ready")] == [2]
    await world.ok(alice, "DELETE", "/v1/tasks/2/dependencies/1")
    assert code(await world.call(alice, "DELETE", "/v1/tasks/2/dependencies/1")) == "not_found"
    assert code(await world.call(alice, "PATCH", "/v1/tasks/1", {"parent": 1})) == "parent_cycle"


async def test_release_all_releases_only_the_callers_leases(world: World) -> None:
    alice, bob = world.teams["ours"]
    for title in ("One", "Two"):
        await world.ok(alice, "POST", "/v1/tasks", {"title": title})
    await world.ok(alice, "POST", "/v1/tasks/1/claim", {})
    await world.ok(bob, "POST", "/v1/tasks/2/claim", {})
    await world.ok(alice, "POST", "/v1/locks", {"resource": "db"})
    released = await world.ok(alice, "POST", "/v1/releases", {"all": True})
    assert released == {"released_claims": [1], "released_locks": ["db"]}
    assert [t["number"] for t in await world.ok(alice, "GET", "/v1/active")] == [2]
    bad = await world.call(alice, "POST", "/v1/releases", {})
    assert bad.status_code == 422


async def test_each_installation_holds_its_own_tasks(world: World) -> None:
    alice, _ = world.teams["ours"]
    (carol,) = world.teams["neighbour"]
    await world.ok(alice, "POST", "/v1/tasks", {"title": "Ours"})
    assert code(await world.call(carol, "GET", "/v1/tasks/1")) == "not_found"
    theirs = await world.ok(carol, "POST", "/v1/tasks", {"title": "Theirs"})
    assert theirs["number"] == 1
    assert [t["title"] for t in await world.ok(alice, "GET", "/v1/tasks")] == ["Ours"]
    # Lock names belong to an installation: the same name locks independently in each.
    await world.ok(alice, "POST", "/v1/locks", {"resource": "shared-name"})
    await world.ok(carol, "POST", "/v1/locks", {"resource": "shared-name"})


@pytest.mark.parametrize(
    ("team", "method", "path", "body"),
    [
        ("read-only", "POST", "/v1/tasks", {"title": "x"}),
        ("no-override", "POST", "/v1/tasks/1/takeover", {"revision": 1}),
        ("no-override", "POST", "/v1/locks/break", {"resource": "r", "revision": 1}),
        ("no-override", "POST", "/v1/tasks/1/close-claimed", {}),
        ("uninstalled", "GET", "/v1/tasks", None),
    ],
    ids=["write-without-scope", "takeover-without-override", "break-without-override",
         "close-claimed-without-override", "not-installed"],
)
async def test_the_installation_grant_admits_each_tool(world: World, team: str, method: str, path: str, body: Any) -> None:
    (member,) = world.teams[team]
    response = await world.call(member, method, path, body)
    assert (response.status_code, code(response)) == (403, "scope_denied")


async def test_an_installation_without_override_still_has_the_ordinary_tools(world: World) -> None:
    (erin,) = world.teams["no-override"]
    await world.ok(erin, "POST", "/v1/tasks", {"title": "Ordinary"})
    await world.ok(erin, "POST", "/v1/tasks/1/claim", {})
    assert (await world.ok(erin, "POST", "/v1/tasks/1/close", {}))["status"] == "closed"
