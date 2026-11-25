"""Create a local custody key on a customer-controlled services host."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path


def initialize(path: Path) -> None:
    """Publish a complete new key without replacing anything at the target."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory).st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("custody key directory is writable by group or others")
        temporary = f".custody-key-{secrets.token_hex(16)}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(secrets.token_hex(32).encode("ascii") + b"\n")
                output.flush()
                os.fchmod(output.fileno(), 0o400)
                os.fsync(output.fileno())
            # Hard-link publication is exclusive. Even a dangling symlink at
            # the final name causes a refusal, and readers see the whole key.
            os.link(
                temporary,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        finally:
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, type=Path, help="new custody key file")
    args = parser.parse_args()
    try:
        initialize(args.path)
    except FileExistsError:
        print("key-init: custody key already exists", file=sys.stderr)
        return 1
    except OSError:
        print("key-init: custody_key_create_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
