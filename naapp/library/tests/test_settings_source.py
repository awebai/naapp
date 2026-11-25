from __future__ import annotations


def test_an_env_file_in_the_working_directory_is_not_configuration(tmp_path, monkeypatch) -> None:
    # Only the environment configures the app: a .env in whatever directory the
    # process starts in (a developer's, another app's) does not.
    from library.config import get_settings

    (tmp_path / ".env").write_text("LIBRARY_AWID_REGISTRY_URL=https://planted.example\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LIBRARY_AWID_REGISTRY_URL", raising=False)

    assert get_settings().awid_registry_url == "https://api.awid.ai"
