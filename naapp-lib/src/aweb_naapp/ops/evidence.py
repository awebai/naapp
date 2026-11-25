"""Operator evidence for deploy health checks: one private file per event.

A run is a new directory under an operator-owned parent of mode 0700, outside
the repository, holding ``001.json``, ``002.json``, ... (mode 0600, written once,
no symlink anywhere on the path). Each event names the run, its metadata (the
executing verifier's identity, the config digest, the service and the expected
commit) and either one health probe as sent and answered (bounded, with a
header allowlist) or the run's outcome. Library's layout, under its own schema
name ``library.health-evidence.v1``: the schema is ``<app_id>.health-evidence.v1``.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from aweb_naapp.ops.git import run_git as _run_git

COMMIT_RE_PATTERN = r"^[0-9a-f]{40}$"
HEALTH_BODY_PREVIEW_LIMIT = 16_384
HEALTH_HEADER_CAPTURE_LIMIT = 16_384
HEALTH_DIAGNOSTIC_HEADERS = {
    "cf-cache-status",
    "cf-mitigated",
    "cf-ray",
    "content-length",
    "content-type",
    "location",
    "retry-after",
    "server",
}


class EvidenceError(RuntimeError):
    """The evidence directory or a record could not be kept safely."""


def _open_directory_nofollow(path: Path) -> list[tuple[int, str, tuple[int, int]]]:
    """Retain a no-follow descriptor chain for an existing absolute directory."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    root_stat = os.fstat(descriptor)
    chain = [(descriptor, "", (root_stat.st_dev, root_stat.st_ino))]
    try:
        for component in path.parts[1:]:
            descriptor = os.open(component, flags, dir_fd=chain[-1][0])
            component_stat = os.fstat(descriptor)
            chain.append((descriptor, component, (component_stat.st_dev, component_stat.st_ino)))
        return chain
    except OSError:
        for opened, _, _ in reversed(chain):
            os.close(opened)
        raise


