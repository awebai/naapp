"""Compile owner-approved Microsoft Graph operations for one mailbox."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from aweb_naapp.egress import InternalDestinations

from aweb_secrets.destination import DestinationRecord, mint_destination_id
from aweb_secrets.grant import GrantRecord, mint_grant_id
from aweb_secrets.operation import OperationTemplate
from aweb_secrets.policy import OWNER_PREFIX, Policy
from aweb_secrets.principal import is_principal
from aweb_secrets.secret import OAUTH2_CERTIFICATE, SecretRecord

_GRAPH = "graph.microsoft.com"
_LOGIN = "login.microsoftonline.com"
_SCOPE = "https://graph.microsoft.com/.default"
_MESSAGE_HEADERS = ("id", "subject", "from", "receivedDateTime", "isRead", "hasAttachments")
_IMMUTABLE_IDS = 'IdType="ImmutableId"'
_MAX_REQUEST_BYTES = 32 * 1024
_MAX_RESULT_BYTES = 128 * 1024


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError("the mailbox and tenant must use their Microsoft object IDs") from None


def _token_path(secret: SecretRecord) -> str:
    params = secret.kind_params
    if (
        secret.kind != OAUTH2_CERTIFICATE
        or params.get("scope") != _SCOPE
        or params.get("signing_alg") != "PS256"
        or params.get("audience") != params.get("token_endpoint")
    ):
        raise ValueError("select a native Graph certificate credential")
    target = urlsplit(params["token_endpoint"])
    tenant = _uuid(target.path.split("/")[1])
    path = f"/{tenant}/oauth2/v2.0/token"
    if params["token_endpoint"] != f"https://{_LOGIN}{path}":
        raise ValueError("the credential must use its tenant's Microsoft token endpoint")
    return path


def _string(
    max_length: int, *, placement: str = "body", charset: str | None = None
) -> dict[str, Any]:
    return {
        "placement": placement,
        "charset": charset,
        "schema": {"type": "string", "minLength": 1, "maxLength": max_length},
    }


def _operation(
    secret_id: str,
    name: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    projection: list[str] | None = None,
    prefer: str = _IMMUTABLE_IDS,
    effect: str = "none",
) -> OperationTemplate:
    url: dict[str, Any] = {"host": _GRAPH, "port": 443, "path": path}
    if query:
        url["query"] = query
    fixed = {"Accept": "application/json", "Prefer": prefer}
    if body is not None:
        fixed["Content-Type"] = "application/json"
    return OperationTemplate.from_after(
        {
            "v": 1,
            "secret_id": secret_id,
            "operation_id": name,
            "method": "GET" if body is None else "POST",
            "url": url,
            "params": params or {},
            "headers": {"fixed": fixed, "allowed": []},
            "body": {"template": body} if body is not None else None,
            "response": {
                "projection": projection,
                "content_types": ["application/json"],
                "max_bytes": _MAX_RESULT_BYTES,
            },
            "side_effect": effect,
            "token_issuing": False,
            "result_handling": "deny",
            "store_as_secret": None,
            "idempotency": None,
            "reconcile": None,
            "timeout_s": 20,
        }
    )


def _destination(
    secret_id: str, *, host: str, path: str, method: str, headers: Sequence[str]
) -> dict[str, Any]:
    return dict(
        DestinationRecord.from_after(
            {
                "v": 1,
                "destination_id": mint_destination_id(),
                "secret_id": secret_id,
                "scheme": "https",
                "host": host,
                "port": 443,
                "path": path,
                "methods": [method],
                "query_rules": {},
                "header_allow": sorted(name.lower() for name in headers),
                "network": "public",
                "cidr": None,
                "response_max_bytes": _MAX_RESULT_BYTES,
            },
            internal_destinations=InternalDestinations.DISABLED,
        ).after
    )


def _item(kind: str, ref: str, after: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "op": "create", "ref": ref, "after": after}


def mailbox_changes(
    secret: SecretRecord,
    *,
    mailbox: str,
    vault_agents: Sequence[str],
    approver: str,
    include_archive: bool = False,
) -> list[dict[str, Any]]:
    """Build value-free draft items for the vault team's provisioned member DIDs."""
    if (
        not vault_agents
        or any(not is_principal(agent) for agent in vault_agents)
        or len(set(vault_agents)) != len(vault_agents)
        or not is_principal(approver)
        or approver in vault_agents
    ):
        raise ValueError("provide distinct vault agent DIDs and the vault owner's DID")
    secret_id = secret.secret_id
    token = _destination(
        secret_id,
        host=_LOGIN,
        path=_token_path(secret),
        method="POST",
        headers=("accept", "content-type"),
    )
    base = f"/v1.0/users/{_uuid(mailbox)}"
    messages = base + "/messages"
    message_params = {"message_id": _string(512, placement="path", charset="base64url-padded")}
    text = {"$param": "text"}
    operations = [
        _operation(
            secret_id,
            "list-messages",
            messages,
            query={"$top": "25", "$select": ",".join(_MESSAGE_HEADERS)},
            projection=["/value"],
        ),
        _operation(
            secret_id,
            "get-message",
            messages + "/{message_id}",
            params=message_params,
            query={"$select": ",".join((*_MESSAGE_HEADERS, "body"))},
            projection=["/" + name for name in (*_MESSAGE_HEADERS, "body")],
            prefer=_IMMUTABLE_IDS + ', outlook.body-content-type="text"',
        ),
        _operation(
            secret_id,
            "send-mail",
            base + "/sendMail",
            params={"recipient": _string(254), "subject": _string(256), "text": _string(4096)},
            body={
                "message": {
                    "subject": {"$param": "subject"},
                    "body": {"contentType": "Text", "content": text},
                    "toRecipients": [{"emailAddress": {"address": {"$param": "recipient"}}}],
                }
            },
            effect="irreversible",
        ),
        _operation(
            secret_id,
            "reply",
            messages + "/{message_id}/reply",
            params={**message_params, "text": _string(4096)},
            body={"comment": text},
            effect="irreversible",
        ),
    ]
    if include_archive:
        operations.append(
            _operation(
                secret_id,
                "archive-message",
                messages + "/{message_id}/move",
                params=message_params,
                body={"destinationId": "archive"},
                projection=["/id", "/parentFolderId"],
                effect="reversible",
            )
        )

    items = [_item("destination", token["destination_id"], token)]
    for op in operations:
        after = dict(op.after)
        dest = _destination(
            secret_id,
            host=_GRAPH,
            path=after["url"]["path"],
            method=after["method"],
            headers=list(after["headers"]["fixed"]),
        )
        items.extend(
            [
                _item("destination", dest["destination_id"], dest),
                _item("operation", op.operation_id, after),
            ]
        )
    for writes in (False, True):
        selected = [op for op in operations if (op.after["side_effect"] != "none") == writes]
        pins = [{"operation_id": op.operation_id, "digest": op.digest} for op in selected]
        policy = Policy.from_body(
            {
                "approval": "per_use" if writes else "none",
                "approvers": [OWNER_PREFIX + approver],
                "approval_ttl_s": 900,
                "rate": [{"per": "principal_secret", "capacity": 30, "window_s": 3600}],
                "windows": None,
                "operations": pins,
                "open_request": False,
                "request": {"max_bytes": _MAX_REQUEST_BYTES},
                "result": {"max_bytes": _MAX_RESULT_BYTES},
                "requires_attestation": None,
                "distinct_approver": "required",
            }
        )
        items.append(_item("policy", policy.digest, {"v": 1, **policy.body}))
        for agent in vault_agents:
            grant = GrantRecord.from_after(
                {
                    "v": 1,
                    "grant_id": mint_grant_id(),
                    "secret_id": secret_id,
                    "subject": {"principal": agent},
                    "capability": "use",
                    "operations": pins,
                    "open_request": False,
                    "jobs": [],
                    "policy_digest": policy.digest,
                }
            )
            items.append(_item("grant", grant.grant_id, grant.after))
    return items
