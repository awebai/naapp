from __future__ import annotations

import pytest
from aweb_naapp.runtime.settings import NaappSettings
from aweb_naapp.runtime.team_auth import DidAwProfile

from folio.config import Settings


def test_folio_observes_did_aw_unless_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOLIO_DID_AW_PROFILE", raising=False)

    assert Settings(_env_file=None).did_aw_profile is DidAwProfile.OBSERVE


def test_folio_settings_are_the_shared_naapp_settings_under_its_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOLIO_DID_AW_PROFILE", "require")
    monkeypatch.setenv("FOLIO_APP_EVENTS_ORIGIN", "https://core.example")

    settings = Settings(_env_file=None)

    assert isinstance(settings, NaappSettings)
    assert settings.did_aw_profile is DidAwProfile.REQUIRE
    assert settings.app_events_origin == "https://core.example"


def test_documented_environment_starts_with_valid_shared_auth_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    from pathlib import Path

    for name in tuple(os.environ):
        if name.startswith("FOLIO_"):
            monkeypatch.delenv(name)
    example = Path(__file__).resolve().parents[1] / ".env.example"
    settings = Settings(_env_file=example)

    assert 1 <= settings.auth_cache_ttl_seconds <= 60
    assert 1 <= settings.timestamp_skew_seconds <= 300


def test_present_defaults_allow_thirty_days_but_keep_one_day_as_default(monkeypatch):
    monkeypatch.delenv("FOLIO_DEFAULT_PRESENT_TTL_SECONDS", raising=False)
    monkeypatch.delenv("FOLIO_MAX_PRESENT_TTL_SECONDS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.default_present_ttl_seconds == 86_400
    assert settings.max_present_ttl_seconds == 2_592_000


def test_present_operator_limit_can_narrow_but_not_exceed_thirty_days(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("FOLIO_MAX_PRESENT_TTL_SECONDS", "604800")
    assert Settings(_env_file=None).max_present_ttl_seconds == 604_800
    monkeypatch.setenv("FOLIO_MAX_PRESENT_TTL_SECONDS", "2592001")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_an_unrelated_env_file_in_the_working_directory_is_not_configuration(tmp_path) -> None:
    # Folio builds its app when folio.api is imported. A .env in whatever
    # directory the process starts in (a developer's, another app's) must not
    # become its configuration; only the environment configures it.
    import os
    import subprocess
    import sys

    (tmp_path / ".env").write_text(
        "FOLIO_PUBLIC_ORIGIN=https://planted.example\nFOLIO_DEFAULT_PRESENT_TTL_SECONDS=7\n"
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("FOLIO_PUBLIC_ORIGIN", "FOLIO_DEFAULT_PRESENT_TTL_SECONDS")
    }
    started = subprocess.run(
        [
            sys.executable,
            "-c",
            "import folio.api; s = folio.api.app.state.folio_runtime.settings; "
            "print(s.public_origin, s.default_present_ttl_seconds)",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert started.returncode == 0, started.stderr
    assert started.stdout.split() == ["http://127.0.0.1:8765", "86400"]
