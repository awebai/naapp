"""Folio on a real stack: the lane's AWID with DNS verification, its
coordination server and Folio's production image behind TLS, on one private
network. Each team installs Folio with the real ``aw plugin install`` before
it calls a member route.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPMethod
from pathlib import Path
from typing import Any

import pytest
from aweb_naapp_testing import Member
from aweb_naapp_testing.compose_lane import REGISTRY_ORIGIN, TEAM_SERVER_ORIGIN, ComposeLane
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneNamespace,
    LaneTeam,
    assert_aw_status,
    assert_aw_success,
    aw,
    aw_json,
    aw_request,
    command,
    connect_team,
    grant_install_admin,
    publish_team_server,
    run_aw,
    run_aw_json,
    shell,
    spawn,
    team_key_seed,
)
from folio_lane import BACKEND_ORIGIN, FolioLane, provision_team
from nacl.signing import SigningKey

pytestmark = pytest.mark.e2e


def _on_host(lane: ComposeLane, path: str) -> Path:
    """A machine path under /runtime, as the same file on the runner."""
    return lane.runtime / path.removeprefix("/runtime/")


def _create_document(
    folio: LaneApp, team: LaneTeam, *, slug: str, title: str, body: str
) -> dict[str, Any]:
    payload = json.dumps({"slug": slug, "title": title, "body": body}, separators=(",", ":"))
    result = aw_request(team, HTTPMethod.POST, f"{folio.origin}/v1/documents", body=payload)
    data = aw_json(result, context=f"create document {slug}")
    assert isinstance(data, dict)
    return data


def _get_document(folio: LaneApp, team: LaneTeam, slug: str) -> dict[str, Any]:
    result = aw_request(team, HTTPMethod.GET, f"{folio.origin}/v1/documents/{slug}")
    data = aw_json(result, context=f"get document {slug}")
    assert isinstance(data, dict)
    return data


def _list_documents(folio: LaneApp, team: LaneTeam) -> list[dict[str, Any]]:
    result = aw_request(team, HTTPMethod.GET, f"{folio.origin}/v1/documents")
    data = aw_json(result, context="list documents")
    assert isinstance(data, list)
    return data


def _append_version(folio: LaneApp, team: LaneTeam, *, slug: str, body: str) -> dict[str, Any]:
    body_file = team.workspace.write(f"{slug}-{uuid.uuid4().hex}.txt", body)
    result = aw_request(
        team, HTTPMethod.POST, f"{folio.origin}/v1/documents/{slug}/versions", body_file=body_file
    )
    data = aw_json(result, context=f"append version {slug}")
    assert isinstance(data, dict)
    return data


def _list_versions(folio: LaneApp, team: LaneTeam, slug: str) -> list[dict[str, Any]]:
    result = aw_request(team, HTTPMethod.GET, f"{folio.origin}/v1/documents/{slug}/versions")
    data = aw_json(result, context=f"list versions {slug}")
    assert isinstance(data, list)
    return data


def _get_billing(folio: LaneApp, team: LaneTeam) -> dict[str, Any]:
    result = aw_request(team, HTTPMethod.GET, f"{folio.origin}/v1/billing")
    data = aw_json(result, context="get billing")
    assert isinstance(data, dict)
    return data


def test_health_endpoints_are_public(folio: LaneApp) -> None:
    for path in ("/health", "/live", "/ready"):
        response = folio.http.get(f"{folio.origin}{path}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok" and body["service"] == folio.app_id, body
        assert "git_sha" in body["build"], body
    checks = folio.http.get(f"{folio.origin}/health").json()["checks"]
    assert checks == {"database": "ok", "outbox": "ok"}, checks


def test_no_envelope_fails_closed(folio: LaneApp) -> None:
    response = folio.http.get(f"{folio.origin}/v1/documents")
    assert response.status_code == 401, response.text


def test_manifest_catalog_is_public_and_variants_byte_stable(folio: LaneApp) -> None:
    from aweb_naapp.manifest import canonical_bytes
    from manifest_config import served_manifest

    # Catalog discovery is public and pins the deployment's filled v2 bytes.
    catalog = folio.http.get(f"{folio.origin}/.well-known/aweb-app-versions.json")
    assert catalog.status_code == 200, catalog.text
    variant = catalog.json()["variants"][0]
    assert catalog.json()["app_id"] == folio.app_id
    assert variant["manifest_version"] == 2
    assert variant["url"] == "/manifests/v2.json"
    response = folio.http.get(f"{folio.origin}{variant['url']}")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    # The real server fills the declared kid from its configured key once at
    # startup, and both discovery paths serve those exact deployment bytes.
    expected = served_manifest(folio.origin)
    expected["app"]["id"] = folio.app_id
    assert response.content == canonical_bytes(expected)
    assert variant["digest"] == "sha256:" + hashlib.sha256(response.content).hexdigest()
    assert folio.http.get(f"{folio.origin}/aweb-app.json").content == response.content
    again = folio.http.get(f"{folio.origin}{variant['url']}")
    assert again.content == response.content
    assert folio.http.get(f"{folio.origin}/.well-known/aweb-app.json").status_code == 404


def test_local_member_installs_folio_without_global_identity_metadata(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    controller = provision_team(namespace)
    invite = run_aw_json(
        controller.workspace,
        "id",
        "team",
        "invite",
        "--namespace",
        controller.namespace,
        "--team",
        controller.team,
        "--member-local",
    )
    # A same-machine controller invite uses the real controller and AWID. The
    # member owns a fresh signing key and certificate, with no global metadata.
    workspace = namespace.workspace(f"{controller.team}-local-member")
    run_aw(workspace, "id", "team", "accept-invite", invite["token"], "--local", "--name", "bob")
    assert not (workspace.host / ".aw" / "identity.yaml").exists()
    assert (workspace.host / ".aw" / "signing.key").is_file()
    run_aw(
        workspace,
        "workspace",
        "connect",
        "--service",
        TEAM_SERVER_ORIGIN,
        "--team",
        controller.team_id,
    )
    cert = run_aw_json(workspace, "id", "cert", "show")
    member = replace(
        controller,
        workspace=workspace,
        alias="bob",
        address="",
        did_key=str(cert["member_did_key"]),
        certificate_id=str(cert["certificate_id"]),
    )
    publish_team_server(controller)
    grant_install_admin(member)
    installed = run_aw_json(workspace, "plugin", "install", folio.origin)
    assert installed["approved"], installed
    receipt = aw_json(
        aw_request(
            member,
            HTTPMethod.GET,
            f"{TEAM_SERVER_ORIGIN}/v1/apps/installed?team_id={member.team_id}",
        ),
        context="local member installation",
    )
    assert any(
        app["app_id"] == folio.app_id
        and app["state"] == "active"
        and app["installation_id"]
        and app["digest"]
        and app["origin_verified"]
        for app in receipt["apps"]
    ), receipt
    result = aw_request(member, HTTPMethod.GET, f"{folio.origin}/v1/documents")
    assert assert_aw_success(result, context="local member Folio tools").strip() == "[]"


def test_member_authenticates_without_the_awid_service_token(
    folio_without_awid_service_token: LaneApp, namespace: LaneNamespace
) -> None:
    # Without a service token Folio checks the member's certificate through
    # AWID's per-certificate status.
    team = provision_team(namespace, folio=folio_without_awid_service_token)
    result = aw_request(
        team, HTTPMethod.GET, f"{folio_without_awid_service_token.origin}/v1/documents"
    )
    assert assert_aw_success(result, context="member without service token").strip() == "[]"


def test_install_after_mint_uses_replacement_grant(
    folio_install: LaneApp,
    folio_lane: FolioLane,
    namespace: LaneNamespace,
) -> None:
    lane = folio_lane.lane
    team = provision_team(namespace)
    resident = team.workspace
    resident_home = f"{resident.path}/.aw"
    before_home = f"{resident.path}-before-install"
    replacement_home = f"{resident.path}-after-install"
    instance = namespace.workspace(f"{team.team}-instance")
    # A reachable TLS deployment has its own identity, never the hosted id.
    app_id = folio_install.app_id
    manifest = folio_install.origin
    connect_team(team)
    pid_file = f"{resident.path}/custody.pid"
    spawn(
        resident,
        f"echo $$ > {shlex.quote(pid_file)}; exec aw --identity-home "
        f"{shlex.quote(resident_home)} custody serve > {shlex.quote(resident.path)}/custody.log 2>&1",
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            status = aw(instance, "--identity-home", resident_home, "--json", "custody", "status")
            if status.returncode == 0:
                break
            assert time.monotonic() < deadline, status.stderr
            time.sleep(0.2)
        minted = run_aw_json(
            resident,
            "id",
            "grant",
            "mint",
            "--bundle",
            "normal-agent",
            "--ttl",
            "30m",
            "--custody-socket",
            "auto",
            "--out",
            before_home,
        )
        assert minted["apps"] == []
        assert minted["custody_socket_path"] == json.loads(status.stdout)["socket_path"]
        before_material = {
            p.name: hashlib.sha256(p.read_bytes()).digest()
            for p in _on_host(lane, before_home).iterdir()
            if p.is_file()
        }
        grant_env = {"AWEB_IDENTITY_HOME": before_home}
        refused = aw(instance, "plugin", "install", manifest, env=grant_env)
        assert refused.returncode != 0 and "app_management_denied" in refused.stderr
        installed = run_aw_json(
            resident, "--identity-home", resident_home, "plugin", "install", manifest
        )
        assert installed["approved"] and installed["existing_grants_unchanged"]
        assert installed["identity_home"] == resident_home
        assert installed["active_grants"] == 1
        assert installed["grant_restart_command"] == [
            "env",
            "AWEB_IDENTITY_HOME=<new-grant-home>",
            "<agent-command>",
        ]
        assert installed["oats_renew_restart_command"] == [
            "oats",
            "session",
            "restart",
            "--home",
            "<instance-home>",
        ]
        updated = run_aw(resident, "--json=false", "plugin", "update", app_id)
        commands = [
            shlex.split(line.strip())
            for line in updated.stdout.splitlines()
            if line.startswith("  ")
        ]
        assert installed["grant_refresh_command"] in commands
        assert installed["grant_restart_command"] in commands
        assert installed["oats_renew_restart_command"] in commands
        payload = instance.write(
            "document with space.json",
            json.dumps(dict(slug="grant-install", title="Grant install", body="first")),
        )
        denied = aw(instance, app_id, "create", "--body-file", payload, env=grant_env)
        assert denied.returncode != 0 and "app_tool_denied" in denied.stderr
        # Follow the machine-readable action; never modify the old grant.
        action = installed["grant_refresh_command"]
        assert action[0] == "aw" and action[-1] == "<new-grant-home>"
        action[-1] = replacement_home
        ttl_index = action.index("--ttl") + 1
        assert action[ttl_index] == "<ttl>"
        action[ttl_index] = "15m"
        replacement = run_aw_json(resident, *action[1:])
        assert replacement["grant_id"] != minted["grant_id"]
        assert replacement["custody_socket_path"] == minted["custody_socket_path"]
        expires = datetime.fromisoformat(replacement["expires_at"].replace("Z", "+00:00"))
        assert timedelta(0) < expires - datetime.now(UTC) <= timedelta(minutes=15)
        assert app_id in {app["app_id"] for app in replacement["apps"]}
        # Follow the printed shell instructions, replacing the WHOLE quoted
        # placeholder and preserving the spaced path argument.
        restart_template = next(
            line.strip()
            for line in updated.stdout.splitlines()
            if line.startswith("  ")
            and shlex.split(line.strip()) == installed["grant_restart_command"]
        )
        restart_shell = restart_template.replace(
            shlex.quote("AWEB_IDENTITY_HOME=<new-grant-home>"),
            shlex.quote(f"AWEB_IDENTITY_HOME={replacement_home}"),
        ).replace(
            shlex.quote("<agent-command>"),
            shlex.join(["aw", "--json", app_id, "create", "--body-file", payload]),
        )
        launched = shell(instance, restart_shell, env=grant_env)
        assert launched.returncode == 0, launched.stderr
        created = json.loads(launched.stdout)
        # JSON uses argument elements, never a joined command string.
        restart_argv = [
            installed["grant_restart_command"][0],
            f"AWEB_IDENTITY_HOME={replacement_home}",
            "aw",
            "--json",
            app_id,
            "show",
            "--slug",
            "grant-install",
        ]
        resumed = command(instance, *restart_argv, env=grant_env)
        assert resumed.returncode == 0, resumed.stderr
        assert json.loads(resumed.stdout)["body"] == "first"
        assert created["slug"] == "grant-install" and created["body"] == "first"
        body = instance.write("append.md", "second\n")
        replacement_env = {"AWEB_IDENTITY_HOME": replacement_home}
        appended = run_aw_json(
            instance,
            app_id,
            "append",
            "--slug",
            "grant-install",
            "--body-file",
            body,
            "--json=true",
            env=replacement_env,
        )
        assert appended["current_version"] == 2
        shown = run_aw_json(
            instance, app_id, "show", "--slug", "grant-install", "--json=false",
            env=replacement_env,
        )
        assert (
            shown["body"] == "second\n"
            and shown["latest"]["created_by_did_key"] == team.did_key
        )
        run_aw(resident, "id", "grant", "revoke", minted["grant_id"])
        active = run_aw_json(resident, "plugin", "update", app_id)
        assert active["active_grants"] == 1  # revoked grants are excluded
        run_aw(resident, "id", "grant", "revoke", replacement["grant_id"])
        inactive = run_aw_json(resident, "plugin", "update", app_id)
        assert inactive["active_grants"] == 0
        assert not (instance.host / ".aw").exists()
        assert not (_on_host(lane, replacement_home) / "signing.key").exists()
        assert before_material == {
            p.name: hashlib.sha256(p.read_bytes()).digest()
            for p in _on_host(lane, before_home).iterdir()
            if p.is_file()
        }
    finally:
        shell(resident, f"kill $(cat {shlex.quote(pid_file)})")


def test_document_endpoints_versions_raw_utf8_and_attribution(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio)

    created = _create_document(folio, team, slug="handoff", title="Handoff", body="first")
    assert created["slug"] == "handoff"
    assert created["body"] == "first"
    assert created["current_version"] == 1
    assert created["latest"]["created_by_did_key"] == team.did_key
    assert created["latest"]["created_by_alias"] == team.alias
    assert created["latest"]["certificate_id"] == team.certificate_id

    listed = _list_documents(folio, team)
    assert [(item["slug"], item["current_version"]) for item in listed] == [("handoff", 1)]

    fetched = _get_document(folio, team, "handoff")
    assert fetched["body"] == "first"

    appended = _append_version(folio, team, slug="handoff", body="second plain text\nnot json")
    assert appended["body"] == "second plain text\nnot json"
    assert appended["current_version"] == 2
    assert appended["latest"]["created_by_did_key"] == team.did_key
    assert appended["latest"]["created_by_alias"] == team.alias
    assert appended["latest"]["certificate_id"] == team.certificate_id

    assert _get_billing(folio, team) == {
        "team_id": team.team_id,
        "usage": {"documents": 1, "max_versions_per_doc": 2},
    }

    versions = _list_versions(folio, team, "handoff")
    assert [item["version_number"] for item in versions] == [2, 1]
    assert all(item["body"] is None for item in versions)
    assert all(item["created_by_did_key"] == team.did_key for item in versions)
    assert all(item["created_by_alias"] == team.alias for item in versions)
    assert all(item["certificate_id"] == team.certificate_id for item in versions)

    bad_body = team.workspace.write("invalid-utf8.bin", b"\xff")
    bad = aw_request(
        team, HTTPMethod.POST, f"{folio.origin}/v1/documents/handoff/versions", body_file=bad_body
    )
    assert_aw_status(bad, 400, context="append invalid UTF-8")


def test_revoked_certificate_fails_after_awid_revocation_cache_refresh(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio)
    assert _list_documents(folio, team) == []

    run_aw(
        team.workspace,
        "id",
        "team",
        "remove-member",
        "--namespace",
        team.namespace,
        "--team",
        team.team,
        "--member",
        team.address,
        "--registry",
        REGISTRY_ORIGIN,
    )
    time.sleep(2.2)
    revoked = aw_request(team, HTTPMethod.GET, f"{folio.origin}/v1/documents")
    assert_aw_status(revoked, 401, context="revoked certificate")


def _await_registry(folio_lane: FolioLane) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            if folio_lane.client.get(f"{REGISTRY_ORIGIN}/health").status_code == 200:
                return
        except AssertionError:
            pass
        assert time.monotonic() < deadline, "the lane's AWID did not come back"
        time.sleep(1.0)


def test_awid_unavailable_honors_cache_then_fails_closed(
    folio: LaneApp,
    folio_lane: FolioLane,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio)
    assert _list_documents(folio, team) == []

    # Killed rather than stopped: a graceful stop can outlast the 2 s cache TTL
    # under load, which would expire the facts before the cached request.
    lane = folio_lane.lane
    lane.checked(lane.compose("kill", "awid"), "kill the lane's AWID")
    try:
        cached = aw_request(team, HTTPMethod.GET, f"{folio.origin}/v1/documents")
        assert (
            assert_aw_success(cached, context="cached request while awid stopped").strip() == "[]"
        )

        time.sleep(2.2)
        expired = aw_request(team, HTTPMethod.GET, f"{folio.origin}/v1/documents")
        assert_aw_status(expired, 503, context="cache expired while awid stopped")
    finally:
        lane.checked(lane.compose("start", "awid"), "start the lane's AWID")
        _await_registry(folio_lane)


def test_replay_negatives_reject_path_method_and_audience(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio)
    # A request the team's own controller key signs, for the member's
    # published certificate: Folio admits it, then refuses it replayed.
    member = Member(
        team_id=team.team_id,
        team_key=SigningKey(team_key_seed(namespace, team)),
        member_key=SigningKey.generate(),
        certificate_id=team.certificate_id,
        alias=team.alias,
    )
    headers = member.headers(aud=folio.origin, method="GET", path="/v1/documents")
    original = folio.http.get(f"{folio.origin}/v1/documents", headers=headers)
    assert original.status_code == 200, original.text
    assert original.json() == []

    path_replay = folio.http.get(f"{folio.origin}/v1/documents/replayed", headers=headers)
    assert path_replay.status_code == 401, path_replay.text

    method_replay = folio.http.post(f"{folio.origin}/v1/documents", headers=headers)
    assert method_replay.status_code == 401, method_replay.text

    wrong_aud = aw_request(team, HTTPMethod.GET, f"{BACKEND_ORIGIN}/v1/documents")
    assert_aw_status(wrong_aud, 401, context="signed audience for backend host")


def test_real_aw_usage_is_measured_not_capped(folio: LaneApp, namespace: LaneNamespace) -> None:
    team = provision_team(namespace, folio=folio)

    for index in range(4):
        created = _create_document(
            folio,
            team,
            slug=f"note-{index}",
            title=f"Note {index}",
            body="hello",
        )
        assert created["slug"] == f"note-{index}"
    for version in range(2, 52):
        appended = _append_version(folio, team, slug="note-0", body=f"v{version}")
        assert appended["current_version"] == version

    assert _get_billing(folio, team) == {
        "team_id": team.team_id,
        "usage": {"documents": 4, "max_versions_per_doc": 51},
    }
