"""A requester's original evidence deadline is enforced, never renewed."""

import time

import pytest
from fastapi import HTTPException

from aweb_naapp.console.handoff_requester import RequesterEvidence


def test_evidence_past_its_original_deadline_refuses(monkeypatch, caplog):
    monkeypatch.setattr(time, "monotonic_ns", lambda: 61_000_000_000)
    expired = RequesterEvidence("did:aw:holder", 60_000_000_000, 0)
    with pytest.raises(HTTPException) as refused:
        expired.require_fresh()
    assert (refused.value.status_code, refused.value.detail) == (
        503,
        "principal_status_unavailable",
    )
    assert [
        r.getMessage() for r in caplog.records if r.name == "aweb_naapp.runtime.principal_status"
    ] == ["principal_status_unavailable reason=requester_identity_deadline elapsed_ms=61000"]


def test_evidence_within_its_window_passes():
    started = time.monotonic_ns()
    RequesterEvidence("did:aw:holder", started + 60_000_000_000, started).require_fresh()
