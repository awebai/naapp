from __future__ import annotations

import json
from typing import Any

import pytest
from aweb_naapp_testing.lane_team import LaneApp, LaneNamespace, assert_aw_status, aw_request
from folio_lane import provision_team
from test_e2e_smoke import _create_document, _get_document, _list_documents

pytestmark = pytest.mark.e2e


def test_cross_team_document_isolation_and_body_named_team_is_ignored(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team_a = provision_team(namespace, folio=folio, alias="alice")
    team_b = provision_team(namespace, folio=folio, alias="bob")

    created = _create_document(folio, team_a, slug="team-a-secret", title="Secret", body="team A only")
    assert created["slug"] == "team-a-secret"
    assert created["latest"]["created_by_alias"] == team_a.alias

    team_a_docs = _list_documents(folio, team_a)
    assert {item["slug"] for item in team_a_docs} == {"team-a-secret"}
    assert _list_documents(folio, team_b) == []

    team_b_read = aw_request(team_b, "GET", f"{folio.origin}/v1/documents/team-a-secret")
    assert_aw_status(team_b_read, 404, context="team B reads team A document")

    team_b_versions = aw_request(team_b, "GET", f"{folio.origin}/v1/documents/team-a-secret/versions")
    assert_aw_status(team_b_versions, 404, context="team B lists team A versions")

    team_b_append = aw_request(
        team_b,
        "POST",
        f"{folio.origin}/v1/documents/team-a-secret/versions",
        body="cross-team append attempt",
    )
    assert_aw_status(team_b_append, 404, context="team B appends team A document")

    body_named_payload: dict[str, Any] = {
        "team_id": team_a.team_id,
        "slug": "body-named-team",
        "title": "Body named team",
        "body": "the certificate decides the team, not this body field",
    }
    body_named = aw_request(
        team_b,
        "POST",
        f"{folio.origin}/v1/documents",
        body=json.dumps(body_named_payload, separators=(",", ":")),
    )
    assert body_named.returncode == 0, body_named.stderr

    assert _get_document(folio, team_b, "body-named-team")["body"] == body_named_payload["body"]
    team_a_body_named = aw_request(team_a, "GET", f"{folio.origin}/v1/documents/body-named-team")
    assert_aw_status(
        team_a_body_named,
        404,
        context="body-named team field did not write into team A",
    )
