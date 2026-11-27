"""What a lane team's public request sends: the query, the JSON body and its type."""

from __future__ import annotations

import json

from aweb_naapp_testing.lane_team import public_request


def test_params_join_the_query_and_json_is_sent_as_json() -> None:
    url, body, headers = public_request(
        "https://library.svc.test/v1/blueprints",
        params={"tags": "e2e first"},
        json={"tags": ["a"]},
        headers={"X-Probe": "1"},
    )
    assert url == "https://library.svc.test/v1/blueprints?tags=e2e+first"
    assert json.loads(body) == {"tags": ["a"]}
    assert headers == [("X-Probe", "1"), ("Content-Type", "application/json")]


def test_a_request_without_body_or_params_is_sent_as_given() -> None:
    assert public_request("https://library.svc.test/v1/shelf?x=1") == (
        "https://library.svc.test/v1/shelf?x=1",
        None,
        [],
    )


def test_params_extend_a_query_the_url_already_has() -> None:
    url, _, _ = public_request("https://a.svc.test/p?x=1", params={"y": "2"})
    assert url == "https://a.svc.test/p?x=1&y=2"
