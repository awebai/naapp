"""Real Chromium steps for the Compose customer journey."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from playwright.sync_api import expect


def load_passkeys(device_path, cdp, authenticator_id, rp_id):
    credentials = json.loads(device_path.read_text())
    for item in credentials:
        if item["rp_id"] != rp_id:
            continue
        private_key = subprocess.run(
            ["openssl", "pkcs8", "-topk8", "-nocrypt", "-outform", "DER"],
            input=item["key"].encode(),
            capture_output=True,
            check=True,
        ).stdout
        cdp.send(
            "WebAuthn.addCredential",
            {
                "authenticatorId": authenticator_id,
                "credential": {
                    "credentialId": item["credential_id"],
                    "isResidentCredential": True,
                    "rpId": item["rp_id"],
                    "privateKey": base64.b64encode(private_key).decode(),
                    "userHandle": item["user_handle"],
                    "signCount": item["sign_count"],
                },
            },
        )
    return credentials


def save_passkey_counters(device_path, cdp, authenticator_id, credentials):
    updated = cdp.send("WebAuthn.getCredentials", {"authenticatorId": authenticator_id})
    counts = {
        base64.b64decode(item["credentialId"]): item["signCount"] for item in updated["credentials"]
    }
    for item in credentials:
        cid = base64.b64decode(item["credential_id"])
        if cid in counts:
            item["sign_count"] = counts[cid]
    device_path.write_text(json.dumps(credentials))


def signin(page, origin, scope):
    page.goto(origin + "/login?scope=" + quote(scope, safe=""))
    with page.expect_response(lambda response: response.url == origin + "/login") as login:
        page.locator("#login button").click()
    response = login.value
    if response.status != 200:
        detail = response.json().get("detail")
        code = detail if isinstance(detail, str) and re.fullmatch("[a-z_]+", detail) else "unknown"
        raise RuntimeError(f"passkey_login_{response.status}_{code}")
    page.wait_for_url(origin + "/")


def post(page, path, body):
    return page.evaluate("([path, body]) => window.awebConsole.post(path, body)", [path, body])


def enter_native_value(page, origin, path, ref, value):
    page.get_by_role("link", name="Create a secret", exact=True).click()
    form = page.locator("#native-secret")
    secret_id = form.locator('[name="secret_id"]').input_value()
    form.get_by_label("Secret name").fill(ref)
    form.get_by_label("Authentication kind").select_option("bearer")
    field = form.locator('[data-kind="bearer"] [data-value="value"]')
    expect(field).to_have_attribute("type", "password")
    field.fill(value)
    with page.expect_response(origin + path) as staging:
        form.get_by_role("button", name="Save a native value draft", exact=True).click()
    response = staging.value
    if response.status != 201:
        raise RuntimeError(f"native_value_{response.status}")
    receipt = response.json()
    if set(receipt) != {"version", "created_at"} or receipt["version"] != 1:
        raise RuntimeError("native_receipt_invalid")
    expect(page.locator("#native-status")).to_contain_text("Version 1 sealed")
    expect(field).to_have_value("")
    state = page.evaluate("() => JSON.stringify({...sessionStorage, ...localStorage})")
    if any(value in evidence for evidence in (response.text(), page.content(), state)):
        raise RuntimeError("native_value_retained")
    return {"secret_id": secret_id}


def approve_change(page, origin, link, root, change_id):
    page.goto(link)
    # Native value entry returns the version and timestamp. The emailed link
    # locates its pending change, which the console shows at this review URL.
    change = re.escape(change_id) if change_id else r"chg_[a-f0-9]+"
    page.wait_for_url(re.compile(re.escape(origin + root) + "/" + change + r"/review$"))
    page.get_by_role("button", name="Confirm with your passkey", exact=True).click()
    expect(page.locator("#form-status")).to_have_attribute("data-state", "applied")


def approve_use(page, link):
    page.goto(link)
    page.get_by_role("button", name="Review with your passkey", exact=True).click()
    expect(page.locator("#request-review")).to_be_visible()
    page.get_by_role("button", name="Approve with your passkey", exact=True).click()
    expect(page.locator("#use-approval")).to_have_attribute("data-state", "approved")


def read_audit(page, origin, expected):
    deadline = time.monotonic() + 60
    while True:
        response = page.request.get(origin + "/v1/events", params={"limit": 200})
        if response.status != 200:
            raise RuntimeError(f"logs_events_{response.status}")
        by_id = {event["event_id"]: event for event in response.json()["events"]}
        if all(event_id in by_id for event_id in expected):
            return {event_id: by_id[event_id] for event_id in expected}
        if time.monotonic() >= deadline:
            raise RuntimeError("logs_delivery_timeout")
        time.sleep(1)


def customer_console(args, browser, authenticator):
    command = json.load(sys.stdin)
    action = command["action"]
    origin = command["origin"]
    rp_id = urlsplit(origin).hostname
    device_path = Path(args.device)
    session_path = device_path.with_name(f"{rp_id}-console-session.json")
    saved = json.loads(session_path.read_text()) if session_path.exists() else None
    context = browser.new_context(storage_state=saved["browser"] if saved else None)
    if saved:
        # Reconstruct the same holder's tab, including its per-tab CSRF value.
        state = json.dumps([origin, saved["key"], saved["csrf"]])
        context.add_init_script(f"""
            (() => {{
                const [origin, key, csrf] = {state};
                if (location.origin === origin && sessionStorage.getItem(key) === null) {{
                    sessionStorage.setItem(key, csrf);
                }}
            }})();
        """)
    page = context.new_page()
    cdp, authenticator_id = authenticator(page, True)
    credentials = load_passkeys(device_path, cdp, authenticator_id, rp_id)
    try:
        if not saved:
            signin(page, origin, command["scope"])
        elif action in {"post", "enter-native"}:
            page.goto(origin + "/")
        if action == "enter-native":
            result = enter_native_value(
                page, origin, command["path"], command["ref"], command["value"]
            )
        elif action == "post":
            result = post(page, command["path"], command["body"])
        elif action == "approve-change":
            approve_change(page, origin, command["link"], command["path"], command.get("change_id"))
            result = {"state": "applied"}
        elif action == "approve-use":
            approve_use(page, command["link"])
            result = {"state": "approved"}
        elif action == "read-audit":
            result = read_audit(page, origin, command["event_ids"])
        else:
            raise ValueError("unknown customer console action")
        if action != "read-audit":
            session = page.evaluate("""() => {
                const key = window.awebConsole.csrfKey;
                return {key, csrf: sessionStorage.getItem(key)};
            }""")
            if not session["csrf"]:
                raise RuntimeError("console_csrf_missing")
            session["browser"] = {"cookies": context.cookies(), "origins": []}
            session_path.write_text(json.dumps(session))
            session_path.chmod(0o600)
    except Exception as exc:
        # Traces from a browser on an approval link can include its locator.
        failure = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        sys.exit(f"customer console {action}: {failure}")
    finally:
        save_passkey_counters(device_path, cdp, authenticator_id, credentials)
    print(json.dumps(result))
