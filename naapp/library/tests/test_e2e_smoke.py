"""Library on a real stack: the lane's AWID with DNS verification, its
coordination server and Library's production image behind TLS, on one
private network. Each team installs Library with the real ``aw plugin
install`` before it calls a member route; no path signs in with a bare
certificate.
"""

from __future__ import annotations

import json
import time
from http import HTTPMethod

import pytest
from aweb_naapp_testing.compose_lane import REGISTRY_ORIGIN
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneNamespace,
    assert_aw_status,
    assert_aw_success,
    aw_request,
    run_aw,
)
from library_lane import E2E_APP_ID, E2E_BUILD_SHA, installed_team

pytestmark = pytest.mark.e2e


def test_health_endpoints_are_public(library: LaneApp) -> None:
    for path in ("/health", "/live", "/ready"):
        response = library.http.get(f"{library.origin}{path}", timeout=10.0)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok", path
        assert body["service"] == E2E_APP_ID, path
        assert body["build"] == {"git_sha": E2E_BUILD_SHA}, path


def test_public_blueprint_catalog_needs_no_auth(library: LaneApp) -> None:
    blueprints = library.http.get(f"{library.origin}/v1/blueprints", timeout=10.0)
    assert blueprints.status_code == 200, blueprints.text
    assert isinstance(blueprints.json(), list)
    assert library.http.get(f"{library.origin}/v1/blueprints/none", timeout=10.0).status_code == 404


def test_shelf_and_team_routes_without_envelope_fail_closed(library: LaneApp) -> None:
    # The shelf is private: shelf reads + team writes require a certificate.
    assert library.http.get(f"{library.origin}/v1/shelf", timeout=10.0).status_code == 401
    assert library.http.get(f"{library.origin}/v1/proposals", timeout=10.0).status_code == 401


def test_manifest_is_public_and_names_this_deployment(library: LaneApp) -> None:
    served = library.http.get(f"{library.origin}/manifests/v2.json", timeout=10.0)
    assert served.status_code == 200, served.text
    bare = library.http.get(f"{library.origin}/aweb-app.json", timeout=10.0)
    assert bare.status_code == 200, bare.text
    assert bare.content == served.content
    document = json.loads(served.content)
    assert document["app"]["origin"] == library.origin
    assert document["app"]["id"] == library.app_id == E2E_APP_ID


def test_private_team_authenticates_without_the_awid_service_token(
    library_without_awid_service_token: LaneApp, namespace: LaneNamespace
) -> None:
    team = installed_team(namespace, library_without_awid_service_token)
    result = aw_request(
        team, HTTPMethod.GET, f"{library_without_awid_service_token.origin}/v1/proposals"
    )
    assert assert_aw_success(result, context="private team without service token").strip() == "[]"


def test_real_aw_team_auth_reaches_team_scoped_routes(
    library: LaneApp, namespace: LaneNamespace
) -> None:
    team = installed_team(namespace, library)
    blueprints = aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/blueprints")
    assert isinstance(
        json.loads(assert_aw_success(blueprints, context="list blueprints smoke")), list
    )
    # A valid certificate passes auth: get-binding for an unbound agent is a real
    # 404 (not 401), proving auth + the live endpoint.
    binding = aw_request(
        team, HTTPMethod.GET, f"{library.origin}/v1/agents/agent-1/profile-binding"
    )
    assert_aw_status(binding, 404, context="authenticated get-binding for unbound agent")
    proposals = aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/proposals")
    assert assert_aw_success(proposals, context="list proposals smoke").strip() == "[]"


def test_revoked_certificate_fails_after_awid_revocation_cache_refresh(
    library: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = installed_team(namespace, library)
    assert (
        assert_aw_success(
            aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/proposals"),
            context="valid certificate reaches proposals",
        ).strip()
        == "[]"
    )

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
    revoked = aw_request(team, HTTPMethod.GET, f"{library.origin}/v1/proposals")
    assert_aw_status(revoked, 401, context="revoked certificate")