class HealthEvidenceRun:
    """Descriptor-anchored, mode-private evidence for unauthenticated health probes."""

    def __init__(
        self,
        path: Path,
        *,
        label: str,
        schema: str,
        repository_root: Path,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not path.is_absolute():
            raise EvidenceError("health evidence directory must be absolute")
        normalized = Path(os.path.normpath(str(path)))
        if normalized != path:
            raise EvidenceError("health evidence directory must not contain traversal components")
        if path == repository_root or repository_root in path.parents:
            raise EvidenceError("health evidence directory must be outside the repository")
        self.path = path
        self.label = label
        self.schema = schema
        self.metadata = {"run_id": str(uuid.uuid4()), **(metadata or {})}
        self.sequence = 0
        self._closed = False
        try:
            self._parent_chain = _open_directory_nofollow(path.parent)
            self._parent_fd = self._parent_chain[-1][0]
        except OSError as exc:
            raise EvidenceError(
                "health evidence parent must be an existing path without symlink components"
            ) from exc
        self._leaf = path.name
        try:
            self._validate_parent_chain()
            parent_stat = os.fstat(self._parent_fd)
            if stat.S_IMODE(parent_stat.st_mode) != 0o700 or parent_stat.st_uid != os.geteuid():
                raise EvidenceError(
                    "health evidence parent must be operator-owned with exact mode 0700"
                )
            previous_umask = os.umask(0)
            try:
                os.mkdir(self._leaf, 0o700, dir_fd=self._parent_fd)
            finally:
                os.umask(previous_umask)
            created_stat = os.stat(self._leaf, dir_fd=self._parent_fd, follow_symlinks=False)
            self._directory_fd = os.open(
                self._leaf,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._parent_fd,
            )
            os.fchmod(self._directory_fd, 0o700)
            directory_stat = os.fstat(self._directory_fd)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or stat.S_IMODE(directory_stat.st_mode) != 0o700
                or (created_stat.st_dev, created_stat.st_ino)
                != (directory_stat.st_dev, directory_stat.st_ino)
            ):
                raise EvidenceError("health evidence directory changed during creation")
            self._directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
            self.record({"probe_kind": "run-manifest", "outcome": "started"})
        except FileExistsError as exc:
            for descriptor, _, _ in reversed(self._parent_chain):
                os.close(descriptor)
            self._closed = True
            raise EvidenceError("health evidence directory must not already exist") from exc
        except Exception:
            self.close()
            raise

    def _validate_parent_chain(self) -> None:
        for index in range(1, len(self._parent_chain)):
            parent_fd = self._parent_chain[index - 1][0]
            descriptor, component, identity = self._parent_chain[index]
            descriptor_stat = os.fstat(descriptor)
            try:
                component_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise EvidenceError("health evidence parent path changed") from exc
            if (
                not stat.S_ISDIR(component_stat.st_mode)
                or (descriptor_stat.st_dev, descriptor_stat.st_ino) != identity
                or (component_stat.st_dev, component_stat.st_ino) != identity
            ):
                raise EvidenceError("health evidence parent path changed")

    def _validate_anchor(self) -> None:
        if self._closed:
            raise EvidenceError("health evidence run is closed")
        self._validate_parent_chain()
        directory_stat = os.fstat(self._directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
            or (directory_stat.st_dev, directory_stat.st_ino) != self._directory_identity
        ):
            raise EvidenceError("health evidence directory identity or mode changed")
        try:
            path_stat = os.stat(self._leaf, dir_fd=self._parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise EvidenceError("health evidence directory path changed") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != self._directory_identity
        ):
            raise EvidenceError("health evidence directory path changed")

    def record(self, event: dict[str, Any]) -> None:
        self._validate_anchor()
        self.sequence += 1
        payload = {
            "schema": self.schema,
            "run_label": self.label,
            "run_metadata": self.metadata,
            "sequence": self.sequence,
            **event,
        }
        encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode()
        destination = f"{self.sequence:03d}.json"
        temporary = f".{destination}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory_fd,
            )
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(
                    temporary,
                    destination,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise EvidenceError("health evidence artifact must not already exist") from exc
            os.fsync(self._directory_fd)
            self._validate_anchor()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._directory_fd)
            except FileNotFoundError:
                pass

    def finish(self, event: dict[str, Any]) -> None:
        try:
            self.record(event)
        finally:
            self.close()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        directory_descriptor = getattr(self, "_directory_fd", None)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        for descriptor, _, _ in reversed(getattr(self, "_parent_chain", [])):
            os.close(descriptor)

    def __del__(self) -> None:
        self.close()


def _control_safe(value: str) -> str:
    return "".join(char if ord(char) >= 32 and ord(char) != 127 else "�" for char in value)


