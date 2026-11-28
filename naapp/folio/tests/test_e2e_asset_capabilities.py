"""Image bytes are served only under a live capability, on the real stack.

Real awid, the real coordination server, a real Folio process and a real aw
install/uninstall: nothing on the authorization path is replaced. Every image
URL is taken from what Folio renders, so the test proves what a viewer sees.
"""

from __future__ import annotations

import base64
import html
import json
import re
import subprocess
import time
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import pytest
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneClient,
    LaneNamespace,
    LaneTeam,
    aw_json,
    aw_read_bytes,
    aw_request,
    run_aw,
)
from folio_lane import FOLIO, POSTGRES_USER, FolioLane, provision_team
from playwright.sync_api import sync_playwright
from test_e2e_present import _mint_present
from test_e2e_smoke import _append_version, _create_document

pytestmark = pytest.mark.e2e

_PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_PNG_TRAILER = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_IMG_SRC = re.compile(r'<img class="folio-img" src="([^"]+)"')
_LOGO_SRC = re.compile(r'<img class="brand-logo" src="([^"]+)"')


# A decodable 1x1 PNG: a browser only reports a loaded image for real pixels.
_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\xe2\x8b\x9b"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
_LOADED_IMAGE = """([selector, source]) => [...document.querySelectorAll(selector)].some(
    (img) => (!source || (img.dataset.folioSource || img.src).includes(source))
        && img.complete && img.naturalWidth > 0)"""


def _png(marker: str) -> bytes:
    """A distinct PNG per image: the bytes say which image a response served."""
    return _PNG_HEADER + marker.encode("ascii") + _PNG_TRAILER


def _upload(folio: LaneApp, team: LaneTeam, payload: bytes) -> str:
    body = json.dumps(
        {"content_type": "image/png", "data_base64": base64.b64encode(payload).decode("ascii")},
        separators=(",", ":"),
    )
    uploaded = aw_json(
        aw_request(team, "POST", f"{folio.origin}/v1/assets", body=body), context="upload image"
    )
    return str(uploaded["asset_id"])


def _set_logo(folio: LaneApp, team: LaneTeam, payload: bytes) -> None:
    body = json.dumps(
        {
            "logo": {
                "content_type": "image/png",
                "data_base64": base64.b64encode(payload).decode("ascii"),
            }
        },
        separators=(",", ":"),
    )
    aw_json(
        aw_request(team, "PUT", f"{folio.origin}/v1/theme", body=body), context="set theme logo"
    )


def _rendered_srcs(page: httpx.Response, pattern: re.Pattern[str]) -> list[str]:
    return [urljoin(str(page.url), html.unescape(src)) for src in pattern.findall(page.text)]


def _served(response: httpx.Response, payload: bytes) -> bool:
    """The image's own bytes, as private user content of the uploaded type."""
    return (
        response.status_code == 200
        and response.content == payload
        and response.headers.get("content-type", "").startswith("image/png")
        and response.headers.get("x-content-type-options") == "nosniff"
        and response.headers.get("cache-control") == "no-store"
    )


class Refusal(NamedTuple):
    """A refusal Folio answers, as status and JSON ``detail``."""

    status: int
    detail: object


# No route serves the bare Markdown reference /assets/<uuid>.
NO_ROUTE = Refusal(404, "Not Found")
# A live link or session asked for an image its capability does not show.
NOT_SHOWN = Refusal(404, "Asset not found")
# The link or session is revoked, expired or unknown.
LINK_GONE = Refusal(404, "Presentation not found")
# A capability of an uninstalled Folio (installation_access hides the cause's status).
CAPABILITY_UNINSTALLED = Refusal(404, {"code": "installation_inactive"})
# A member route of an uninstalled Folio: naapp-lib's admission finds no active
# grant (team_grants.grant_for) and refuses before Folio's installation fence.
MEMBER_UNINSTALLED = Refusal(
    403, {"code": "scope_denied", "message": "The team has not granted this app any scopes"}
)


def _refused(response: httpx.Response, payload: bytes, refusal: Refusal) -> bool:
    """Exactly ``refusal``, and none of the image's bytes."""
    return (
        response.status_code == refusal.status
        and response.json().get("detail") == refusal.detail
        and payload not in response.content
    )


def _member_refused(
    result: subprocess.CompletedProcess[bytes], payload: bytes, refusal: Refusal
) -> bool:
    """aw's raw read answered ``refusal`` (body on stdout, status on stderr)."""
    return (
        result.returncode != 0
        and f"HTTP {refusal.status}" in result.stderr.decode(errors="replace")
        and json.loads(result.stdout).get("detail") == refusal.detail
        and payload not in result.stdout
    )


