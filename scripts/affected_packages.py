#!/usr/bin/env python3
"""Which packages a change selects for testing.

Usage: git diff --name-only BASE...HEAD | scripts/affected_packages.py
       scripts/affected_packages.py --all

Prints the selected packages as a JSON list. A changed path belongs to the
innermost package that contains it. A package is selected when a changed path
belongs to it or to any package it depends on through its uv path sources,
directly or not; the runtime (naapp-lib) therefore selects every package. A
changed path that belongs to no package selects every package, so no change can
leave an affected package untested.
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every project CI tests, by its directory.
PACKAGES = (
    'naapp-lib',
    'naapp/folio',
    'naapp/library',
    'naapp/logs',
    'naapp/secrets',
    'naapp/secrets/runner',
    'naapp/secrets/values',
)


def _owner(path: str) -> str | None:
    """The innermost package containing ``path``."""
    containing = [package for package in PACKAGES if path == package or path.startswith(package + '/')]
    return max(containing, key=len) if containing else None


def _dependencies(package: str) -> set[str]:
    """The packages ``package`` reaches through its own uv path sources."""
    project = tomllib.loads((ROOT / package / 'pyproject.toml').read_text())
    sources = project.get('tool', {}).get('uv', {}).get('sources', {})
    reached = set()
    for source in sources.values():
        if 'path' not in source:
            continue
        target = (ROOT / package / source['path']).resolve().relative_to(ROOT).as_posix()
        owner = _owner(target)
        if owner is not None and owner != package:
            reached.add(owner)
    return reached


def _closure() -> dict[str, set[str]]:
    """Each package with every package it depends on, directly or not."""
    direct = {package: _dependencies(package) for package in PACKAGES}
    closure = {}
    for package in PACKAGES:
        seen, pending = set(), [package]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(direct[current])
        closure[package] = seen
    return closure


def affected(paths: list[str]) -> set[str]:
    owners = {_owner(path) for path in paths}
    if None in owners:
        return set(PACKAGES)
    closure = _closure()
    return {package for package in PACKAGES if closure[package] & owners}


def main() -> None:
    if sys.argv[1:] == ['--all']:
        print(json.dumps(sorted(PACKAGES)))
        return
    if sys.argv[1:]:
        raise SystemExit(f'usage: {sys.argv[0]} [--all] < changed paths')
    paths = [line.strip() for line in sys.stdin if line.strip()]
    print(json.dumps(sorted(affected(paths))))


if __name__ == '__main__':
    main()
