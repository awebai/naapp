"""The sha256 of every manifest document an app serves, for fixed deployment inputs.

Run in the app's own environment, from the repository root:

    uv run --project naapp/<app> python scripts/served_manifest_digests.py <app>

It prints one JSON object mapping each served document to the sha256 of its
bytes. Installations pin an app by its manifest digest, so these must not
change when the code moves between repositories or packages:
scripts/test_served_manifest_digests.py compares them with the recorded ones.

The deployment inputs are fixed: the origins, a self-hosted app id, and a key
file per declared kid whose seed is derived from the app and the kid. Each app
builds its manifests with its own code and constants, the way it does when it
serves.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def _app_id(app: str) -> str:
    return f"example-{app}"


def _origin(app: str) -> str:
    return f"https://{app}.example.test"


def _console(app: str) -> str:
    return f"https://console.{app}.example.test"


def _key_files(app: str, template: dict[str, Any], directory: Path) -> dict[str, Path]:
    from aweb_naapp.manifest import template_kids

    files = {}
    for index, kid in enumerate(template_kids(template)):
        path = directory / f"key-{index}"
        path.write_text(hashlib.sha256(f"served-manifest-digests/{app}/{kid}".encode()).hexdigest() + "\n")
        path.chmod(0o600)
        files[kid] = path
    return files


def _runtime_documents(runtime: Any) -> dict[str, bytes]:
    """The manifest documents a runtime serves: the current manifest, each
    published version and the catalog of versions."""
    documents = {"/aweb-app.json": runtime.manifest_bytes}
    for version in (1, 2, 3):
        try:
            documents[f"/manifests/v{version}.json"] = runtime.manifest_variant_bytes(version)
        except KeyError:
            pass
    if runtime.catalog_bytes is not None:
        documents["/.well-known/aweb-app-versions.json"] = runtime.catalog_bytes
    return documents


def _folio(directory: Path) -> dict[str, bytes]:
    from folio.aweb_manifest import MANIFEST_PATH

    # folio.api builds the served app from its environment when imported.
    template = json.loads(MANIFEST_PATH.read_bytes())
    os.environ.update(
        FOLIO_PUBLIC_ORIGIN=_origin("folio"),
        FOLIO_APP_ID=_app_id("folio"),
        FOLIO_APP_KEY_FILES=json.dumps(
            {kid: str(path) for kid, path in _key_files("folio", template, directory).items()}
        ),
    )
    import folio.api

    return _runtime_documents(folio.api.app.state.folio_runtime)


def _library(directory: Path) -> dict[str, bytes]:
    from fastapi.testclient import TestClient

    # library.api builds the served app from its environment when imported.
    os.environ.update(LIBRARY_PUBLIC_ORIGIN=_origin("library"))
    import library.api

    client = TestClient(library.api.app)
    documents = {}
    for path in ("/aweb-app.json", "/.well-known/aweb-app.json"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        documents[path] = response.content
    return documents


def _logs(directory: Path) -> dict[str, bytes]:
    from aweb_logs import MANIFEST_PATH, MIGRATIONS_MODULE, MIGRATIONS_PATH
    from aweb_logs.config import LogsSettings
    from aweb_logs.reader_acts import OWNER_ACTS
    from aweb_naapp.runtime.app import NaappRuntime

    template = json.loads(MANIFEST_PATH.read_bytes())
    settings = LogsSettings(
        _env_file=None,
        database_url="postgresql://localhost/unused",
        purge_ledger_database_url="postgresql://localhost/unused",
        public_origin=_origin("logs"),
        console_origin=_console("logs"),
        app_id=_app_id("logs"),
        app_key_files=_key_files("logs", template, directory),
    )
    # The arguments aweb_logs.app.create_runtime gives the runtime's manifest.
    runtime = NaappRuntime(
        settings,
        module_name=MIGRATIONS_MODULE,
        migrations_path=MIGRATIONS_PATH,
        manifest_path=MANIFEST_PATH,
        owner_acts=OWNER_ACTS,
        owner_acts_origin=settings.console_origin,
    )
    return _runtime_documents(runtime)


def _secrets(directory: Path) -> dict[str, bytes]:
    from aweb_naapp.runtime.app import NaappRuntime
    from aweb_secrets.app import MIGRATIONS_PATH, MODULE_NAME
    from aweb_secrets.config import SecretsSettings
    from aweb_secrets.manifest import MANIFEST_PATH, OWNER_ACTS

    template = json.loads(MANIFEST_PATH.read_bytes())
    custody = directory / "custody-key"
    custody.write_text(hashlib.sha256(b"served-manifest-digests/secrets/custody").hexdigest() + "\n")
    custody.chmod(0o600)
    settings = SecretsSettings(
        _env_file=None,
        database_url="postgresql://localhost/unused",
        public_origin=_origin("secrets"),
        console_origin=_console("secrets"),
        app_id=_app_id("secrets"),
        app_key_files=_key_files("secrets", template, directory),
        custody_key_file=custody,
        email_smtp_host="smtp.example.test",
        email_username="unused",
        email_from="secrets@example.test",
    )
    # The arguments aweb_secrets.app.create_app gives the runtime's manifest.
    runtime = NaappRuntime(
        settings,
        module_name=MODULE_NAME,
        migrations_path=MIGRATIONS_PATH,
        manifest_path=MANIFEST_PATH,
        owner_acts=OWNER_ACTS,
        owner_acts_origin=settings.console_origin,
    )
    return _runtime_documents(runtime)


def _tasks(directory: Path) -> dict[str, bytes]:
    from aweb_naapp.runtime.app import NaappRuntime
    from aweb_naapp.runtime.installation import apply_installation_migrations
    from aweb_tasks import MANIFEST_PATH
    from aweb_tasks.app import MIGRATIONS_PATH
    from aweb_tasks.config import TasksSettings

    template = json.loads(MANIFEST_PATH.read_bytes())
    settings = TasksSettings(
        _env_file=None,
        database_url="postgresql://localhost/unused",
        public_origin=_origin("tasks"),
        app_id=_app_id("tasks"),
        app_key_files=_key_files("tasks", template, directory),
    )
    # The arguments aweb_tasks.app.create_app gives the runtime's manifest.
    runtime = NaappRuntime(
        settings,
        module_name="tasks",
        migrations_path=MIGRATIONS_PATH,
        manifest_path=MANIFEST_PATH,
        stores=(apply_installation_migrations,),
    )
    return _runtime_documents(runtime)


APPS = {"folio": _folio, "library": _library, "logs": _logs, "secrets": _secrets, "tasks": _tasks}


def digests(app: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as directory:
        documents = APPS[app](Path(directory))
    return {path: hashlib.sha256(data).hexdigest() for path, data in sorted(documents.items())}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in APPS:
        raise SystemExit(f"usage: {sys.argv[0]} {{{','.join(APPS)}}}")
    # Folio's and Library's settings also read ./.env, which would change the inputs.
    if Path(".env").exists():
        raise SystemExit("refusing to run beside a .env file")
    print(json.dumps(digests(sys.argv[1]), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