def _member_read(team: LaneTeam, url: str) -> subprocess.CompletedProcess[bytes]:
    return aw_read_bytes(team, url)


def _content_url(folio: LaneApp, asset_id: str) -> str:
    return f"{folio.origin}/v1/assets/{asset_id}/content"


# A naapp may reuse its team server's installation answer for the grant's max
# age: the aweb server's AWEB_APP_GRANT_MAX_AGE_SECONDS, at most and by default
# 60 s (server/src/aweb/config.py), which the e2e server does not lower. An
# uninstall therefore reaches Folio within that time, not at once.
GRANT_MAX_AGE_SECONDS = 60


def _wait_until_refused(
    http: LaneClient, url: str, refusal: Refusal, *, bound_seconds: float
) -> None:
    """Polls until ``url`` answers exactly ``refusal``; any other answer keeps waiting."""
    deadline = time.monotonic() + bound_seconds
    while True:
        response = http.get(url, timeout=10.0)
        if (
            response.status_code == refusal.status
            and response.json().get("detail") == refusal.detail
        ):
            return
        assert time.monotonic() < deadline, (
            f"{url} still answers {response.status_code} {response.text[:200]} after {bound_seconds}s"
        )
        time.sleep(0.5)


def test_bare_asset_reference_is_served_to_nobody(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    image = _png("bare")
    asset_id = _upload(folio, team, image)

    anonymous = folio.http.get(f"{folio.origin}/assets/{asset_id}", timeout=10.0)
    assert _refused(anonymous, image, NO_ROUTE), (anonymous.status_code, anonymous.text)


def test_present_page_images_follow_their_link(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    other = provision_team(namespace, folio=folio, alias="bob")
    first, second, logo, foreign = _png("first"), _png("second"), _png("logo"), _png("foreign")
    first_id = _upload(folio, team, first)
    second_id = _upload(folio, team, second)
    foreign_id = _upload(folio, other, foreign)
    _set_logo(folio, team, logo)
    _create_document(
        folio, team, slug="deck", title="Deck", body=f"# Deck\n\n![first](/assets/{first_id})\n"
    )
    _append_version(
        folio,
        team,
        slug="deck",
        body=f"# Deck\n\n![second](/assets/{second_id})\n",
    )

    # An older pinned version keeps showing its own image, with the team logo.
    pinned = _mint_present(folio, team, slug="deck", version=1)
    page = folio.http.get(pinned["url"], timeout=10.0)
    assert page.status_code == 200, page.text
    [image_url] = _rendered_srcs(page, _IMG_SRC)
    [logo_url] = _rendered_srcs(page, _LOGO_SRC)
    assert _served(folio.http.get(image_url, timeout=10.0), first)
    assert _served(folio.http.get(logo_url, timeout=10.0), logo)

    # The link shows only what its pinned version references.
    for unreferenced, payload in ((second_id, second), (foreign_id, foreign)):
        swapped = image_url.replace(first_id, unreferenced)
        assert swapped != image_url
        assert _refused(folio.http.get(swapped, timeout=10.0), payload, NOT_SHOWN)

    revoked = aw_request(team, "POST", f"{folio.origin}/v1/present/{pinned['token']}/revoke")
    assert aw_json(revoked, context="revoke link") == {"revoked": True}
    assert folio.http.get(pinned["url"], timeout=10.0).status_code == 404
    assert _refused(folio.http.get(image_url, timeout=10.0), first, LINK_GONE)
    assert _refused(folio.http.get(logo_url, timeout=10.0), logo, LINK_GONE)

    short = _mint_present(folio, team, slug="deck", version=1, ttl_seconds=3)
    short_page = folio.http.get(short["url"], timeout=10.0)
    assert short_page.status_code == 200, short_page.text
    [short_image] = _rendered_srcs(short_page, _IMG_SRC)
    [short_logo] = _rendered_srcs(short_page, _LOGO_SRC)
    assert _served(folio.http.get(short_image, timeout=10.0), first)
    expires_at = datetime.fromisoformat(str(short["expires_at"]).replace("Z", "+00:00"))
    time.sleep(max(0.0, (expires_at - datetime.now(UTC)).total_seconds()) + 0.5)
    assert folio.http.get(short["url"], timeout=10.0).status_code == 404
    assert _refused(folio.http.get(short_image, timeout=10.0), first, LINK_GONE)
    assert _refused(folio.http.get(short_logo, timeout=10.0), logo, LINK_GONE)


def test_member_reads_image_bytes_with_team_auth_in_its_own_team(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    other = provision_team(namespace, folio=folio, alias="bob")
    image = _png("member")
    asset_id = _upload(folio, team, image)

    read = _member_read(team, _content_url(folio, asset_id))
    assert read.returncode == 0, read.stderr.decode(errors="replace")
    assert read.stdout == image

    cross_team = _member_read(other, _content_url(folio, asset_id))
    assert _member_refused(cross_team, image, NOT_SHOWN), cross_team.stderr

    anonymous = folio.http.get(_content_url(folio, asset_id), timeout=10.0)
    # Team auth refuses a request that carries no member signature.
    assert anonymous.status_code == 401 and image not in anonymous.content, anonymous.text


def test_uninstall_ends_presented_images_and_member_reads(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    image = _png("uninstall")
    asset_id = _upload(folio, team, image)
    _create_document(
        folio, team, slug="deck", title="Deck", body=f"# Deck\n\n![one](/assets/{asset_id})\n"
    )
    link = _mint_present(folio, team, slug="deck")
    page = folio.http.get(link["url"], timeout=10.0)
    assert page.status_code == 200, page.text
    [image_url] = _rendered_srcs(page, _IMG_SRC)
    assert _served(folio.http.get(image_url, timeout=10.0), image)
    before = _member_read(team, _content_url(folio, asset_id))
    assert before.returncode == 0 and before.stdout == image, before.stderr.decode(errors="replace")

    run_aw(team.workspace, "plugin", "uninstall", folio.app_id)

    # The unrevoked link's page ends once Folio observes the uninstall.
    _wait_until_refused(
        folio.http, link["url"], CAPABILITY_UNINSTALLED, bound_seconds=GRANT_MAX_AGE_SECONDS + 10
    )
    assert _refused(folio.http.get(image_url, timeout=10.0), image, CAPABILITY_UNINSTALLED)
    after = _member_read(team, _content_url(folio, asset_id))
    assert _member_refused(after, image, MEMBER_UNINSTALLED), after.stderr


def test_editor_session_reads_images_until_revoked(
    folio: LaneApp,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    image, logo = _png("editor"), _png("editor-logo")
    asset_id = _upload(folio, team, image)
    _set_logo(folio, team, logo)
    _create_document(
        folio, team, slug="deck", title="Deck", body=f"# Deck\n\n![one](/assets/{asset_id})\n"
    )

    editable = _mint_present(folio, team, slug="deck", editable=True)
    parts = urlsplit(editable["url"])
    session_url = parts._replace(fragment="").geturl()
    opened = folio.http.post(
        session_url + "/session", json={"opener": parse_qs(parts.fragment)["k"][0]}, timeout=10.0
    )
    assert opened.status_code == 200, opened.text
    snapshot = opened.json()
    headers = {"X-Folio-Edit-Session": snapshot["session_secret"]}

    [snapshot_image] = [
        urljoin(session_url, html.unescape(src)) for src in _IMG_SRC.findall(snapshot["html"])
    ]
    logo_url = urljoin(session_url, snapshot["theme"]["logo_url"])
    preview = folio.http.post(
        session_url + "/preview",
        headers=headers,
        json={"body": f"Draft\n\n![one](/assets/{asset_id})\n"},
        timeout=10.0,
    )
    assert preview.status_code == 200, preview.text
    [preview_image] = _rendered_srcs(preview, _IMG_SRC)
    for url, payload in ((snapshot_image, image), (preview_image, image), (logo_url, logo)):
        assert _served(folio.http.get(url, headers=headers, timeout=10.0), payload)
        assert _refused(folio.http.get(url, timeout=10.0), payload, LINK_GONE)

    revoked = aw_request(team, "POST", f"{folio.origin}/v1/present/{editable['token']}/revoke")
    assert aw_json(revoked, context="revoke editable link") == {"revoked": True}
    for url, payload in ((preview_image, image), (logo_url, logo)):
        assert _refused(folio.http.get(url, headers=headers, timeout=10.0), payload, LINK_GONE)


def _stored_copies(folio_lane: FolioLane, token: str) -> list[str]:
    """Every user-table column, in any schema of Folio's database, whose value
    contains the token, read in one read-only query."""
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token), token
    found = folio_lane.lane.query(
        POSTGRES_USER,
        FOLIO.database,
        f"""SELECT format('%I.%I.%I', c.table_schema, c.table_name, c.column_name)
        FROM information_schema.columns c
        WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
          AND c.data_type IN ('text', 'character varying', 'jsonb', 'json', 'bytea')
          AND (xpath('/row/found/text()', query_to_xml(
            CASE WHEN c.data_type = 'bytea'
              THEN format('SELECT EXISTS (SELECT 1 FROM %I.%I WHERE '
                          'position(convert_to(%L, ''UTF8'') in %I) > 0) AS found',
                          c.table_schema, c.table_name, '{token}', c.column_name)
              ELSE format('SELECT EXISTS (SELECT 1 FROM %I.%I WHERE '
                          'strpos(%I::text, %L) > 0) AS found',
                          c.table_schema, c.table_name, c.column_name, '{token}')
            END, false, true, '')))[1]::text = 'true'""",
    )
    return found.splitlines()


def test_read_only_token_is_stored_only_as_a_verifier(
    folio: LaneApp,
    folio_lane: FolioLane,
    namespace: LaneNamespace,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    _create_document(folio, team, slug="deck", title="Deck", body="# Deck\n\nBody")
    link = _mint_present(folio, team, slug="deck")
    token = str(link["token"])
    assert urlsplit(link["url"]).path == f"/present/{token}"

    assert _stored_copies(folio_lane, token) == []
    assert folio.http.get(link["url"], timeout=10.0).status_code == 200
    revoked = aw_request(team, "POST", f"{folio.origin}/v1/present/{token}/revoke")
    assert aw_json(revoked, context="revoke by minted token") == {"revoked": True}
    assert folio.http.get(link["url"], timeout=10.0).status_code == 404


def test_browser_shows_images_on_the_present_page_and_in_the_editor(
    folio: LaneApp,
    namespace: LaneNamespace,
    lane_browser: str,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    first_id = _upload(folio, team, _PIXEL_PNG)
    second_id = _upload(folio, team, _PIXEL_PNG)
    _set_logo(folio, team, _PIXEL_PNG)
    _create_document(
        folio, team, slug="deck", title="Deck", body=f"# Deck\n\n![first](/assets/{first_id})\n"
    )
    _append_version(
        folio,
        team,
        slug="deck",
        body=f"# Deck\n\n![second](/assets/{second_id})\n",
    )
    pinned = _mint_present(folio, team, slug="deck", version=1)
    editable = _mint_present(folio, team, slug="deck", editable=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect(lane_browser)
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.goto(pinned["url"])
            page.wait_for_function(_LOADED_IMAGE, arg=["img.folio-img", first_id], timeout=10000)
            page.wait_for_function(_LOADED_IMAGE, arg=["img.brand-logo", ""], timeout=10000)

            editor = browser.new_page(ignore_https_errors=True)
            editor.goto(editable["url"])
            editor.wait_for_function(
                _LOADED_IMAGE, arg=["#preview img.folio-img", second_id], timeout=10000
            )
            editor.wait_for_function(_LOADED_IMAGE, arg=[".brand-logo", ""], timeout=10000)
            editor.locator("#body").fill(f"Draft\n\n![first](/assets/{first_id})\n")
            editor.wait_for_function(
                _LOADED_IMAGE, arg=["#preview img.folio-img", first_id], timeout=10000
            )
        finally:
            browser.close()


def test_editor_marks_refused_images_and_retries_failed_reads(
    folio: LaneApp,
    namespace: LaneNamespace,
    lane_browser: str,
) -> None:
    team = provision_team(namespace, folio=folio, alias="alice")
    asset_id = _upload(folio, team, _PIXEL_PNG)
    _create_document(
        folio, team, slug="deck", title="Deck", body=f"# Deck\n\n![one](/assets/{asset_id})\n"
    )
    image_path = f"**/assets/{asset_id}"

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect(lane_browser)
        try:
            # An outage on the first read: marked as retrying, then shown.
            failures = [503]
            retrying = browser.new_page(ignore_https_errors=True)
            retrying.route(
                image_path,
                lambda route: (
                    route.fulfill(status=failures.pop()) if failures else route.continue_()
                ),
            )
            retrying.goto(_mint_present(folio, team, slug="deck", editable=True)["url"])
            retrying.wait_for_selector('#preview img[data-folio-image="retrying"]', timeout=10000)
            retrying.wait_for_function(
                _LOADED_IMAGE, arg=["#preview img.folio-img", asset_id], timeout=15000
            )
            assert retrying.locator("#preview img[data-folio-image]").count() == 0

            # A refusal: marked as refused, not retried into a blank.
            refused = browser.new_page(ignore_https_errors=True)
            refused.route(image_path, lambda route: route.fulfill(status=404))
            refused.goto(_mint_present(folio, team, slug="deck", editable=True)["url"])
            refused.wait_for_selector('#preview img[data-folio-image="refused"]', timeout=10000)
        finally:
            browser.close()
