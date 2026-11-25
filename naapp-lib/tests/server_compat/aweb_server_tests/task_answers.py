"""aweb server/tests/test_task_answers.py at 2788969b6fce509437eab53637927bb97cf18b58, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from awid.signing import canonical_json_bytes, sign_message
from datetime import datetime, timezone
import base64
import hashlib
from tests.server_compat.aweb_server_tests.dashboard_privacy import TEAM as LIVE_TEAM

# Copied from an aweb commit, not a release: replaced by the re-copy
# from the first server tag that carries this change.
AWEB_REF = '2788969b6fce509437eab53637927bb97cf18b58'
AWEB_PATH = 'server/tests/test_task_answers.py'
AWEB_BLOB = 'cd32c4823c73280c370cded3019dc8e6d351b297'
COPIED = {
    '_v2_request': '920faaa21bb770913be9d96c4fd586915c71bcadda674fa4bfb89405a45591bb',
}


async def _v2_request(env, alias, method, path, body=None):
    actor = env.actors[alias]
    content = canonical_json_bytes(body) if body is not None else b""
    timestamp = datetime.now(timezone.utc).isoformat()
    origin = str(env.client.base_url).rstrip("/")
    env.app.state.public_origin = origin
    signed = canonical_json_bytes(dict(v=2, aud=origin, method=method, path=path,
        team_id=LIVE_TEAM, timestamp=timestamp, body_sha256=hashlib.sha256(content).hexdigest()))
    headers = {
        "Authorization": f"DIDKey {actor.did} {sign_message(actor.sk, signed)}",
        "X-AWID-Team-Certificate": actor.certificate,
        "X-AWEB-Signed-Payload": base64.urlsafe_b64encode(signed).decode().rstrip("="),
        "X-AWEB-Timestamp": timestamp, "Content-Type": "application/json",
    }
    return await env.client.request(method, path, content=content, headers=headers)
