"""Verifying an exported team log, ``aweb.logs-export/1`` (docs/logs-chain.md):
real signed envelopes, receipts and checkpoints, checked with no server."""

from __future__ import annotations

from typing import Any

import pytest

from aweb_logs.export import (
    BundleMalformed,
    verify_bundle,
)

from .chain_cases import LOGS_DID
from .export_cases import bundle, two_epochs
from .export_vector import stream

KEYS = frozenset({LOGS_DID})


def verified(lines: list[bytes], **witnesses: Any) -> dict[str, Any]:
    return verify_bundle(
        stream(lines), receipt_keys=KEYS, checkpoint_keys=KEYS, **witnesses
    ).to_json()


def test_a_bundle_whose_header_is_not_canonical_is_refused_at_its_first_line() -> None:
    lines = bundle(two_epochs())
    with pytest.raises(BundleMalformed) as refused:
        verified([b'{"v": 1}\n', *lines[1:]])
    assert refused.value.line == 1
