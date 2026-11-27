"""The local key-encryption-key file is read once, at startup, and refused unless it is safe."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aweb_secrets.custody import KeyFileError, LocalKeyFileProvider

from .conftest import write_key_file


def test_a_private_regular_key_file_loads(kek_file: Path) -> None:
    LocalKeyFileProvider(kek_file)


def test_a_missing_key_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError):
        LocalKeyFileProvider(tmp_path / "absent")


def test_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError):
        LocalKeyFileProvider(tmp_path)


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "kek"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(KeyFileError):
        LocalKeyFileProvider(fifo)


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_key_file_writable_by_others_is_refused(tmp_path: Path, mode: int) -> None:
    path = write_key_file(tmp_path / "kek", os.urandom(32), mode)
    with pytest.raises(KeyFileError):
        LocalKeyFileProvider(path)


def test_a_world_readable_key_file_loads(tmp_path: Path) -> None:
    LocalKeyFileProvider(write_key_file(tmp_path / "kek", os.urandom(32), 0o644))


@pytest.mark.parametrize("length", [16, 33])
def test_a_key_of_the_wrong_length_is_refused(tmp_path: Path, length: int) -> None:
    with pytest.raises(KeyFileError):
        LocalKeyFileProvider(write_key_file(tmp_path / "kek", os.urandom(length)))


def test_the_refusal_never_carries_the_file_contents(tmp_path: Path) -> None:
    content = os.urandom(20).hex()
    path = tmp_path / "kek"
    path.write_text(content)
    path.chmod(0o600)
    with pytest.raises(KeyFileError) as refused:
        LocalKeyFileProvider(path)
    assert content not in str(refused.value)
    assert content not in repr(refused.value)


def key_init(path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "aweb_secrets.key_init", "--path", str(path)],
        capture_output=True, timeout=10, check=False,
    )


def test_explicit_initialization_creates_a_private_usable_custody_key(tmp_path):
    path = tmp_path / "custody-key"
    result = key_init(path)
    assert result.returncode == 0
    if result.stdout or result.stderr:
        raise AssertionError("key initialization printed output")
    if not re.fullmatch(rb"[0-9a-f]{64}\n", path.read_bytes()):
        raise AssertionError("key initialization wrote an invalid key")
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    provider = LocalKeyFileProvider(path)
    context = {"team_id": "test", "secret_id": "sec_test001"}
    data_key = provider.generate_data_key(context)
    if provider.decrypt_data_key(data_key.wrapped, data_key.key_id, context) != data_key.plaintext:
        raise AssertionError("initialized key did not open its wrapped data key")


def test_an_existing_key_or_symbolic_link_is_preserved(tmp_path):
    original = write_key_file(tmp_path / "existing", os.urandom(32))
    content = original.read_bytes()
    link = tmp_path / "link"
    link.symlink_to(original)
    for path in (original, link):
        result = key_init(path)
        assert result.returncode == 1
        if result.stderr != b"key-init: custody key already exists\n":
            raise AssertionError("key initialization did not report the expected refusal")
        if result.stdout or original.read_bytes() != content:
            raise AssertionError("existing key was exposed or changed")
    assert link.is_symlink()


def test_concurrent_initialization_publishes_one_complete_key(tmp_path):
    path = tmp_path / "custody-key"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(key_init, [path, path]))
    assert sorted(result.returncode for result in results) == [0, 1]
    assert list(tmp_path.iterdir()) == [path]
    LocalKeyFileProvider(path)
