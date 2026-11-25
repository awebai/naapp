"""The ops tools' evidence directory: private, written once, bounded, anchored."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from email.message import Message
from pathlib import Path

import pytest

from aweb_naapp.ops import evidence as ops_evidence
from aweb_naapp.ops.evidence import EvidenceError
from aweb_naapp.ops.git import GIT_REPOSITORY_ENV

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run(path: Path, *, label: str) -> ops_evidence.HealthEvidenceRun:
    return ops_evidence.HealthEvidenceRun(
        path, label=label, schema="test.health-evidence.v1", repository_root=REPOSITORY_ROOT
    )


def test_evidence_refuses_inside_repo_from_subdirectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPOSITORY_ROOT / "naapp-lib")
    path = REPOSITORY_ROOT / ".pytest-inside-evidence"
    with pytest.raises(EvidenceError, match="outside the repository"):
        _run(path, label="test")
    assert not path.exists()


def test_evidence_no_replace_preserves_existing_destination(tmp_path: Path) -> None:
    evidence = _run(tmp_path / "evidence", label="test")
    destination = evidence.path / "002.json"
    destination.write_text("preserve", encoding="utf-8")
    with pytest.raises(EvidenceError, match="must not already exist"):
        evidence.record({"probe_kind": "test"})
    assert destination.read_text(encoding="utf-8") == "preserve"
    evidence.close()


def test_evidence_enforces_exact_modes_under_restrictive_umask(tmp_path: Path) -> None:
    previous = os.umask(0o777)
    try:
        evidence = _run(tmp_path / "evidence", label="test")
    finally:
        os.umask(previous)
    assert evidence.path.stat().st_mode & 0o777 == 0o700
    assert (evidence.path / "001.json").stat().st_mode & 0o777 == 0o600
    evidence.close()


def test_evidence_refuses_symlink_component(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(EvidenceError, match="without symlink components"):
        _run(symlink_parent / "evidence", label="test")


def test_evidence_rejects_non_private_parent_mode(tmp_path: Path) -> None:
    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o750)
    parent.chmod(0o750)
    evidence_path = parent / "evidence"
    with pytest.raises(EvidenceError, match="operator-owned with exact mode 0700"):
        _run(evidence_path, label="test")
    assert not evidence_path.exists()


def test_evidence_rejects_parent_not_owned_by_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "other-owner-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    actual_euid = os.geteuid()
    monkeypatch.setattr(ops_evidence.os, "geteuid", lambda: actual_euid + 1)
    evidence_path = parent / "evidence"
    with pytest.raises(EvidenceError, match="operator-owned with exact mode 0700"):
        _run(evidence_path, label="test")
    assert not evidence_path.exists()


def test_evidence_detects_swap_during_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    evidence_path = parent / "evidence"
    moved = parent / "created-original"
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "evidence" and flags & os.O_DIRECTORY and dir_fd is not None and not swapped:
            swapped = True
            evidence_path.rename(moved)
            evidence_path.mkdir(mode=0o700)
            evidence_path.chmod(0o700)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops_evidence.os, "open", swapping_open)
    with pytest.raises(EvidenceError, match="changed during creation"):
        _run(evidence_path, label="test")
    assert not (evidence_path / "001.json").exists()
    assert not (moved / "001.json").exists()


def test_evidence_detects_swap_during_terminal_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence = _run(tmp_path / "evidence", label="test")
    moved = tmp_path / "moved"
    real_link = os.link
    swapped = False

    def swapping_link(src, dst, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            evidence.path.rename(moved)
            evidence.path.mkdir(mode=0o700)
            evidence.path.chmod(0o700)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(ops_evidence.os, "link", swapping_link)
    with pytest.raises(EvidenceError, match="path changed"):
        evidence.finish({"probe_kind": "run-outcome", "outcome": "passed"})
    assert not (evidence.path / "002.json").exists()
    assert (moved / "002.json").exists()


def test_evidence_detects_parent_swap(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    evidence = _run(parent / "evidence", label="test")
    moved = tmp_path / "moved-parent"
    parent.rename(moved)
    parent.mkdir()
    with pytest.raises(EvidenceError, match="parent path changed"):
        evidence.record({"probe_kind": "test"})
    assert not (parent / "evidence" / "002.json").exists()
    assert not (moved / "evidence" / "002.json").exists()
    evidence.close()


def test_evidence_detects_directory_swap(tmp_path: Path) -> None:
    evidence = _run(tmp_path / "evidence", label="test")
    moved = tmp_path / "moved"
    evidence.path.rename(moved)
    evidence.path.mkdir()
    with pytest.raises(EvidenceError, match="path changed"):
        evidence.record({"probe_kind": "test"})
    assert not (evidence.path / "002.json").exists()
    assert not (moved / "002.json").exists()
    evidence.close()


def test_header_evidence_has_one_total_bound_and_control_safe_preview() -> None:
    class Headers:
        def items(self):
            values = [(f"X-Omitted-{index}-" + "n" * 300, "secret") for index in range(100)]
            values.extend(("Server", "") for _ in range(100))
            values.append(("Location", "https://user:secret@example.test:bad/a?token=x"))
            return values

    event = ops_evidence._health_event(
        url="https://library.example/health",
        user_agent="test",
        started_wall="2026-01-01T00:00:00+00:00",
        started_mono=0.0,
        status=403,
        headers=Headers(),
        body=b"safe\x00\x1bunsafe",
        body_complete=True,
        phase="test",
    )
    header_size = ops_evidence._header_evidence_size(
        event["response_headers"],
        event["omitted_response_header_names"],
        event["omitted_response_header_count"],
    )
    assert header_size <= ops_evidence.HEALTH_HEADER_CAPTURE_LIMIT
    assert event["response_headers_complete"] is False
    assert "\x00" not in event["body_preview"]
    assert "\x1b" not in event["body_preview"]
    assert "secret" not in json.dumps(event["response_headers"])


def test_header_evidence_exact_boundary() -> None:
    base_size = ops_evidence._header_evidence_size({"server": [""]}, [], 0)
    exact_value = "x" * (ops_evidence.HEALTH_HEADER_CAPTURE_LIMIT - base_size)
    exact_headers = Message()
    exact_headers.add_header("Server", exact_value)
    captured, omitted, omitted_count, complete = ops_evidence._health_headers(exact_headers)
    assert ops_evidence._header_evidence_size(captured, omitted, omitted_count) == (
        ops_evidence.HEALTH_HEADER_CAPTURE_LIMIT
    )
    assert complete is True

    oversized_headers = Message()
    oversized_headers.add_header("Server", exact_value + "x")
    captured, omitted, omitted_count, complete = ops_evidence._health_headers(oversized_headers)
    assert ops_evidence._header_evidence_size(captured, omitted, omitted_count) <= (
        ops_evidence.HEALTH_HEADER_CAPTURE_LIMIT
    )
    assert complete is False


def _git_without_inherited_repository(*args: str) -> None:
    env = {key: value for key, value in os.environ.items() if key not in GIT_REPOSITORY_ENV}
    subprocess.run(["git", *args], check=True, env=env)


def _committed_script_repo(tmp_path: Path, content: str = "print('clean')\n") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    script = repo / "scripts" / "render.py"
    script.parent.mkdir(parents=True)
    script.write_text(content, encoding="utf-8")
    _git_without_inherited_repository("init", "-q", str(repo))
    _git_without_inherited_repository("-C", str(repo), "add", ".")
    _git_without_inherited_repository(
        "-C",
        str(repo),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    return repo, script


def _head(repo: Path) -> str:
    env = {key: value for key, value in os.environ.items() if key not in GIT_REPOSITORY_ENV}
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def test_verifier_identity_ignores_an_inherited_git_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel, _ = _committed_script_repo(tmp_path / "sentinel", "print('sentinel')\n")
    sentinel_head = _head(sentinel)
    monkeypatch.setenv("GIT_DIR", str(sentinel / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(sentinel))

    repo, script = _committed_script_repo(tmp_path)
    identity = ops_evidence.verifier_identity(repo_root=repo, script_path=script)

    assert identity["verifier_source_sha"] == _head(repo)
    assert identity["verifier_source_sha"] != sentinel_head
    assert _head(sentinel) == sentinel_head


def test_verifier_identity_is_repo_anchored_and_rejects_dirty_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, script = _committed_script_repo(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    identity = ops_evidence.verifier_identity(repo_root=repo, script_path=script)
    assert len(identity["verifier_source_sha"]) == 40
    assert identity["verifier_script_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()

    script.write_text("print('dirty')\n", encoding="utf-8")
    with pytest.raises(EvidenceError, match="tracked changes"):
        ops_evidence.verifier_identity(repo_root=repo, script_path=script)