def _header_evidence_size(
    captured: dict[str, list[str]], omitted_names: list[str], omitted_count: int
) -> int:
    return len(
        json.dumps(
            {
                "response_headers": captured,
                "omitted_response_header_names": omitted_names,
                "omitted_response_header_count": omitted_count,
                "response_headers_complete": False,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _health_headers(
    headers: Any,
) -> tuple[dict[str, list[str]], list[str], int, bool]:
    captured: dict[str, list[str]] = {}
    omitted_names: list[str] = []
    omitted_seen: set[str] = set()
    omitted_count = 0
    complete = True
    if headers is None:
        return captured, omitted_names, omitted_count, complete
    for raw_name, raw_value in headers.items():
        name = _control_safe(str(raw_name).lower())
        if name not in HEALTH_DIAGNOSTIC_HEADERS:
            omitted_count += 1
            if name in omitted_seen:
                continue
            candidate_names = sorted([*omitted_names, name])
            if (
                _header_evidence_size(captured, candidate_names, omitted_count)
                <= HEALTH_HEADER_CAPTURE_LIMIT
            ):
                omitted_names = candidate_names
                omitted_seen.add(name)
            else:
                complete = False
            continue
        value = _control_safe(str(raw_value))
        if name == "location":
            try:
                parts = urlsplit(value)
                host = parts.hostname or ""
                if parts.port is not None:
                    host = f"{host}:{parts.port}"
                value = urlunsplit((parts.scheme, host, parts.path, "", ""))
            except ValueError:
                value = "[invalid-location]"
        candidate = {key: list(values) for key, values in captured.items()}
        candidate.setdefault(name, []).append(value)
        if (
            _header_evidence_size(candidate, omitted_names, omitted_count)
            <= HEALTH_HEADER_CAPTURE_LIMIT
        ):
            captured = candidate
        else:
            omitted_count += 1
            complete = False
            if name not in omitted_seen:
                candidate_names = sorted([*omitted_names, name])
                if (
                    _header_evidence_size(captured, candidate_names, omitted_count)
                    <= HEALTH_HEADER_CAPTURE_LIMIT
                ):
                    omitted_names = candidate_names
                    omitted_seen.add(name)
    while (
        _header_evidence_size(captured, omitted_names, omitted_count) > HEALTH_HEADER_CAPTURE_LIMIT
        and omitted_names
    ):
        omitted_names.pop()
        complete = False
    return dict(sorted(captured.items())), omitted_names, omitted_count, complete


def _body_preview(body: bytes) -> tuple[str, bool]:
    tokens: list[str] = []
    remaining = HEALTH_BODY_PREVIEW_LIMIT - 2  # JSON string quotes
    complete = True
    for byte in body:
        token = chr(byte) if 32 <= byte <= 126 else f"\\x{byte:02x}"
        serialized_length = len(json.dumps(token, ensure_ascii=True).encode()) - 2
        if serialized_length > remaining:
            complete = False
            break
        tokens.append(token)
        remaining -= serialized_length
    return "".join(tokens), complete


def _health_event(
    *,
    url: str,
    user_agent: str,
    started_wall: str,
    started_mono: float,
    status: int | None,
    headers: Any,
    body: bytes,
    body_complete: bool,
    phase: str,
    error_class: str | None = None,
) -> dict[str, Any]:
    allowed_headers, omitted_headers, omitted_count, headers_complete = _health_headers(headers)
    body_preview, preview_complete = _body_preview(body)
    return {
        "probe_kind": "unauthenticated-health",
        "phase": phase,
        "method": "GET",
        "url": url,
        "request_headers": {"accept": "application/json", "user-agent": user_agent},
        "started_at": started_wall,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - started_mono, 6),
        "status": status,
        "response_headers": allowed_headers,
        "response_headers_complete": headers_complete,
        "omitted_response_header_names": omitted_headers,
        "omitted_response_header_count": omitted_count,
        "body_preview_encoding": "control-safe-ascii-with-hex-byte-escapes",
        "body_preview": body_preview,
        "body_preview_complete": preview_complete,
        "captured_body_bytes": len(body),
        "captured_body_sha256": hashlib.sha256(body).hexdigest(),
        "body_complete": body_complete,
        "error_class": error_class,
    }


def verifier_identity(*, repo_root: Path, script_path: Path) -> dict[str, str]:
    """The executing verifier: its repository's clean HEAD and the committed bytes
    of the script that runs, so a record says exactly which code checked."""
    try:
        root = repo_root.resolve(strict=True)
        script = script_path.resolve(strict=True)
        relative_script = script.relative_to(root)
        top_level = Path(
            _run_git(
                ["-C", str(root), "rev-parse", "--show-toplevel"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        ).resolve(strict=True)
        if top_level != root:
            raise EvidenceError("verifier repository root does not match the executing script")
        source_sha = _run_git(
            ["-C", str(root), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        if not re.fullmatch(COMMIT_RE_PATTERN, source_sha):
            raise EvidenceError("verifier source commit is invalid")
        tracked_changes = _run_git(
            ["-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        if tracked_changes:
            raise EvidenceError("verifier repository has tracked changes")
        committed_script = _run_git(
            ["-C", str(root), "show", f"HEAD:{relative_script.as_posix()}"],
            check=True,
            capture_output=True,
        ).stdout
        script_bytes = script.read_bytes()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise EvidenceError("failed to establish executing verifier identity") from exc
    if script_bytes != committed_script:
        raise EvidenceError("executing verifier does not match its source commit")
    return {
        "verifier_source_sha": source_sha,
        "verifier_script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        "verifier_script_path": relative_script.as_posix(),
    }
