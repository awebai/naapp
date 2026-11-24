"""Runner HTTP contract vectors at production framing and dispatch boundaries."""

import hashlib
import json
from pathlib import Path

import pytest
from aweb_naapp.dispatch import TRANSITIONS, State

from tests.test_run_dispatch_service import setup
from tests.test_run_runner_routes import headers, runner_client
from tests.test_use_approvals import uses as uses
from tests.test_use_approvals import world as world

HTTP_VECTOR = json.loads(
    (Path(__file__).parents[3] / "docs" / "vectors" / "secrets-runner-http-v1.json").read_text()
)


@pytest.mark.parametrize("case", HTTP_VECTOR["bodies"], ids=lambda case: case["name"])
async def test_http_body_vectors(uses, case):
    raw = case["body_json"].encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    assert digest.hex() == case["body_sha256"]
    context = await setup(uses, job_output_max_bytes=case["output_max_bytes"])
    endpoint = case["endpoint"]
    if endpoint == "claim":
        target = "/runner/v1/claim"
    else:
        _, _, assignment = await context.assigned()
        ref = assignment["assignment"]["dispatch_id"]
        target = f"/runner/v1/dispatches/{ref}/{endpoint}"

    db = uses.world.db
    before = await db.fetch_value("SELECT count(*) FROM {{tables.scoped_nonces}}")
    signed = context.signed(target, raw)
    async with runner_client(context) as client:
        response = await client.post(target, content=raw, headers=headers(signed))
    expected = case["expected"]
    assert response.status_code == expected["status"]
    assert response.headers["cache-control"] == "no-store"
    accepted = expected["status"] == 204
    if accepted:
        assert response.content == b""
    else:
        assert response.json() == {"detail": {"code": expected["code"]}}
    assert (
        await db.fetch_value("SELECT count(*) FROM {{tables.scoped_nonces}}") == before + accepted
    )

    state = await db.fetch_value("SELECT state FROM {{tables.dispatches}}")
    if endpoint == "claim":
        assert state is None
        return

    assert state == ("completed" if endpoint == "result" and accepted else "dispatching")
    delivery = await db.fetch_one("SELECT started_at, report_sha256 FROM {{tables.run_deliveries}}")
    assert (delivery["started_at"] is not None) == (endpoint == "started" and accepted)
    assert delivery["report_sha256"] == (digest if endpoint == "result" and accepted else None)


@pytest.mark.parametrize("case", HTTP_VECTOR["report_statuses"])
def test_http_report_status_vectors(case):
    if not case["released"]:
        status, code, effect = 409, "runner_dispatch_state_conflict", "none"
    elif case["prior_result"] == "different":
        status, code, effect = 409, "runner_result_conflict", "none"
    elif case["prior_result"] == "identical":
        status, code, effect = 204, None, "none"
    else:
        status, code = 204, None
        effect = "completed" if case["state"] == "dispatching" else "late_evidence"
    assert (status, code, effect) == (case["expected_status"], case.get("code"), case["effect"])


@pytest.mark.parametrize("case", HTTP_VECTOR["claim_statuses"], ids=lambda case: case["name"])
def test_http_claim_status_vectors(case):
    if case["state"] == "outcome_unknown":
        actual = {"status": 409, "code": "runner_reconciliation_required"}
    elif case["state"] == "dispatching":
        actual = (
            {"status": 409, "code": "runner_dispatch_state_conflict"}
            if case["assignment_expired"]
            else {"status": 200, "assignment": "stored_bytes"}
        )
    else:
        actual = {"status": 204}
    assert actual == case["expected"]


@pytest.mark.parametrize(
    "case", HTTP_VECTOR["preparation_transitions"], ids=lambda case: case["name"]
)
def test_http_preparation_vectors_match_the_real_shared_dispatch_graph(case):
    assert (State(case["to"]) in TRANSITIONS[State(case["from"])]) is case["allowed"]
