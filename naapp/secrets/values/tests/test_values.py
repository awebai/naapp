"""Freeze existing bytes and validation, including use without app dependencies."""

import json
import subprocess
import sys

import pytest

from aweb_secrets_values import ValueInvalid, decode_value, encode_value

CANONICAL_VALUES = {
    "bearer": b'{"value":"fixture"}',
    "header": b'{"value":"fixture"}',
    "query": b'{"value":"fixture"}',
    "basic": b'{"password":"fixture","username":"fixture"}',
    "oauth2-client-credentials": b'{"client_id":"fixture","client_secret":"fixture"}',
    "oauth2-certificate-credentials": (
        b'{"certificate_pem":"certificate text","client_id":"fixture",'
        b'"key_id":"fixture-key-id","private_key_pem":"private key text"}'
    ),
    "aws-sigv4": (
        b'{"access_key_id":"fixture","secret_access_key":"fixture","session_token":"fixture"}'
    ),
}


@pytest.mark.parametrize("kind", CANONICAL_VALUES)
def test_every_kind_preserves_canonical_bytes_and_round_trips(kind):
    expected = CANONICAL_VALUES[kind]
    value = dict(reversed(tuple(json.loads(expected).items())))
    assert encode_value(kind, value) == expected
    assert decode_value(kind, expected) == value


def test_existing_unicode_bytes_and_optional_fields_are_preserved():
    assert encode_value("bearer", {"value": "é"}) == b'{"value":"\\u00e9"}'
    basic = {"username": "", "password": "fixture"}
    assert encode_value("basic", basic) == b'{"password":"fixture","username":""}'
    assert decode_value("basic", b'{"username":"","password":"fixture"}') == basic
    assert decode_value(
        "aws-sigv4", b'{"access_key_id":"fixture","secret_access_key":"fixture"}'
    ) == {"access_key_id": "fixture", "secret_access_key": "fixture"}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"value":"untrusted-codec-field","value":"two"}',
        b'{"value":42}',
        b'{"value":""}',
        b'{"value":"untrusted-codec-field","extra":"extra"}',
        b"{",
        b"{}",
        b"null",
        b"[]",
        b"\xff",
        b'{"value":"\\ud800"}',
    ],
)
def test_invalid_material_is_not_retained_in_errors(raw):
    with pytest.raises(ValueInvalid) as error:
        decode_value("bearer", raw)
    assert "untrusted-codec-field" not in str(error.value)
    assert "untrusted-codec-field" not in repr(error.value)
    assert error.value.__cause__ is None


def test_runtime_import_is_standalone():
    script = """
import sys
from aweb_secrets_values import encode_value
assert encode_value('bearer', {'value':'fixture'}) == b'{"value":"fixture"}'
assert not any(k.startswith(('aweb_naapp','aweb_secrets.','awid','pgdbm')) for k in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()
