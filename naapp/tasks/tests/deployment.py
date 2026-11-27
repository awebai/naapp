"""A Tasks deployment's configuration for the tests: a key file per kid its
committed manifest template declares, and the manifest bytes it then serves."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from aweb_naapp.manifest import fill_manifest
from aweb_naapp.signing_key import read_signing_key_file
from awid.did import did_from_public_key

from aweb_tasks import MANIFEST_PATH

# A deployment's own app id: the hosted id is refused at any other reachable origin.
TEST_APP_ID = "tasks-test"


def template() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_bytes())


def write_key_files(directory: Path) -> dict[str, Path]:
    """A freshly generated key file for each kid of the template."""
    files = {}
    for index, entry in enumerate(template()["app_keys"]):
        path = directory / f"key-{index}"
        path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        path.chmod(0o600)
        files[entry["kid"]] = path
    return files


def served_manifest(key_files: dict[str, Path], *, origin: str) -> bytes:
    """The bytes a deployment with these key files serves at ``origin``."""
    return fill_manifest(
        template(),
        app_id=TEST_APP_ID,
        origin=origin,
        did_keys={
            kid: did_from_public_key(bytes(read_signing_key_file(path).verify_key))
            for kid, path in key_files.items()
        },
    )
