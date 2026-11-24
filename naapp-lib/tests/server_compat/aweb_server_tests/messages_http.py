"""aweb server/tests/test_messages_http.py at server-v1.27.53, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from __future__ import annotations
from awid.did import did_from_public_key, did_key_from_signing_key, stable_id_from_public_key
from awid.signing import canonical_json_bytes, sign_message
from datetime import datetime, timedelta, timezone
from nacl.signing import SigningKey
import base64
import json

AWEB_TAG = 'server-v1.27.53'
AWEB_PATH = 'server/tests/test_messages_http.py'
AWEB_BLOB = '1eff7642d7fc3fa2e3e46b070d5b725ea258676d'
COPIED = {
    '_make_keypair': 'b0617e944c1699fa7bf4a1bc21145455f47769924c51e45d658ee8e7f90283aa',
    '_make_certificate': '6cf8020dee30a81f703ce79759791d0eab8252993cfc1cdf3de3df811dea471b',
    '_encode_certificate': '8f6cad8f5c17966482d7eb35fcf07d8c8520ed27863a255149bd874e8b6bc137',
}


def _make_keypair():
    sk = SigningKey.generate()
    pk = bytes(sk.verify_key)
    did_key = did_from_public_key(pk)
    return bytes(sk), pk, did_key


def _make_certificate(team_sk, team_did_key, member_did_key, **kwargs):
    cert = {
        "version": 1,
        "certificate_id": kwargs.get("certificate_id", "cert-001"),
        "team_id": kwargs.get("team_id", "backend:acme.com"),
        "team_did_key": team_did_key,
        "member_did_key": member_did_key,
        "member_did_aw": kwargs.get("member_did_aw", ""),
        "member_address": kwargs.get("member_address", ""),
        "alias": kwargs.get("alias", "alice"),
        "identity_scope": kwargs.get("identity_scope", "global"),
        "issued_at": kwargs.get("issued_at", datetime.now(timezone.utc).isoformat()),
    }
    payload = canonical_json_bytes(cert)
    cert["signature"] = sign_message(team_sk, payload)
    return cert


def _encode_certificate(cert):
    return base64.b64encode(json.dumps(cert).encode()).decode()
