"""Library admits a member only for a team that installed it and granted the
operation's scopes, and only with a certificate the registry published.

The lane's AWID, its coordination server and Library's production image
behind TLS; teams install Library with the real ``aw plugin install``.
"""

from __future__ import annotations

import json
import time
import uuid
from http import HTTPMethod

import pytest
from aweb_naapp_testing import Member
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneNamespace,
    LaneTeam,
    assert_aw_status,
    aw_json,
    aw_request,
    team_key_seed,
    uninstall_app,
)
from library_lane import connected_team, installed_team
from nacl.signing import SigningKey
from test_library_contract_e2e import _blueprint_payload

pytestmark = pytest.mark.e2e

# The grant cache follows the server's freshness window, at most a minute.
GRANT_WINDOW_SECONDS = 75.0


def _publish(team: LaneTeam, app: LaneApp, blueprint_ref: str):
    body = json.dumps(_blueprint_payload(blueprint_ref=blueprint_ref), separators=(",", ":"))
    return aw_request(team, HTTPMethod.POST, f"{app.origin}/v1/blueprints/import", body=body)


def _catalog_has(app: LaneApp, blueprint_ref: str) -> bool:
    response = app.http.get(f"{app.origin}/v1/blueprints/{blueprint_ref}", timeout=10.0)
    return response.status_code == 200


def _ref() -> str:
    return f"example.authz-{uuid.uuid4().hex[:8]}"


def test_a_team_that_never_installed_library_cannot_publish(
    library: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = connected_team(namespace)
    blueprint_ref = _ref()

    assert_aw_status(
        _publish(team, library, blueprint_ref), 403, context="publish without an install"
    )
    assert not _catalog_has(library, blueprint_ref)
    assert_aw_status(
        aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/shelf"),
        403,
        context="shelf read without an install",
    )


def test_an_install_without_library_write_cannot_write(
    library: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = installed_team(namespace, library, grant_scopes=("library:read",))
    blueprint_ref = _ref()

    assert_aw_status(
        _publish(team, library, blueprint_ref), 403, context="publish with library:read only"
    )
    assert not _catalog_has(library, blueprint_ref)
    assert_aw_status(
        aw_request(team, HTTPMethod.POST, f"{library.origin}/v1/team/register", body="{}"),
        403,
        context="register with library:read only",
    )
    shelf = aw_json(
        aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/shelf"),
        context="shelf with library:read",
    )
    assert isinstance(shelf, dict)


def test_an_uninstalled_team_is_refused(
    library: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = installed_team(namespace, library)
    aw_json(
        aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/proposals"),
        context="installed member reads",
    )

    uninstall_app(team, library)

    deadline = time.monotonic() + GRANT_WINDOW_SECONDS
    while True:
        refused = aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/proposals")
        if refused.returncode != 0 or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    assert_aw_status(refused, 403, context="member of a team that uninstalled Library")
    blueprint_ref = _ref()
    assert_aw_status(_publish(team, library, blueprint_ref), 403, context="publish after uninstall")
    assert not _catalog_has(library, blueprint_ref)


def test_a_team_signed_certificate_the_registry_never_published_is_unauthenticated(
    library: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = installed_team(namespace, library)
    # Signed by the team's real controller key, never registered at AWID.
    unpublished = Member(
        team_id=team.team_id,
        team_key=SigningKey(team_key_seed(namespace, team)),
        certificate_id=str(uuid.uuid4()),
        alias="unpublished",
    )
    response = library.http.get(
        f"{library.origin}/v1/shelf",
        headers=unpublished.headers(aud=library.origin, method="GET", path="/v1/shelf"),
        timeout=10.0,
    )
    assert response.status_code == 401, response.text


def test_a_granted_member_publishes_shelves_and_materializes(
    library: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = installed_team(namespace, library)
    blueprint_ref = _ref()

    published = aw_json(_publish(team, library, blueprint_ref), context="granted publish")
    assert published["blueprint_ref"] == blueprint_ref
    assert _catalog_has(library, blueprint_ref)
    shelved = aw_json(
        aw_request(
            team,
            HTTPMethod.POST,
            f"{library.origin}/v1/shelf/import",
            body=json.dumps({"source_blueprint_ref": blueprint_ref, "profile_ref": "developer"}),
        ),
        context="granted import to shelf",
    )
    assert shelved["created"] is True
    materialized = aw_json(
        aw_request(
            team,
            HTTPMethod.POST,
            f"{library.origin}/v1/materialize",
            body=json.dumps(
                {"profile_ref": "developer", "runtime_kind": "claude-code", "target": "local"}
            ),
        ),
        context="granted materialize",
    )
    assert materialized["source_blueprint_ref"] == blueprint_ref
    assert materialized["home_files"]
