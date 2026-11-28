"""Owner HTTP access drafts; the existing change writer owns all effects."""

from html import escape
from pathlib import Path
from typing import Any

from aweb_naapp.console import ConsoleRoute
from aweb_naapp.egress import EgressRefused, InternalDestinations, canonical_url
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from aweb_secrets import records
from aweb_secrets.console import _body, _require_json, _text
from aweb_secrets.console_changes import ConsoleChanges, _errors
from aweb_secrets.destination import DestinationInvalid, DestinationRecord, mint_destination_id
from aweb_secrets.grant import GrantRecord, is_secrets_role, mint_grant_id
from aweb_secrets.graph_mailbox import mailbox_changes
from aweb_secrets.operation import MAX_RESPONSE_BYTES, METHODS, SIDE_EFFECTS, OperationTemplate
from aweb_secrets.policy import APPROVALS, IDENTIFIER, Policy

_FIELDS = {
    "secret_ref",
    "role",
    "operation_id",
    "method",
    "url",
    "projection",
    "max_bytes",
    "side_effect",
    "approval",
    "capacity",
    "window_s",
    "reason",
}
_GRAPH_FIELDS = {
    "secret_ref", "mailbox", "mailbox_address", "vault_agents", "include_archive", "reason",
}


def _invalid(field: str, status: int = 400) -> HTTPException:
    # Only emitted before the sole writer is called, never after a commit or delivery.
    return HTTPException(
        status, {"code": "grant_form_invalid", "field": field, "draft_created": False}
    )


def _configuration(body: dict[str, Any], *, secret_id: str, owner: str) -> list[dict[str, Any]]:
    """Translate the form into existing, validated creation projections.

    This form defines one fixed HTTP operation, its exact destination and a
    role grant. Digests use the domain models; the browser does not author pins.
    """
    try:
        url = canonical_url(_text(body, "url"))
    except (ValueError, TypeError, EgressRefused):
        raise _invalid("url") from None
    if url.query or "*" in url.path:
        raise _invalid("url")
    role = body["role"]
    if (
        not isinstance(role, str)
        or not role.startswith("secrets.role.")
        or not is_secrets_role(role)
    ):
        raise _invalid("role")
    if not isinstance(body["operation_id"], str) or not IDENTIFIER.fullmatch(body["operation_id"]):
        raise _invalid("operation_id")
    for field, choices in (
        ("method", METHODS),
        ("side_effect", SIDE_EFFECTS),
        ("approval", APPROVALS),
    ):
        if body[field] not in choices:
            raise _invalid(field)
    for field in ("max_bytes", "capacity", "window_s"):
        if type(body[field]) is not int or body[field] < 1:
            raise _invalid(field)
    if body["max_bytes"] > MAX_RESPONSE_BYTES:
        raise _invalid("max_bytes")
    if not isinstance(body["projection"], list):
        raise _invalid("projection")
    try:
        operation = OperationTemplate.from_after(
            {
                "v": 1,
                "secret_id": secret_id,
                "operation_id": _text(body, "operation_id"),
                "method": body["method"],
                "url": {"host": url.host, "port": url.port, "path": url.path},
                "params": {},
                "body": None,
                "headers": {"fixed": {}, "allowed": []},
                "response": {
                    "projection": body["projection"],
                    "max_bytes": body["max_bytes"],
                    "content_types": ["application/json"],
                },
                "side_effect": body["side_effect"],
                "token_issuing": False,
                "result_handling": "deny",
                "store_as_secret": None,
                "idempotency": None,
                "reconcile": None,
                "timeout_s": 10,
            }
        )
        pin = {"operation_id": operation.operation_id, "digest": operation.digest}
        policy = Policy.from_body(
            {
                "approval": body["approval"],
                # The secret can independently require approval, now or after a
                # confirmed change. Keep its owner approval route available.
                "approvers": ["owner:" + owner],
                "approval_ttl_s": 900,
                "rate": [
                    {
                        "per": "principal_secret",
                        "capacity": body["capacity"],
                        "window_s": body["window_s"],
                    }
                ],
                "windows": None,
                "operations": [pin],
                "open_request": False,
                "request": {"max_bytes": 65536},
                "result": {"max_bytes": body["max_bytes"]},
                "requires_attestation": None,
                "distinct_approver": "required",
            }
        )
        grant = GrantRecord.from_after(
            {
                "v": 1,
                "grant_id": mint_grant_id(),
                "secret_id": secret_id,
                "subject": {"role": role},
                "capability": "use",
                "operations": [pin],
                "open_request": False,
                "jobs": [],
                "policy_digest": policy.digest,
            }
        )
    except (ValueError, TypeError):
        raise _invalid("projection") from None
    destination = {
        "v": 1,
        "destination_id": mint_destination_id(),
        "secret_id": secret_id,
        "scheme": "https",
        "host": url.host,
        "port": url.port,
        "path": url.path,
        "methods": [body["method"]],
        "query_rules": {},
        "header_allow": [],
        "network": "public",
        "cidr": None,
        "response_max_bytes": body["max_bytes"],
    }
    try:
        DestinationRecord.from_after(
            destination, internal_destinations=InternalDestinations.ALLOWED
        )
    except DestinationInvalid:
        raise _invalid("url") from None
    return [
        {
            "kind": "destination",
            "op": "create",
            "ref": destination["destination_id"],
            "after": destination,
        },
        {
            "kind": "operation",
            "op": "create",
            "ref": operation.operation_id,
            "after": dict(operation.after),
        },
        {"kind": "policy", "op": "create", "ref": policy.digest, "after": {"v": 1, **policy.body}},
        {"kind": "grant", "op": "create", "ref": grant.grant_id, "after": grant.after},
    ]


