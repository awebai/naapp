"""Human configuration entry and frozen change review pages on the shared console shell."""

import json
from html import escape
from pathlib import Path
from urllib.parse import quote

from aweb_naapp.console import ConsoleRoute
from aweb_secrets_values import CAPTURE_FIELDS, OAUTH2_CERTIFICATE, VALUE_FIELDS
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from aweb_secrets import records
from aweb_secrets.change import MAX_REASON_CHARS
from aweb_secrets.client_assertion import MAX_PEM_BYTES
from aweb_secrets.console_changes import ConsoleChanges, _errors
from aweb_secrets.console_forms.grants import grant_forms_router
from aweb_secrets.secret import (
    ASSERTION_ALGORITHMS,
    KINDS,
    OAUTH2_SECRET,
    SecretRecord,
    mint_secret_id,
)

_ASSETS = Path(__file__).parent
_SCRIPT = (
    '<link rel="stylesheet" href="/console-forms/forms.css">'
    '<script src="/console-forms/forms.js" defer></script>'
)
_STATUS = '<p id="form-status" role="status" aria-live="polite"></p>'
_REVIEW = (
    "<h1>Review changes</h1>"
    '<p id="approval-instructions">Open the approval link sent to your registered contact.</p>'
    '<section id="change-summary" hidden><h2>Frozen change</h2>'
    '<dl id="change-facts"></dl>'
    '<p id="backend-replacement-notice" hidden><strong>Activating a new service-account token invalidates existing 1Password item bindings. '
    "Those items need to be linked and confirmed again. Disabled secrets stay disabled.</strong></p>"
    '<h2>Reason supplied by the drafter (untrusted)</h2><pre id="change-reason"></pre>'
    '<button id="confirm-change" type="button" disabled>Confirm with your passkey</button></section>'
)

_KIND_NAMES = {
    "bearer": "Bearer token",
    "basic": "Basic credentials",
    "header": "Custom header",
    "query": "Query parameter",
    OAUTH2_SECRET: "OAuth2 client credentials",
    OAUTH2_CERTIFICATE: "OAuth2 certificate credentials",
    "aws-sigv4": "AWS Signature Version 4",
}


def _kind_fields(prefix: str, *, fixed_kind: str | None = None) -> str:
    fields = []
    oauth_params = (("token_endpoint", "Token endpoint URL", True), ("scope", "OAuth scopes", False))
    params = {
        "header": (("name", "Header name", True), ("prefix", "Header prefix", False)),
        "query": (("param", "Query parameter name", True),),
        OAUTH2_SECRET: oauth_params,
        OAUTH2_CERTIFICATE: (*oauth_params, ("audience", "Assertion audience URL", True)),
        "aws-sigv4": (("region", "AWS region", True), ("service", "AWS service", True)),
    }
    reference = prefix == "reference"
    value_fields = CAPTURE_FIELDS if reference else VALUE_FIELDS
    for kind in (fixed_kind,) if fixed_kind else (kind for kind in KINDS if kind in value_fields):
        inactive = " hidden disabled" if fixed_kind is None and kind != "bearer" else ""
        fields.append(
            f'<fieldset data-kind="{kind}"{inactive}><legend>{_KIND_NAMES[kind]}</legend>'
        )
        for name, label, needed in params.get(kind, ()) if fixed_kind is None else ():
            identity = f"{prefix}-{kind}-{name}"
            fields.append(
                f'<label for="{identity}">{label}</label><input id="{identity}" '
                f'data-param="{name}" autocomplete="off" spellcheck="false"'
                + (" required" if needed else "")
                + ">"
            )
        if kind == OAUTH2_SECRET and fixed_kind is None:
            fields.append(
                f'<label for="{prefix}-client-auth">Client authentication</label>'
                f'<select id="{prefix}-client-auth" data-param="client_auth">'
                '<option value="client_secret_basic">HTTP Basic authentication</option>'
                '<option value="client_secret_post">Form body</option></select>'
            )
        if kind == OAUTH2_CERTIFICATE and fixed_kind is None:
            algorithms = "".join(f'<option value="{alg}">{alg}</option>' for alg in ASSERTION_ALGORITHMS)
            fields.append(
                f'<label for="{prefix}-signing-alg">Assertion signing algorithm</label>'
                f'<select id="{prefix}-signing-alg" data-param="signing_alg">{algorithms}</select>'
            )
        required, optional = value_fields[kind]
        for name in sorted(required | optional):
            identity = f"{prefix}-{kind}-{name}-field"
            label = name.replace("_", " ").capitalize()
            if reference:
                label += " field ID"
            if name in optional:
                label += " (optional)"
            if kind == OAUTH2_CERTIFICATE and name == "private_key_pem":
                fields.append(
                    f'<label for="{identity}">Private key PEM file</label><input id="{identity}" '
                    f'type="file" data-value="{name}" data-max-bytes="{MAX_PEM_BYTES}" required>'
                )
                continue
            if kind == OAUTH2_CERTIFICATE and name == "certificate_pem":
                fields.append(
                    f'<label for="{identity}">{label}</label><textarea id="{identity}" '
                    f'data-value="{name}" autocomplete="off" spellcheck="false" rows="4" required>'
                    "</textarea>"
                )
                continue
            attributes = (
                'data-field="' + name + '" pattern="[A-Za-z0-9_-]{1,128}" maxlength="128"'
                if reference else 'data-value="' + name + '" type="password"'
            )
            fields.append(
                f'<label for="{identity}">{label}</label><input id="{identity}" '
                f'{attributes} autocomplete="off" spellcheck="false"'
                + (" required" if name in required and (reference or name != "username") else "")
                + ">"
            )
        fields.append("</fieldset>")
    return "".join(fields)


