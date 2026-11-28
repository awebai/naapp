from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneNamespace,
    assert_aw_status,
    aw_json,
    aw_request,
)
from folio_lane import provision_team
from test_e2e_asset_capabilities import _member_read, _served
from test_e2e_present import _mint_present
from test_e2e_smoke import _create_document

pytestmark = pytest.mark.e2e

_ONE_BY_ONE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\xe2\x8b\x9b"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
_ONE_BY_ONE_PNG = base64.b64encode(_ONE_BY_ONE_PNG_BYTES).decode("ascii")
_SVG_WITH_SCRIPT = base64.b64encode(
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
).decode("ascii")


def _upload_asset(
    folio: LaneApp,
    team: Any,
    *,
    content_type: str = "image/png",
    data_base64: str = _ONE_BY_ONE_PNG,
) -> dict[str, Any]:
    result = aw_request(
        team,
        "POST",
        f"{folio.origin}/v1/assets",
        body=json.dumps(
            {"content_type": content_type, "data_base64": data_base64}, separators=(",", ":")
        ),
    )
    data = aw_json(result, context="upload asset")
    assert isinstance(data, dict)
    return data


def test_image_asset_upload_embed_render_and_team_scoped_sanitizer(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team_a = provision_team(namespace, folio=folio, alias="alice")
    team_b = provision_team(namespace, folio=folio, alias="bob")

    invalid_svg = aw_request(
        team_a,
        "POST",
        f"{folio.origin}/v1/assets",
        body=json.dumps(
            {"content_type": "image/svg+xml", "data_base64": _SVG_WITH_SCRIPT},
            separators=(",", ":"),
        ),
    )
    assert_aw_status(invalid_svg, 400, context="reject SVG asset upload")
    mismatched = aw_request(
        team_a,
        "POST",
        f"{folio.origin}/v1/assets",
        body=json.dumps(
            {"content_type": "image/png", "data_base64": _SVG_WITH_SCRIPT}, separators=(",", ":")
        ),
    )
    assert_aw_status(mismatched, 400, context="reject asset magic byte mismatch")

    asset_a = _upload_asset(folio, team_a)
    assert asset_a["content_type"] == "image/png"
    assert asset_a["url"] == f"{folio.origin}/assets/{asset_a['asset_id']}"

    # The url is the Markdown reference; members read the bytes with team auth.
    fetched = _member_read(team_a, f"{folio.origin}/v1/assets/{asset_a['asset_id']}/content")
    assert fetched.returncode == 0, fetched.stderr.decode(errors="replace")
    assert fetched.stdout == _ONE_BY_ONE_PNG_BYTES

    _create_document(
        folio,
        team_a,
        slug="deck",
        title="Deck",
        body=(
            f"# Deck\n\n![safe chart]({asset_a['url']})\n\n"
            "![external](https://example.com/hotlink.png)\n\n"
            "![data](data:image/png;base64,AAAA)\n\n"
            "<script>alert('x')</script>"
        ),
    )
    presented = _mint_present(folio, team_a, slug="deck")
    page = folio.http.get(presented["url"], timeout=10.0)
    assert page.status_code == 200, page.text
    image_url = f"{presented['url']}/assets/{asset_a['asset_id']}"
    assert f'src="{image_url}"' in page.text
    assert _served(folio.http.get(image_url, timeout=10.0), _ONE_BY_ONE_PNG_BYTES)
    assert "safe chart" in page.text
    assert "example.com" not in page.text
    assert "data:image" not in page.text
    assert "<script" not in page.text.lower()
    assert "alert(" not in page.text

    _create_document(
        folio,
        team_b,
        slug="borrowed",
        title="Borrowed",
        body=f"# Borrowed\n\n![other team]({asset_a['url']})",
    )
    b_page = folio.http.get(_mint_present(folio, team_b, slug="borrowed")["url"], timeout=10.0)
    assert b_page.status_code == 200, b_page.text
    assert "<img" not in b_page.text.lower()
    assert str(asset_a["asset_id"]) not in b_page.text
