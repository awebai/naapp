"""Git for the ops tools: always the repository a command names, never one an
inherited Git context points at."""

from __future__ import annotations

import os
import subprocess
from typing import Any

# Git variables that would point git at another repository, another object store
# or another namespace of refs than the one named.
GIT_REPOSITORY_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)


def run_git(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run git against the repository named in args, never an inherited Git context."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_REPOSITORY_ENV}
    return subprocess.run(["git", *args], env=env, **kwargs)
