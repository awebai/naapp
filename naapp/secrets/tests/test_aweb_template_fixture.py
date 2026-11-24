"""aweb's CLI tests the Secrets run against a copy of this manifest template.

aweb keeps the copy at cli/go/internal/appmanifest/testdata/secrets-template.json,
with this digest in that directory's README. A change to the template here has
to reach that copy too, or aweb's CLI keeps testing the old template.
"""

import hashlib

from aweb_secrets.manifest import MANIFEST_PATH

AWEB_FIXTURE_SHA256 = "a4016cef63a56fb10233580df9ff1e231129404cf8c60c8d103382cf66b9cac5"


def test_the_template_matches_the_copy_aweb_tests_its_cli_against():
    digest = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    assert digest == AWEB_FIXTURE_SHA256, (
        f"Secrets' manifest template now has sha256 {digest}. Update aweb's "
        "cli/go/internal/appmanifest/testdata/secrets-template.json to these bytes "
        "and its README's digest, then AWEB_FIXTURE_SHA256 here."
    )
