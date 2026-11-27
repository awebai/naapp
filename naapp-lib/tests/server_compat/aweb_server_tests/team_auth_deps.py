"""aweb server/tests/test_team_auth_deps.py at server-v1.27.53, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from __future__ import annotations
from awid.signing import canonical_json_bytes, sign_message
from datetime import datetime, timedelta, timezone
import base64
import json

AWEB_REF = 'server-v1.27.53'
AWEB_PATH = 'server/tests/test_team_auth_deps.py'
AWEB_BLOB = '90ed05a566e2619b43abf9f990945bbfe6e05639'
COPIED = {
    '_make_certificate': 'fb7ee0c487c74b338fc1f9f68071ac046b66bd687d15b93da4c13de3893c4a57',
    '_encode_certificate': '8f6cad8f5c17966482d7eb35fcf07d8c8520ed27863a255149bd874e8b6bc137',
}


def _make_certificate(team_sk, team_did_key, member_did_key, **kwargs):
    cert = {
        "version": 1,
        "certificate_id": kwargs.get("certificate_id", "cert-001"),
        "team_id": kwargs.get("team_id", "backend:acme.com"),
        "team_did_key": team_did_key,
        "member_did_key": member_did_key,
        "member_did_aw": "",
        "member_address": "",
        "alias": kwargs.get("alias", "alice"),
        "identity_scope": kwargs.get("identity_scope", "global"),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    payload = canonical_json_bytes(cert)
    sig = sign_message(team_sk, payload)
    cert["signature"] = sig
    return cert


def _encode_certificate(cert):
    return base64.b64encode(json.dumps(cert).encode()).decode()