def _input(name: str, label: str, *, kind: str = "text", extra: str = "") -> str:
    return (
        f'<p><label for="grant-{name}">{label}</label><br>'
        f'<input id="grant-{name}" name="{name}" type="{kind}" size="24" required {extra}></p>'
    )


def _form(installation_id: str) -> str:
    action = "/installations/" + installation_id + "/grants/drafts"
    return (
        "<h1>Grant HTTP access to a role</h1>"
        '<p><a href="/grants/graph-mailbox/new">Grant access to a Microsoft 365 mailbox</a></p>'
        "<p>Choose an existing secret and the exact request this role may make. "
        "Access stays unchanged until you review the draft and confirm with your passkey.</p>"
        f'<form id="role-grant" class="form" method="post" action="{escape(action, quote=True)}" '
        f'data-installation="{escape(installation_id, quote=True)}">'
        '<fieldset class="card"><legend>Secret and role</legend>'
        + _input("secret_ref", "Secret name", extra='maxlength="64" autocomplete="off"')
        + _input(
            "role",
            "Role",
            extra='pattern="secrets\\.role\\.[a-z0-9][a-z0-9_-]*" '
            'placeholder="secrets.role.deployers" autocomplete="off"',
        )
        + "<p>Every member of this role receives this grant. Role membership is managed separately.</p>"
        '</fieldset><fieldset class="card"><legend>Allowed request</legend>'
        + _input(
            "operation_id",
            "Operation name",
            extra='pattern="[a-z0-9][a-z0-9_-]{0,63}" maxlength="64" autocomplete="off"',
        )
        + '<p><label for="grant-method">Method</label><br><select id="grant-method" name="method">'
        + "".join(f"<option>{method}</option>" for method in METHODS)
        + "</select></p>"
        + _input(
            "url",
            "Destination URL",
            kind="url",
            extra='placeholder="https://api.example.com/status" '
            'autocomplete="off" aria-describedby="grant-url-help"',
        )
        + '<p id="grant-url-help">An exact HTTPS URL, without query parameters or wildcards. '
        "This request has no body or agent-supplied parameters.</p>"
        '<p><label for="grant-projection">Response fields</label><br>'
        '<textarea id="grant-projection" name="projection" cols="24" rows="3" required '
        'aria-describedby="grant-projection-help">/status</textarea></p>'
        '<p id="grant-projection-help">One JSON pointer per line, such as /status. '
        "Only these fields are returned from the JSON response.</p>"
        + _input(
            "max_bytes",
            "Maximum response bytes",
            kind="number",
            extra='min="1" max="67108864" value="16777216"',
        )
        + '<p><label for="grant-side-effect">Side effect</label><br>'
        '<select id="grant-side-effect" name="side_effect">'
        '<option value="none">None</option><option value="reversible">Reversible change</option>'
        '<option value="irreversible">Irreversible change</option></select></p>'
        "<p>Requests are not automatically repeated after an uncertain outcome.</p></fieldset>"
        '<fieldset class="card"><legend>Use policy</legend>'
        '<p><label for="grant-approval">Approval</label><br><select id="grant-approval" name="approval">'
        '<option value="none">No additional approval</option>'
        '<option value="per_use">My passkey approval for every use</option></select></p>'
        "<p>The secret’s own approval requirements still apply; you approve those uses with your passkey. A use approval expires after 15 minutes.</p>"
        + _input("capacity", "Uses per agent", kind="number", extra='min="1" value="30"')
        + _input("window_s", "Rate window in seconds", kind="number", extra='min="1" value="3600"')
        + '</fieldset><p><label for="grant-reason">Reason for this change (optional)</label><br>'
        '<textarea id="grant-reason" name="reason" cols="24" rows="2" maxlength="512"></textarea></p>'
        '<button class="btn primary" type="submit">Save a draft for review</button></form>'
        '<p id="form-status" role="status" aria-live="polite"></p>'
        '<script src="/console-forms/grants.js" defer></script>'
    )


