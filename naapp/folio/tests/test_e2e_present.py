from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneNamespace,
    LaneTeam,
    assert_aw_status,
    aw_json,
    aw_request,
)
from folio_lane import provision_team
from test_e2e_smoke import _append_version, _create_document

pytestmark = pytest.mark.e2e


def _mint_present(folio: LaneApp, team: LaneTeam, **payload: Any) -> dict[str, Any]:
    result = aw_request(
        team,
        "POST",
        f"{folio.origin}/v1/present",
        body=json.dumps(payload, separators=(",", ":")),
    )
    data = aw_json(result, context=f"mint present link {payload}")
    assert isinstance(data, dict)
    return data


def test_document_bound_present_links_are_isolated_pinned_revocable_and_sanitized(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team_a = provision_team(namespace, folio=folio, alias="alice")
    team_b = provision_team(namespace, folio=folio, alias="bob")

    created = _create_document(
        folio,
        team_a,
        slug="pitch",
        title="Pitch",
        body="# Original\n\n<script>alert('x')</script>\n\n<img src=x onerror=alert(1)>\n\nHello **investor**.",
    )

    cross_team_mint = aw_request(
        team_b,
        "POST",
        f"{folio.origin}/v1/present",
        body=json.dumps({"slug": "pitch"}, separators=(",", ":")),
    )
    assert_aw_status(cross_team_mint, 404, context="team B mints team A present link")

    minted = _mint_present(folio, team_a, slug="pitch")
    token = minted["token"]
    assert minted["url"] == f"{folio.origin}/present/{token}"

    public = folio.http.get(minted["url"])
    assert public.status_code == 200, public.text
    assert "text/html" in public.headers["content-type"]
    assert "Original" in public.text
    assert "<strong>investor</strong>" in public.text
    assert "<script" not in public.text.lower()
    assert "alert(" not in public.text
    assert "<img" not in public.text.lower()
    assert "onerror" not in public.text.lower()
    assert team_a.team_id not in public.text
    assert str(created["document_id"]) not in public.text
    assert team_a.did_key not in public.text
    assert team_a.certificate_id not in public.text
    assert "version_number" not in public.text
    assert "created_by" not in public.text

    _append_version(folio, team_a, slug="pitch", body="# Updated\n\nSecond version")
    still_pinned = folio.http.get(minted["url"])
    assert still_pinned.status_code == 200, still_pinned.text
    assert "Original" in still_pinned.text
    assert "Second version" not in still_pinned.text

    explicit_second = _mint_present(folio, team_a, slug="pitch", version=2)
    explicit_second_page = folio.http.get(explicit_second["url"])
    assert explicit_second_page.status_code == 200, explicit_second_page.text
    assert "Second version" in explicit_second_page.text
    assert "Original" not in explicit_second_page.text

    missing_version = aw_request(
        team_a,
        "POST",
        f"{folio.origin}/v1/present",
        body=json.dumps({"slug": "pitch", "version": 99}, separators=(",", ":")),
    )
    assert_aw_status(missing_version, 404, context="mint missing document version")

    cross_team_revoke = aw_request(team_b, "POST", f"{folio.origin}/v1/present/{token}/revoke")
    assert_aw_status(cross_team_revoke, 404, context="team B revokes team A present link")
    assert folio.http.get(minted["url"]).status_code == 200

    revoke = aw_request(team_a, "POST", f"{folio.origin}/v1/present/{token}/revoke")
    assert aw_json(revoke, context="revoke present link") == {"revoked": True}
    revoked = folio.http.get(minted["url"])
    assert revoked.status_code == 404, revoked.text
    assert team_a.team_id not in revoked.text
    assert str(created["document_id"]) not in revoked.text

    short_lived = _mint_present(folio, team_a, slug="pitch", ttl_seconds=1)
    time.sleep(1.2)
    expired = folio.http.get(short_lived["url"])
    assert expired.status_code == 404, expired.text

    bogus = folio.http.get(f"{folio.origin}/present/not-a-real-token")
    assert bogus.status_code == 404, bogus.text
