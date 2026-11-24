"""Runner HTTP contract vectors for framing and dispatch state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from aweb_naapp.dispatch import TRANSITIONS, State

SAFE_INTEGER = 2**53 - 1


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_float(text: str) -> Any:
    raise ValueError("float")


def strict_json(raw: bytes | str, code: str) -> Any:
    """Parse JSON, refusing duplicate keys, floats and non-finite numbers."""
    try:
        return json.loads(
            raw, object_pairs_hook=_no_duplicates, parse_float=_no_float, parse_constant=_no_float
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused(code) from exc


def _check_values(value: Any, code: str) -> None:
    """Strings are Unicode scalar values; integers are within the safe range."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise Refused(code) from exc
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int):
        if abs(value) > SAFE_INTEGER:
            raise Refused(code)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_values(key, code)
            _check_values(item, code)
    elif isinstance(value, list):
        for item in value:
            _check_values(item, code)


# This is a contract reference reader, not a mounted route or release authority.
HTTP_VECTOR = json.loads(
    (Path(__file__).parents[3] / "docs" / "vectors" / "secrets-runner-http-v1.json").read_text()
)
RESULT_FIELDS = {"exit_code", "stdout", "stderr", "truncated", "result_withheld", "redaction_count"}


def _http_body_result(case):
    raw = case["body_json"].encode("utf-8")
    limit = case["output_max_bytes"]
    is_result = case["endpoint"] == "result"
    if len(raw) > (6 * limit + 1024 if is_result else 4096):
        return {"status": 413, "code": "runner_request_too_large"}
    code = "runner_result_invalid" if is_result else "runner_request_invalid"
    try:
        body = strict_json(raw, code)
        _check_values(body, code)
        if not isinstance(body, dict) or set(body) != (RESULT_FIELDS if is_result else set()):
            raise Refused(code)
        if is_result:
            if type(body["exit_code"]) is not int or type(body["redaction_count"]) is not int:
                raise Refused(code)
            if body["redaction_count"] < 0:
                raise Refused(code)
            if any(type(body[key]) is not bool for key in ("truncated", "result_withheld")):
                raise Refused(code)
            if any(not isinstance(body[key], str) for key in ("stdout", "stderr")):
                raise Refused(code)
            if sum(len(body[key].encode("utf-8")) for key in ("stdout", "stderr")) > limit:
                raise Refused(code)
            if body["result_withheld"] and (body["stdout"] or body["stderr"]):
                raise Refused(code)
    except Refused:
        return {"status": 400, "code": code}
    return {"status": 204}


@pytest.mark.parametrize("case", HTTP_VECTOR["bodies"], ids=lambda case: case["name"])
def test_http_body_vectors(case):
    assert hashlib.sha256(case["body_json"].encode("utf-8")).hexdigest() == case["body_sha256"]
    assert _http_body_result(case) == case["expected"]


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
