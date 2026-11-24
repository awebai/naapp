"""Which local source tree an imported package came from."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def shadowing_local_source(package_file: Path | str, candidates: Iterable[Path]) -> Path | None:
    """The local source tree an imported package came from, or None if it did not.

    Kept as a plain function of its inputs so it can be exercised in both directions. The
    imports it is normally asked about resolve from this checkout, so the other case
    cannot be produced by importing anything - it has to be constructed, and a decision
    that cannot be constructed is one nobody has watched fail.
    """
    resolved = Path(package_file).resolve()
    for candidate in candidates:
        if candidate.exists() and candidate in resolved.parents:
            return candidate
    return None
