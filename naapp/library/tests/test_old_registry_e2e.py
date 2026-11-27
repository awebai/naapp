"""Real pre-status AWID refuses private-team verification without fallback."""

from __future__ import annotations

from http import HTTPMethod

import pytest
from aweb_naapp_testing.compose_lane import REGISTRY_ORIGIN
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneNamespace,
    assert_aw_status,
    assert_aw_success,
    aw_request,
)
from test_e2e_smoke import PRE_STATUS_REGISTRY, installed_team

pytestmark = pytest.mark.e2e


def test_private_certificate_refused_by_real_pre_status_registry(
    namespace: LaneNamespace,
    library_without_awid_service_token: LaneApp,
    library_on_pre_status_registry: LaneApp,
) -> None:
    team = installed_team(namespace, library_without_awid_service_token)
    team_path = f"/v1/namespaces/{team.namespace}/teams/{team.team}"
    status_path = f"{team_path}/certificates/{team.certificate_id}/status"
    http = library_without_awid_service_token.http

    # Both actual registry builds see the same real private team and certificate.
    # The old build has no /status route; no response or verifier is substituted.
    for registry in (PRE_STATUS_REGISTRY, REGISTRY_ORIGIN):
        private = http.get(registry + team_path)
        assert private.status_code == 403, private.text
        assert private.json()["detail"]["code"] == "team_private"
    missing_route = http.get(PRE_STATUS_REGISTRY + status_path)
    assert missing_route.status_code == 404, missing_route.text
    assert missing_route.json() == {"detail": "Not Found"}
    current_status = http.get(REGISTRY_ORIGIN + status_path)
    assert current_status.status_code == 200, current_status.text
    assert current_status.json()["status"] == "active"

    result = aw_request(
        team, HTTPMethod.GET, f"{library_on_pre_status_registry.origin}/v1/proposals"
    )
    assert_aw_status(result, 401, context="private certificate on registry without status")
    current = aw_request(
        team, HTTPMethod.GET, f"{library_without_awid_service_token.origin}/v1/proposals"
    )
    assert (
        assert_aw_success(current, context="same certificate on current registry").strip() == "[]"
    )
