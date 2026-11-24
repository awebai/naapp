"""Shared output checks stay usable without the app or a database."""

import base64
import hashlib
import subprocess
import sys
from time import thread_time

from aweb_secrets_values.credential_shapes import has_credential_shape
from aweb_secrets_values.redaction import KnownValues


def test_same_known_value_encodings_and_partial_tail_checks():
    known = KnownValues()
    known.add_basic("fixture", "user", "fixture-password")
    encoded = base64.b64encode(b"user:fixture-password")
    assert known.redact(encoded) == (b"[REDACTED:fixture]", 1)
    assert known.redact_tail(b"prefix fixture-pass") == (b"prefix [REDACTED:fixture]", 1)


def test_shape_detection_remains_independent_of_known_values():
    assert has_credential_shape(b'{"password":"undeclared-fixture"}')
    assert not has_credential_shape(b'{"message":"finished"}')


def test_maximum_ordinary_response_leaves_time_for_shape_inspection():
    ceiling = 64 * 1024 * 1024
    opening, closing = b'{"data":"', b'"}'
    body = opening + b"." * (ceiling - len(opening) - len(closing)) + closing
    known = KnownValues()
    for index in range(4):
        name = f"fixture-{index}"
        known.add(name, hashlib.sha256(name.encode()).hexdigest())

    started = thread_time()
    assert known.redact(body) == (body, 0)
    assert known.redact_json(body) == (body, 0)
    # Leave most of the broker's 15-second budget for the other inspection steps.
    elapsed = thread_time() - started
    assert elapsed < 5, f"known-value redaction took {elapsed:.2f} seconds of CPU"


def test_encoded_matches_keep_the_original_priority_and_replace_a_single_byte():
    known = KnownValues()
    known.add("first", "/fixture")
    known.add("second", "/fixture")
    known.add("short", "/")
    known.add("one-byte", "!")

    assert known.redact(b"%2ffixture /fixture %2F !") == (
        b"[REDACTED:first] [REDACTED:first] [REDACTED:short] [REDACTED:one-byte]",
        4,
    )


def test_output_check_runtime_imports_are_standalone():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from aweb_secrets_values.redaction import KnownValues; "
            "from aweb_secrets_values.credential_shapes import has_credential_shape; "
            "assert not any(k.startswith(('aweb_naapp','aweb_secrets.','awid','pgdbm')) for k in sys.modules)",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
