"""The committed manifest template against the spec's scope table (section 4).

Admission grants a tool only the scopes it declares, so the scope of each
tool is the access rule itself.
"""

from __future__ import annotations

import json

from aweb_naapp.manifest import load_manifest
from aweb_naapp.runtime.authorization import DeclaredTools

from aweb_tasks import MANIFEST_PATH

from .deployment import served_manifest, write_key_files

SCOPES = {
    "tasks.read": {
        "show", "list", "ready", "active", "blocked", "locks", "history", "comment-list",
    },
    "tasks.write": {
        "create", "update", "close", "reopen", "comment-add", "dep-add", "dep-remove",
        "claim", "release", "claim-renew", "lock", "lock-renew", "unlock",
    },
    "tasks.override": {"takeover", "break", "close-claimed"},
}


def template() -> dict:
    return json.loads(MANIFEST_PATH.read_bytes())


def test_each_tool_requires_exactly_its_scope() -> None:
    declared = {tool["name"]: tool["scopes"] for tool in template()["tools"]}
    expected = {name: [scope] for scope, names in SCOPES.items() for name in names}
    assert declared == expected


def test_reads_are_gets_and_everything_else_mutates() -> None:
    for tool in template()["tools"]:
        assert (tool["method"] == "GET") == (tool["scopes"] == ["tasks.read"]), tool["name"]
        assert tool["mutation"] is (tool["method"] != "GET"), tool["name"]
        assert tool["invocation_contexts"] == ["agent"], tool["name"]


def test_events_are_status_claim_and_lock_changes() -> None:
    assert [event["type"] for event in template()["events"]] == ["task.status", "task.claim", "lock"]
    assert {event["default_delivery_intent"] for event in template()["events"]} == {"wake"}


def test_the_filled_manifest_loads_with_one_tool_per_request(tmp_path) -> None:
    manifest = load_manifest(served_manifest(write_key_files(tmp_path), origin="https://tasks.example.test"))
    DeclaredTools.from_manifest(manifest)
    assert len(manifest.tools_by_name()) == 24