def _secret_fields(prefix: str) -> str:
    kinds = CAPTURE_FIELDS if prefix == "reference" else VALUE_FIELDS
    options = "".join(
        f'<option value="{kind}" data-auth-kind>{_KIND_NAMES[kind]}</option>' for kind in KINDS
        if kind in kinds
    )
    return (
        f'<input type="hidden" name="secret_id" value="{mint_secret_id()}">'
        f'<label for="{prefix}-name">Secret name</label>'
        f'<input id="{prefix}-name" name="ref" pattern="[a-z0-9][a-z0-9_-]{{0,63}}" '
        'maxlength="64" autocomplete="off" required>'
        f'<label for="{prefix}-description">Description (optional)</label>'
        f'<textarea id="{prefix}-description" name="description" maxlength="512" rows="2"></textarea>'
        f'<label for="{prefix}-kind">Authentication kind</label>'
        f'<select id="{prefix}-kind" name="kind">{options}</select>'
        f'<label for="{prefix}-sensitivity">Sensitivity</label>'
        f'<select id="{prefix}-sensitivity" name="sensitivity">'
        '<option value="standard">Standard</option><option value="sensitive">Sensitive</option></select>'
        '<label class="check"><input type="checkbox" name="approve_only">Require approval for every use</label>'
    )


def _reference_form(installation_id: str, credential_version: int) -> str:
    action = "/installations/" + installation_id + "/onepassword/references"
    return (
        "<p>Select the item and field IDs from your connected vault. Enter references here; "
        "the field values stay in 1Password. Review and confirm the draft before agents can use it.</p>"
        f'<form id="onepassword-reference" method="post" action="{escape(action, quote=True)}" '
        f'data-installation="{escape(installation_id, quote=True)}">'
        f'<input type="hidden" name="credential_version" value="{credential_version}">'
        + _secret_fields("reference")
        + '<label for="reference-vault">Vault ID</label>'
        '<input id="reference-vault" name="vault_id" autocomplete="off" spellcheck="false" '
        'pattern="[a-z0-9]{26}" minlength="26" maxlength="26" required>'
        '<label for="reference-item">Item ID</label>'
        '<input id="reference-item" name="item_id" autocomplete="off" spellcheck="false" '
        'pattern="[a-z0-9]{26}" minlength="26" maxlength="26" required>'
        + _kind_fields("reference")
        + '<label for="reference-reason">Reason for this change (optional)</label>'
        '<input id="reference-reason" name="reason" autocomplete="off">'
        '<button type="submit">Save a reference draft</button></form>'
    )