def _graph_form(installation_id: str) -> str:
    installation = escape(installation_id, quote=True)
    return (
        "<h1>Grant access to a Microsoft 365 mailbox</h1>"
        "<p>Choose an active Graph certificate secret dedicated to this mailbox, with no existing use grants. "
        "Only the listed vault-team identities "
        "can use this mailbox. You approve each send, reply and archive with your passkey.</p>"
        f'<form id="graph-mailbox" class="form" method="post" data-installation="{installation}" '
        f'action="/installations/{installation}/grants/graph-mailbox/drafts">'
        + _input("secret_ref", "Certificate secret name", extra='maxlength="64" autocomplete="off"')
        + _input("mailbox", "Mailbox object ID", extra='maxlength="36" autocomplete="off"')
        + _input(
            "mailbox_address", "Mailbox email address", kind="email",
            extra='maxlength="254" autocomplete="off"',
        )
        + '<p><label for="graph-vault-agents">Vault member DIDs, one per line</label><br>'
        '<textarea id="graph-vault-agents" name="vault_agents" rows="4" required></textarea></p>'
        '<p><label><input type="checkbox" name="include_archive"> Allow moving to Archive</label></p>'
        "<p>Archive requires Exchange mailbox-scoped Mail.ReadWrite permission.</p>"
        '<p><label for="graph-reason">Reason for this change (optional)</label><br>'
        '<textarea id="graph-reason" name="reason" rows="2" maxlength="512"></textarea></p>'
        '<button class="btn primary" type="submit">Save a draft for review</button></form>'
        '<p id="form-status" role="status" aria-live="polite"></p>'
        '<script src="/console-forms/grants.js" defer></script>'
    )


def grant_forms_router(*, changes: ConsoleChanges) -> APIRouter:
    shell = changes.console.shell
    router = APIRouter(route_class=ConsoleRoute)

    async def grant_page(request: Request, *, graph: bool) -> Response:
        try:
            current = await changes.console.sessions.current(request)
        except HTTPException as refused:
            return shell.refused_page(refused)
        async with _errors(changes.console, current):
            installation_id = await changes.installation_for_session(current)
            admitted = await changes._installation(current, installation_id)
            async with changes.console.db.transaction() as tx:
                await changes._owner(tx, current, admitted)
                await changes.console.fence.check(tx, admitted)
                changes._unexpired(current)
        title = "Grant mailbox access" if graph else "Grant access"
        body_html = _graph_form(installation_id) if graph else _form(installation_id)
        return shell.console_page(title=title, body_html=body_html, session=current)

    @router.get("/grants/new")
    async def page(request: Request) -> Response:
        return await grant_page(request, graph=False)

    @router.get("/grants/graph-mailbox/new")
    async def graph_page(request: Request) -> Response:
        return await grant_page(request, graph=True)

    async def grant_draft(installation_id: str, request: Request, *, graph: bool) -> dict[str, Any]:
        current = await changes.console.sessions.current(request)
        body = await _body(request, _GRAPH_FIELDS if graph else _FIELDS)
        async with _errors(changes.console, current):
            admitted = await changes._installation(current, installation_id)
            async with changes.console.db.transaction() as tx:
                await changes._owner(tx, current, admitted)
                stored = await records.secret_by_ref(
                    tx, installation_id, admitted.team_id, _text(body, "secret_ref"), lock=False
                )
                if stored is None or stored.state != "active":
                    raise _invalid("secret_ref", 404)
                owner = await changes.console.directory.owner_of(tx, current.scope)
                if owner is None:
                    raise HTTPException(403, "owner_required")
                if graph:
                    if (
                        not isinstance(body["vault_agents"], list)
                        or type(body["include_archive"]) is not bool
                    ):
                        raise _invalid("vault_agents")
                    try:
                        items = mailbox_changes(
                            stored.record,
                            mailbox=_text(body, "mailbox"),
                            mailbox_address=_text(body, "mailbox_address"),
                            vault_agents=body["vault_agents"],
                            approver=owner.principal_did,
                            include_archive=body["include_archive"],
                        )
                    except ValueError:
                        raise _invalid("mailbox") from None
                else:
                    items = _configuration(
                        body, secret_id=stored.record.secret_id, owner=owner.principal_did
                    )
                await changes.console.fence.check(tx, admitted)
                changes._unexpired(current)
            return await changes.draft(
                current, installation_id, {"changes": items, "sealed": [], "reason": body["reason"]},
                require_unused_broker_secret=stored.record.secret_id if graph else None,
            )

    @router.post(
        "/installations/{installation_id}/grants/drafts",
        status_code=201,
        dependencies=[Depends(_require_json)],
    )
    async def draft(installation_id: str, request: Request) -> dict[str, Any]:
        return await grant_draft(installation_id, request, graph=False)

    @router.post(
        "/installations/{installation_id}/grants/graph-mailbox/drafts",
        status_code=201,
        dependencies=[Depends(_require_json)],
    )
    async def graph_draft(installation_id: str, request: Request) -> dict[str, Any]:
        return await grant_draft(installation_id, request, graph=True)

    @router.get("/console-forms/grants.js")
    async def script() -> Response:
        return Response(
            Path(__file__).with_name("grants.js").read_text(), media_type="text/javascript"
        )

    return router
