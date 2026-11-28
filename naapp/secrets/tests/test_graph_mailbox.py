"""A Graph mailbox draft uses the existing destination, operation and grant contracts."""

import base64
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlsplit

import pytest
from aweb_naapp.egress import EgressRefused

from aweb_secrets.change import validate_change
from aweb_secrets.graph_mailbox import mailbox_changes
from aweb_secrets.operation import OperationTemplate
from aweb_secrets.operation_request import render_operation
from aweb_secrets.secret import SecretRecord
from tests.test_admission import ALICE, BOB, SECRET
from tests.test_change_sets import CAROL, by_member, change_set

MAILBOX = "865c76a6-1318-4ec1-b877-6e42e8dcc302"
TOKEN = "https://login.microsoftonline.com/8647e1b0-6b4e-4a9a-b81d-808fa74a6b97/oauth2/v2.0/token"
SENDER = "vault-owner@example.test"


def graph_secret(**overrides):
    return SecretRecord.from_after(
        {
            **SECRET,
            "kind": "oauth2-certificate-credentials",
            "kind_params": {
                "token_endpoint": TOKEN,
                "audience": TOKEN,
                "scope": "https://graph.microsoft.com/.default",
                "signing_alg": "PS256",
                **overrides,
            },
        }
    )


def test_mailbox_is_fixed_and_only_the_selected_vault_agents_get_a_grant():
    items = mailbox_changes(
        graph_secret(), mailbox=MAILBOX, mailbox_address=SENDER, vault_agents=[BOB.did_aw], approver=ALICE.did_aw
    )
    validate_change(change_set(drafter=by_member(CAROL), changes=items))
    records = [item["after"] for item in items if item["kind"] == "operation"]
    operations = {body["operation_id"]: OperationTemplate.from_after(body) for body in records}
    assert set(operations) == {"list-messages", "get-message", "send-mail", "reply"}
    destinations = [item["after"] for item in items if item["kind"] == "destination"]
    assert len(destinations) == len(operations)
    assert {d["host"] for d in destinations} == {"graph.microsoft.com", "login.microsoftonline.com"}
    assert all("**" not in d["path"] for d in destinations)

    query = urlsplit(render_operation(operations["list-messages"], {}, headers=[]).url)
    assert query.path == f"/v1.0/users/{MAILBOX}/messages"
    assert parse_qs(query.query)["$top"] == ["25"]
    assert "body" not in parse_qs(query.query)["$select"][0].split(",")
    assert operations["list-messages"].projection == ["/value"]
    for injected in ({"mailbox": "other"}, {"$select": "body"}, {"$top": 1000}):
        with pytest.raises(EgressRefused) as refused:
            render_operation(operations["list-messages"], injected, headers=[])
        assert refused.value.code == "params_invalid"

    get = operations["get-message"]
    for message_id in ("..", "a/b", "a%2fb", "x?expand=attachments"):
        assert not get.params_valid({"message_id": message_id})
    assert urlsplit(render_operation(get, {"message_id": "AAMkAA="}, headers=[]).url).path.endswith(
        "/messages/AAMkAA%3D"
    )

    policies = {item["ref"]: item["after"] for item in items if item["kind"] == "policy"}
    grants = [item["after"] for item in items if item["kind"] == "grant"]
    assert len(grants) == 2
    for grant in grants:
        assert grant["subject"] == {"principal": BOB.did_aw}
        assert not grant["open_request"] and grant["jobs"] == []
        approved = policies[grant["policy_digest"]]
        names = {pin["operation_id"] for pin in grant["operations"]}
        assert all(
            pin["digest"] == operations[pin["operation_id"]].digest for pin in grant["operations"]
        )
        if names == {"send-mail", "reply"}:
            assert approved["approval"] == "per_use"
            assert approved["approvers"] == ["owner:" + ALICE.did_aw]
            assert all(operations[name].after["idempotency"] is None for name in names)
        else:
            assert names == {"list-messages", "get-message"}
            assert approved["approval"] == "none"


def test_send_has_one_text_recipient_and_optional_archive_has_a_fixed_folder():
    import json

    items = mailbox_changes(
        graph_secret(),
        mailbox=MAILBOX, mailbox_address=SENDER,
        vault_agents=[BOB.did_aw],
        approver=ALICE.did_aw,
        include_archive=True,
    )
    operations = {
        item["ref"]: OperationTemplate.from_after(item["after"])
        for item in items
        if item["kind"] == "operation"
    }
    send = render_operation(
        operations["send-mail"],
        {"recipient": "reader@example.test", "subject": "Receipt", "text": "Thanks"},
        headers=[],
    )
    assert json.loads(send.body) == {
        "message": {
            "subject": "Receipt",
            "body": {"contentType": "Text", "content": "Thanks"},
            "toRecipients": [{"emailAddress": {"address": "reader@example.test"}}],
        }
    }
    archive = operations["archive-message"]
    move = render_operation(archive, {"message_id": "AAMkAA="}, headers=[])
    assert move.url.endswith("/messages/AAMkAA%3D/move")
    assert json.loads(move.body) == {"destinationId": "archive"}
    assert not archive.params_valid({"message_id": "AAMkAA=", "destinationId": "deleteditems"})


def test_graph_setup_requires_the_tenant_certificate_configuration():
    with pytest.raises(ValueError):
        mailbox_changes(
            graph_secret(scope="api://different-service/.default"),
            mailbox=MAILBOX, mailbox_address=SENDER,
            vault_agents=[BOB.did_aw],
            approver=ALICE.did_aw,
        )


def test_mailbox_reply_sends_one_explicit_recipient_and_threads_the_message():
    items = mailbox_changes(
        graph_secret(), mailbox=MAILBOX, mailbox_address=SENDER, vault_agents=[BOB.did_aw], approver=ALICE.did_aw,
    )
    [body] = [item["after"] for item in items if item["kind"] == "operation" and item["ref"] == "reply"]
    operation = OperationTemplate.from_after(body)
    assert operation.after["url"]["path"] == f"/v1.0/users/{MAILBOX}/sendMail"
    parameters = {
        "recipient": "reply-to@example.test", "in_reply_to": "<original@example.test>",
        "subject": "Re: Résumé", "text": "Thanks\nBcc: this is part of the body",
    }
    request = render_operation(operation, parameters, headers=[])
    assert ("Content-Type", "text/plain") in request.headers
    message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(request.body))
    assert message["From"].addresses[0].addr_spec == SENDER
    assert [address.addr_spec for address in message["To"].addresses] == [parameters["recipient"]]
    assert message["Cc"] is None and message["Bcc"] is None
    assert message["In-Reply-To"] == parameters["in_reply_to"]
    assert message["Subject"] == parameters["subject"]
    assert message.get_content().splitlines() == parameters["text"].splitlines()
    for name, value in (
        ("recipient", "alice@example.test,bob@example.test"),
        ("in_reply_to", "<a@example.test>\r\nBcc: b@example.test"),
        ("subject", "Hi\r\nBcc: b@example.test"),
    ):
        with pytest.raises(EgressRefused) as refused:
            render_operation(operation, {**parameters, name: value}, headers=[])
        assert refused.value.code == "params_invalid"