def _native_form(installation_id: str, record: SecretRecord | None = None) -> str:
    action = "/installations/" + installation_id + "/native-values"
    if record is not None:
        action += "/" + quote(record.ref, safe="")
    return (
        '<p>Your value is sealed when you save the draft. Confirm it with your passkey '
        'before agents can use it.</p>'
        f'<form id="native-secret" method="post" action="{escape(action, quote=True)}">'
        + (_secret_fields("native") if record is None else "")
        + _kind_fields("native", fixed_kind=record.kind if record else None)
        + '<label for="native-reason">Reason for this change (optional)</label>'
        f'<input id="native-reason" name="reason" autocomplete="off" maxlength="{MAX_REASON_CHARS}">'
        '<button type="submit">Save a native value draft</button></form>'
        '<p id="native-status" role="status" aria-live="polite"></p>'
    )


def console_forms_router(*, changes: ConsoleChanges) -> APIRouter:
    """Mount on the human app with the same ConsoleChanges and value routers.

    No values go into page templates, URLs, browser storage or returned results.
    The forms use only the shared POST and WebAuthn assertion helpers.
    """
    shell = changes.console.shell
    router = APIRouter(route_class=ConsoleRoute)
    router.include_router(grant_forms_router(changes=changes))

    async def session(request: Request):
        return await changes.console.sessions.current(request)

    async def reference_page(
        request: Request, installation_id: str | None = None, *, include_native: bool = False
    ) -> Response:
        try:
            current = await session(request)
        except HTTPException as refused:
            return shell.refused_page(refused)
        async with _errors(changes.console, current):
            if installation_id is None:
                installation_id = await changes.installation_for_session(current)
            admitted = await changes._installation(current, installation_id)
            async with changes.console.db.transaction() as tx:
                await changes._owner(tx, current, admitted)
                version = await tx.fetch_value(
                    "SELECT active_version FROM {{tables.onepassword_credentials}} "
                    "WHERE installation_id=$1 AND team_id=$2",
                    admitted.installation_id,
                    admitted.team_id,
                )
                await changes.console.fence.check(tx, admitted)
                changes._unexpired(current)
        body = (
            _reference_form(installation_id, version)
            if version is not None
            else "<p>Connect and confirm your team’s 1Password service account first.</p>"
            '<p><a href="/backend-credentials/onepassword/new">Connect 1Password</a></p>'
        )
        if include_native:
            body = (
                '<h1>Create a secret</h1><h2>Store a native value</h2>'
                + _native_form(installation_id)
                + '<p><a href="/secrets/replace">Replace a native value</a></p>'
                '<h2>Link a 1Password secret</h2>' + body
            )
        else:
            body = "<h1>Link a 1Password secret</h1>" + body
        return shell.console_page(
            title="Create a secret" if include_native else "Link a 1Password secret",
            session=current, body_html=body + _STATUS + _SCRIPT,
        )

    @router.get("/secrets/new", response_class=HTMLResponse)
    async def create_secret_entry(request: Request) -> Response:
        return await reference_page(request, include_native=True)

    @router.get("/onepassword/references/new", response_class=HTMLResponse)
    async def reference_entry(request: Request) -> Response:
        return await reference_page(request)

    @router.get(
        "/installations/{installation_id}/onepassword/references/new", response_class=HTMLResponse
    )
    async def scoped_reference_entry(installation_id: str, request: Request) -> Response:
        return await reference_page(request, installation_id)

    @router.get("/secrets/replace", response_class=HTMLResponse)
    async def replacement_entry(request: Request) -> Response:
        try:
            current = await session(request)
            async with _errors(changes.console, current):
                installation_id = await changes.installation_for_session(current)
                ref = request.query_params.get("ref")
                if ref is None:
                    body = (
                        '<form method="get" action="/secrets/replace">'
                        '<label for="replace-name">Secret name</label>'
                        '<input id="replace-name" name="ref" required>'
                        '<button type="submit">Continue</button></form>'
                    )
                else:
                    admitted = await changes._installation(current, installation_id)
                    async with changes.console.db.transaction() as tx:
                        await changes._owner(tx, current, admitted)
                        stored = await records.secret_by_ref(
                            tx, installation_id, admitted.team_id, ref, lock=False
                        )
                    if stored is None or stored.record.backend != "native":
                        raise HTTPException(404, "secret_unknown")
                    body = f'<p>{escape(stored.record.ref)}</p>' + _native_form(
                        installation_id, stored.record
                    )
        except HTTPException as refused:
            return shell.refused_page(refused)
        return shell.console_page(
            title="Replace a native value",
            session=current, body_html="<h1>Replace a native value</h1>" + body + _SCRIPT,
        )

    @router.get("/backend-credentials/onepassword/new", response_class=HTMLResponse)
    async def token_page(request: Request) -> Response:
        try:
            current = await session(request)
        except HTTPException as refused:
            return shell.refused_page(refused)
        async with _errors(changes.console, current):
            installation = await changes.installation_for_session(current)
        path = "/installations/" + installation + "/backend-credentials/onepassword/versions"
        return shell.console_page(
            title="Connect 1Password",
            session=current,
            body_html="<h1>Connect 1Password</h1>"
            "<p>Use a read-only service-account token limited to your team’s vault. "
            "The token is sealed and is never shown again.</p>"
            f'<form id="backend-token" method="post" action="{escape(path, quote=True)}" '
            f'data-installation="{escape(installation, quote=True)}">'
            '<label for="service-account-token">1Password service-account token</label>'
            '<input id="service-account-token" type="password" name="value" '
            'autocomplete="off" spellcheck="false" required>'
            '<button type="submit">Save a draft</button></form>' + _STATUS + _SCRIPT,
        )

    @router.get("/approvals", response_class=HTMLResponse)
    async def approval_page(request: Request) -> Response:
        try:
            current = await session(request)
        except HTTPException as refused:
            if refused.status_code != 401:
                return shell.refused_page(refused)
            response = shell.page(
                "Sign in to review",
                "<div data-approval-entry></div><h1>Sign in to review an approval</h1>"
                "<p>The approval link selects your sign-in. Your passkey is required to continue.</p>"
                '<form id="approval-login" method="post" action="/login">'
                '<button type="submit" disabled>Sign in with your passkey</button></form>'
                + _STATUS
                + _SCRIPT,
            )
            response.headers.update(refused.headers or {})
            return response
        return shell.console_page(
            title="Review approval",
            session=current,
            body_html="<div data-approval-entry></div>" + _REVIEW + _STATUS + _SCRIPT,
        )

    @router.get(
        "/installations/{installation_id}/change-sets/{change_id}/review",
        response_class=HTMLResponse,
    )
    async def review_page(installation_id: str, change_id: str, request: Request) -> Response:
        try:
            current = await session(request)
        except HTTPException as refused:
            return shell.refused_page(refused)
        async with _errors(changes.console, current):
            frozen = await changes.read(current, installation_id, change_id)
        # HTML escaping protects even </pre>/<script> in an untrusted reason.
        # The script reads textContent, and every displayed field uses textContent.
        projection = '<pre id="frozen-change" hidden>' + escape(json.dumps(frozen)) + "</pre>"
        return shell.console_page(
            title="Review approval",
            session=current,
            body_html=_REVIEW + projection + _STATUS + _SCRIPT,
        )

    @router.get("/console-forms/forms.css")
    async def style() -> Response:
        return Response((_ASSETS / "forms.css").read_text(), media_type="text/css")

    @router.get("/console-forms/forms.js")
    async def script() -> Response:
        return Response((_ASSETS / "forms.js").read_text(), media_type="text/javascript")

    return router
