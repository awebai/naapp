"""The H owner-acts journey with the real aw, end to end (make e2e only).

Real: AWID with DNS verification, aweb, CoreDNS, SMTP, this app's production
image behind TLS on two public hostnames, the Logs app as a team deployment
runs it (the audit target a designation's governance needs), the aw binary,
the holder's human key in a real Secret Service, and the shared handoff page
and routes. The only stand-in is the person: a software authenticator drives
the page's JSON routes from the holder's own network namespace, so aw's
loopback receives the code.

That same owner uses Chromium to enter a native value and approve a request.
An isolated released aw submits the request and retries it over team auth.
The upstream verifies the injected credential, Logs reads the linked audit
events, and the databases and key files are backed up at the end.

The owner signs the designation locally, so aw keeps no request to replay:
its proof is the app's own records (team_journey_support.assert_designated),
and a refused duplicate leaves them unchanged.

Topology, as on H: the holder (a human) runs aw on their own machine, where the
browser is; the team owner signs with the team's controller key on another.
Setup authority (the namespace key that creates the team) is used only to set
the lane up, and the team key is then moved to the controller's machine.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pexpect
import pytest
from aweb_naapp.roles import holding_scope
from aweb_naapp_testing.compose_lane import (
    CA_PATH,
    DEFAULT_SUBNET,
    REGISTRY_ORIGIN,
    TEAM_SERVER_ORIGIN,
    ComposeLane,
    await_awid_txt,
    identity_folder,
)
from aweb_naapp_testing.compose_lane import (
    docker as _docker,
)
from customer_acceptance import (
    UPSTREAM,
    confirm_native_bearer,
    create_agent,
    exercise_customer_use,
    start_upstream,
)
from nacl.signing import SigningKey
from team_journey_support import (
    Designation,
    Holding,
    activate_hooks,
    assert_designated,
    assert_unchanged,
    aw,
    aw_refused,
    credential_sha256,
    designate_command,
    designation_snapshot,
    first_json,
    install_app,
    publish_team_server,
    sql_literal,
)
from team_journey_support import Holder as SharedHolder

pytestmark = pytest.mark.e2e

APP_DIR = Path(__file__).resolve().parents[1]
REPO = APP_DIR.parents[1]
DOMAIN = "h.test"
TEAM = "h"
# The lane's own app id: only the hosted origin may serve the hosted id "secrets".
APP_ID = "lane-secrets"
TEAM_ID = f"{TEAM}:{DOMAIN}"
REGISTRY = REGISTRY_ORIGIN
AWEB = TEAM_SERVER_ORIGIN
API = "https://api.secrets.svc.test"
CONSOLE = "https://console.secrets.svc.test"
ROLE = "secrets.owner"
# The lane's Logs, and the network both apps' hooks stay inside.
LOGS_APP_ID = "lane-logs"
READER_ROLE = "logs.reader"
LOGS_API = "https://api.logs.svc.test"
LOGS_CONSOLE = "https://console.logs.svc.test"
LANE_SUBNET = DEFAULT_SUBNET
# The lane's Secrets keeps its records in this schema of the lane's database.
SECRETS_SCHEMA = "secrets"
# The notice contact the owner designates the holder with.
CONTACT = "holder@h.test"
# Every setting the Logs base service names, as a deployment's .env gives
# them: Compose reads them when it loads the file, for every lane command.
LOGS_COMPOSE_ENV = {
    "LOGS_APP_ID": LOGS_APP_ID,
    "LOGS_PUBLIC_ORIGIN": LOGS_API,
    "LOGS_CONSOLE_ORIGIN": LOGS_CONSOLE,
    "TEAM_ID": TEAM_ID,
    "AWID_REGISTRY_URL": REGISTRY,
    "LOGS_EMAIL_SMTP_HOST": "mail.svc.test",
    "LOGS_EMAIL_SMTP_PORT": "1025",
    "LOGS_EMAIL_FROM": "logs@svc.test",
    "LOGS_EMAIL_CA_FILE": CA_PATH,
    "LOGS_EGRESS_DENIED_NETWORKS": "[]",
}
# What the TLS front serves, and the service behind each host.
UPSTREAMS = {
    "api.secrets.svc.test": "secrets:8770",
    "console.secrets.svc.test": "secrets:8770",
    "api.logs.svc.test": "logs:8765",
    "console.logs.svc.test": "logs:8765",
    UPSTREAM.removeprefix("https://"): "customer-upstream:8080",
}


def write_secrets_env(lane: ComposeLane) -> None:
    """The app's settings file. Compose checks every service's env_file when
    any service starts, so it is written before the lane's first service."""
    (lane.runtime / "secrets.env").write_text("".join(f"{k}={v}\n" for k, v in secrets_env().items()))


def secrets_env() -> dict[str, str]:
    """The lane's Secrets settings: its own member credential (no AWID
    service token), its key files, its mail, and its hook events to the
    lane's Logs, inside the lane's network."""
    return {
        "APP_ENV": "development",
        "SSL_CERT_FILE": "/run/secrets-keys/hook-ca.pem",
        "SECRETS_DATABASE_URL": "postgresql://secrets:disposable@postgres:5432/secrets",
        "SECRETS_DB_SCHEMA": SECRETS_SCHEMA,
        "SECRETS_APP_ID": APP_ID,
        "SECRETS_PUBLIC_ORIGIN": API,
        "SECRETS_CONSOLE_ORIGIN": CONSOLE,
        "SECRETS_AWID_REGISTRY_URL": REGISTRY,
        "SECRETS_AWID_MEMBER_CREDENTIALS": json.dumps({TEAM_ID: {
            "signing_key_file": "/run/secrets-keys/member.pem",
            "certificate_file": "/run/secrets-keys/member-cert.json"}}),
        "SECRETS_AUTH_CACHE_TTL_SECONDS": "1",
        "SECRETS_CUSTODY_KEY_FILE": "/run/secrets-keys/custody",
        "SECRETS_SECRET_REQUEST_KEY_FILE": "/run/secrets-keys/request",
        "SECRETS_SECRET_PARAMS_KEY_FILE": "/run/secrets-keys/params",
        "SECRETS_RESPONSE_KEY_FILE": "/run/secrets-keys/response",
        "SECRETS_RUNNER_SERVICE_KEY_FILE": "/run/secrets-keys/runner",
        "SECRETS_MANIFEST_REQUEST_KEY_FILE": "/run/secrets-keys/manifest",
        "SECRETS_APP_KEY_FILES": json.dumps({
            "secrets.hook-event-1": "/run/secrets-keys/event",
            "secrets.hook-delivery-1": "/run/secrets-keys/delivery",
            "secrets:grant-read-1": "/run/secrets-keys/grant",
            "secrets:hook-resolve-1": "/run/secrets-keys/resolve"}),
        "SECRETS_EMAIL_SMTP_HOST": "mail.svc.test",
        "SECRETS_EMAIL_SMTP_PORT": "1025",
        "SECRETS_EMAIL_CA_FILE": "/run/secrets-keys/hook-ca.pem",
        "SECRETS_EMAIL_USERNAME": "lane",
        "SECRETS_EMAIL_PASSWORD_FILE": "/run/secrets-keys/smtp",
        "SECRETS_EMAIL_FROM": "secrets@svc.test",
        "SECRETS_EGRESS_DENIED_NETWORKS": "[]",
        "SECRETS_INTERNAL_DESTINATIONS": "allowed",
        "SECRETS_EGRESS_CA_FILE": "/run/secrets-keys/hook-ca.pem",
        "SECRETS_HOOK_INTERNAL_ORIGINS": json.dumps([LOGS_API]),
        "SECRETS_HOOK_INTERNAL_DESTINATIONS": "allowed",
        "SECRETS_HOOK_INTERNAL_CIDR": LANE_SUBNET,
        "SECRETS_HOOK_CA_FILE": "/run/secrets-keys/hook-ca.pem",
    }


class Holder(SharedHolder):
    """The holder's machine, with the holder's own browser for this journey."""

    def __init__(self, lane: ComposeLane, container: str) -> None:
        super().__init__(container, DOMAIN)
        self.lane = lane

    def browser(
        self, page: str, purpose: str, *, page_check: bool, device: str = "authenticator.json",
    ) -> None:
        """The holder's browser, in the holder's network namespace."""
        args = [
            "run", "--rm", f"--network=container:{self.container}",
            "--user", f"{os.getuid()}:{os.getgid()}",
            f"--volume={self.lane.runtime}:/runtime", f"{self.lane.project}-browser",
            "--page", page, "--purpose", purpose, "--device", f"/runtime/holder/{device}",
            "--ca", "/runtime/tls/ca.pem",
        ]
        if not page_check:
            args.append("--no-page")
        result = _docker(*args, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr

    def login(self, console: str, scope: str) -> dict:
        """The answer to the holder's own passkey login on ``console``."""
        result = _docker(
            "run", "--rm", f"--network=container:{self.container}",
            "--user", f"{os.getuid()}:{os.getgid()}",
            f"--volume={self.lane.runtime}:/runtime", f"{self.lane.project}-browser",
            "--page", console + "/login", "--purpose", "login", "--scope", scope,
            "--device", "/runtime/holder/authenticator.json", "--ca", "/runtime/tls/ca.pem",
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return first_json(result.stdout)

    def refused_login(self, console: str, scope: str) -> None:
        """Wait until ``console`` refuses the holder as no member. The app may
        hold evidence of a replaced certificate for at most 60 seconds, and
        each login is two of the twelve the console admits a minute."""
        deadline = time.monotonic() + 120
        while (answer := self.login(console, scope))["status"] == 200:
            assert time.monotonic() < deadline, "the replaced certificate still signs in"
            time.sleep(15)
        assert answer == {"status": 403, "detail": "principal_not_member"}

    def run(self, principal: str, *args: str) -> str:
        """aw as the holder, to its end."""
        child = self.act(principal, *args)
        output = self.answer_passphrases(child)
        child.close()
        assert child.exitstatus == 0, output
        return output

    def run_in_browser(
        self, principal: str, console: str, purpose: str, *args: str,
        device: str = "authenticator.json",
    ) -> str:
        """aw as the holder, completing the handoff page it opens on ``console``."""
        child = self.act(principal, *args)
        output = self.answer_passphrases(child, until=r"Open this page to continue: (\S+)")
        link = re.search(r"Open this page to continue: (\S+)", output)
        assert link is not None, output
        page = link.group(1)
        assert page.startswith(console + "/handoff/"), page
        self.browser(page, purpose, page_check=False, device=device)
        child.expect(pexpect.EOF)
        rest = child.before
        child.close()
        assert child.exitstatus == 0, rest
        return output + rest


@pytest.fixture
def lane(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> Iterator[ComposeLane]:
    if os.environ.get("SECRETS_E2E") != "1":
        pytest.skip("make e2e only")
    project = os.environ.get("NAAPP_E2E_PROJECT", "secrets-owner-acts-e2e")
    runtime = tmp_path_factory.mktemp("owner-acts")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, capture_output=True, check=True).stdout.strip()
    lane = ComposeLane(
        project, runtime, APP_DIR / "docker-compose.e2e.yml",
        env={
            **LOGS_COMPOSE_ENV,
            "NAAPP_E2E_POSTGRES_USER": "secrets",
            "NAAPP_E2E_POSTGRES_DB": "secrets",
            "NAAPP_E2E_POSTGRES_PASSWORD": "disposable",
            "SECRETS_E2E_SOURCE_COMMIT": commit,
        },
        profiles=("logs", "browser", "chromium", "customer"),
        subnet=LANE_SUBNET,
    )
    tls = lane.prepare(
        domain=DOMAIN,
        upstreams=UPSTREAMS,
        app_origins=(API, LOGS_API),
        controller_did="did:key:z6MkpendingpendingpendingpendingpendingpendingP",
        extra_tls_hosts=("mail.svc.test",),
        extra_front_aliases=("signer.svc.test", "human.svc.test"),
    )
    shutil.copyfile(tls.cert_file, runtime / "tls" / "smtp.pem").chmod(0o644)
    shutil.copyfile(tls.key_file, runtime / "tls" / "smtp-key.pem").chmod(0o644)
    write_secrets_env(lane)
    try:
        lane.start_core()
        lane.up("smtp-tls")
        lane.checked(lane.compose("build", "cli", "browser", "chromium"), "build lane clients")
        yield lane
    finally:
        request.node.add_report_section("teardown", "lane Compose", lane.close())


def grant_install_owner(lane: ComposeLane, did_key: str) -> None:
    """The server operator's local bootstrap of the team's first installation
    owner. Membership grants no install authority; only this does."""
    granted = lane.compose("exec", "-T", "aweb", "aweb", "install-admin", "grant", "--team-id", TEAM_ID,
                           "--principal-did", did_key, "--role", "owner")
    assert granted.returncode == 0, granted.stdout + granted.stderr


def admit_app_member(lane: ComposeLane, controller: str, name: str, home: Path) -> tuple[str, Path]:
    """One app as an ordinary member of the team: its key, the owner's
    add-member, and the member's own fetch of its certificate with its key
    alone, as any member does, in an identity home of its own under the
    runtime. Returns the key's PEM and the certificate's path."""
    key = SigningKey.generate()
    from awid.did import did_from_public_key  # noqa: PLC0415

    did = did_from_public_key(bytes(key.verify_key))
    added = aw(controller, "id", "team", "add-member", "--team", TEAM, "--namespace", DOMAIN,
               "--did", did, "--name", name, "--global")
    pem = (
        "-----BEGIN ED25519 PRIVATE KEY-----\n"
        + __import__("base64").b64encode(bytes(key)).decode()
        + "\n-----END ED25519 PRIVATE KEY-----\n"
    )
    home.mkdir(mode=0o700)
    (home / "signing.key").write_text(pem)
    (home / "signing.key").chmod(0o600)
    fetched = aw(controller, "--identity-home", f"/runtime/{home.relative_to(lane.runtime)}", "id", "team",
                 "fetch-cert", "--namespace", DOMAIN, "--team", TEAM, "--cert-id", added["certificate_id"],
                 "--registry", REGISTRY)
    return pem, home / fetched["cert_path"]


def write_service_member(lane: ComposeLane, controller: str) -> None:
    """The app's own team member credential (no AWID service token), and its config."""
    keys = lane.runtime / "keys"
    keys.mkdir(mode=0o755)
    keys.chmod(0o755)  # mkdir's mode is masked by the launching umask
    pem, certificate = admit_app_member(lane, controller, "secrets-service", keys / "service-home")
    lane.redact(pem)
    (keys / "member.pem").write_text(pem)
    shutil.copyfile(certificate, keys / "member-cert.json")
    for name in ("custody", "request", "params", "response", "runner", "smtp", "manifest", "event", "delivery", "grant", "resolve"):
        # The app's shared key-file format: 64 hex characters, each independent.
        value = secrets.token_hex(32)
        lane.redact(value)
        (keys / name).write_text(value + "\n")
    # The lane's CA for hook deliveries and team-server reads.
    shutil.copyfile(lane.runtime / "tls" / "ca.pem", keys / "hook-ca.pem")
    for path in keys.iterdir():
        path.chmod(0o644)
    write_secrets_env(lane)


def write_logs_files(lane: ComposeLane, controller: str) -> None:
    """The lane's Logs files, which the base service names: its member key
    and certificate, its own keys, its mail credentials and the URLs of its
    content and purge-ledger databases, each its own database."""
    files = lane.runtime / "logs"
    files.mkdir(mode=0o755)
    files.chmod(0o755)  # mkdir's mode is masked by the launching umask
    pem, certificate = admit_app_member(lane, controller, "logs-service", lane.runtime / "logs-member")
    lane.redact(pem)
    (files / "awid-member-key").write_text(pem)
    shutil.copyfile(certificate, files / "awid-member-certificate")
    for name in ("logs-chain", "logs-resolve", "logs-grants", "logs-governance"):
        value = secrets.token_hex(32)
        lane.redact(value)
        (files / name).write_text(value + "\n")
    (files / "smtp-username").write_text("logs-service")
    password = secrets.token_urlsafe(16)
    lane.redact(password)
    (files / "smtp-password").write_text(password)
    (files / "database-url").write_text("postgresql://secrets:disposable@postgres:5432/logs\n")
    (files / "purge-ledger-database-url").write_text("postgresql://secrets:disposable@postgres:5432/logsledger\n")
    for path in files.iterdir():
        path.chmod(0o644)


def create_logs_databases(lane: ComposeLane) -> None:
    for database in ("logs", "logsledger"):
        created = lane.compose("exec", "-T", "postgres", "psql", "-U", "secrets", "-d", "secrets", "-v",
                               "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {database}")
        assert created.returncode == 0, created.stdout + created.stderr


def start_apps(lane: ComposeLane, controller: str) -> None:
    """The lane's Secrets and Logs, with their members, files and databases,
    behind the TLS front."""
    write_service_member(lane, controller)
    write_logs_files(lane, controller)
    create_logs_databases(lane)
    lane.up("secrets", "logs")


def install_apps(lane: ComposeLane, controller: str, owner_did_key: str) -> None:
    """The owner publishes the team's coordination server, the operator
    grants the owner installation, and the owner installs Secrets and Logs
    from their internal origins."""
    publish_team_server(controller, TEAM, DOMAIN, AWEB, REGISTRY)
    grant_install_owner(lane, owner_did_key)
    install_app(controller, API)
    install_app(controller, LOGS_API)


def test_the_owner_designates_a_human_holder_registered_on_another_machine(lane: ComposeLane) -> None:
    released_aw = os.environ.get("NAAPP_AW_BINARY")
    assert released_aw, "set NAAPP_AW_BINARY to the pinned Linux aw release"
    aw_binary = Path(released_aw)
    controller = lane.machine("controller", aw_binary=aw_binary)
    installed_version = subprocess.check_output(
        [str(REPO / "scripts/install-aw.sh"), "--version"], text=True,
    ).strip()
    version = lane.checked(_docker("exec", controller, "aw", "version"), "inspect released aw")
    assert version.splitlines()[0] == f"aw {installed_version}", version
    holder = Holder(lane, lane.machine("holder", aw_binary=aw_binary))

    # Setup authority: the namespace, its root human (the holder) and the team.
    prepared = aw(holder.container, "id", "namespace", "prepare-controller", "--domain", DOMAIN, "--registry", REGISTRY)
    lane.publish_awid_txt(DOMAIN, prepared["controller_did"])
    await_awid_txt(holder.container, DOMAIN, prepared["controller_did"])
    child = holder.spawn("--json", "id", "human", "init", "--domain", DOMAIN, "--as-root")
    output = holder.answer_passphrases(child)
    child.close()
    assert child.exitstatus == 0, output
    human = first_json(output)
    principal = human["id"]
    aw(holder.container, "id", "team", "create", "--name", TEAM, "--namespace", DOMAIN, "--registry", REGISTRY)

    # The team key moves to the owner's machine; the holder keeps none.
    team_key = lane.runtime / "holder" / ".awid" / "team-keys" / DOMAIN / f"{TEAM}.key"
    owner_keys = lane.runtime / "controller" / ".awid" / "team-keys" / DOMAIN
    owner_keys.mkdir(parents=True)
    shutil.move(str(team_key), owner_keys / f"{TEAM}.key")

    # The owner's own agent identity: the namespace root issues it on the
    # holder's machine, where its controller key is, straight into the
    # owner's machine's identity folder (a fresh explicit target). Only the
    # owner's own key and certificate land there; the namespace controller key
    # stays where it is.
    owner = aw(holder.container, "--identity-home", identity_folder("controller"), "id", "create", "--name", "owner",
               "--domain", DOMAIN, "--root-parent", "--registry", REGISTRY)
    # Then the owner's membership, and the holder's.
    invite = aw(controller, "id", "team", "invite", "--team", TEAM, "--namespace", DOMAIN, "--member-global")
    # The owner's machine holds no namespace authority, so the membership
    # names the address the namespace root already issued to this identity.
    lane.redact(invite["token"])
    accepted = _docker(
        "exec", "-i", controller, "aw", "--json", "id", "team", "accept-invite", "--token-stdin",
        "--name", "owner", "--global", "--address", owner["address"], input=invite["token"] + "\n",
    )
    lane.checked(accepted, "accept the controller's invite")
    aw(controller, "init", "--url", AWEB, "--name", "owner", "--do-not-touch-agents-md")
    added = aw(controller, "id", "team", "add-member", "--team", TEAM, "--namespace", DOMAIN,
               "--did", human["did_key"], "--did-aw", principal, "--name", "holder", "--global")
    child = holder.act(principal, "--json", "id", "team", "fetch-cert",
                         "--namespace", DOMAIN, "--team", TEAM, "--cert-id", added["certificate_id"], "--registry", REGISTRY)
    output = holder.answer_passphrases(child)
    child.close()
    assert child.exitstatus == 0, output

    # The apps, as their owner installs them, from their internal origins.
    start_apps(lane, controller)
    install_apps(lane, controller, owner["did_key"])
    # The holder's machine installs the app host-only: plugin install is not a
    # human act, and with no agent identity here nothing is registered. Then
    # the human connects to this server, so its --human commands reach it.
    aw(holder.container, "plugin", "install", API)
    child = holder.act(principal, "init", "--url", AWEB)
    output = holder.answer_passphrases(child)
    child.close()
    assert child.exitstatus == 0, output

    # The holder registers a passkey on their own machine and exports it.
    child = holder.act(principal, "app", "credential", "register",
                         APP_ID, "--role", ROLE, "--out", "/runtime/holder/export.json")
    output = holder.answer_passphrases(child, until=r"Open this page to continue: (\S+)")
    page = re.search(r"Open this page to continue: (\S+)", output).group(1)
    assert page.startswith(CONSOLE + "/handoff/"), page
    holder.browser(page, "credential", page_check=False)
    child.expect(pexpect.EOF)
    rest = child.before
    child.close()
    assert child.exitstatus == 0, rest
    fingerprint = re.search(r"Fingerprint: ([0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{4})", output + rest).group(1)
    exported = json.loads((lane.runtime / "holder" / "export.json").read_text())

    # The designation's governance needs its audit target: the owner
    # activates the apps' hook bindings and waits until they are in effect.
    hooks = activate_hooks(controller, {"secrets": APP_ID, "logs": LOGS_APP_ID})
    installed = hooks["active"]["secrets/production/post-governance-hook"]
    holding = Holding(app_id=APP_ID, installation_id=installed["installation_id"],
                      generation=installed["generation"], team_id=TEAM_ID, role=ROLE, principal=principal)
    designation = Designation(holding=holding, audience=CONSOLE,
                              credential_sha256=credential_sha256(exported["credential"]), contact=CONTACT)
    query = partial(lane.query, "secrets", "secrets")
    baseline = designation_snapshot(query, holding, schema=SECRETS_SCHEMA)

    # The owner signs only for that exact credential: the fingerprint matches.
    shutil.copyfile(lane.runtime / "holder" / "export.json", lane.runtime / "controller" / "export.json")
    command = designate_command(APP_ID, ROLE, principal, "/runtime/controller/export.json", CONTACT)
    result = _docker("exec", controller, *command, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fingerprint in result.stderr, result.stderr
    assert "human-role.designate" in result.stdout
    designated = designation_snapshot(query, holding, schema=SECRETS_SCHEMA)
    assert_designated(baseline, designated, designation)

    # A second designation of the same holding, a fresh act, is refused by
    # the app, and leaves its records as the first one left them.
    code = aw_refused(controller, *command[1:])
    assert code == "owner_required", code
    assert_unchanged(designated, designation_snapshot(query, holding, schema=SECRETS_SCHEMA))

    # The holder holds the role: a session handoff starts for it.
    child = holder.act(principal, "--json", "app", "human", APP_ID, "--help")
    listed = holder.answer_passphrases(child)
    assert "confirm" in listed and "draft-change" in listed, listed

    scopes = signs_in_again_after_a_reissue(lane, controller, holder, human, hooks)
    value = start_upstream(lane)
    secret_id = confirm_native_bearer(
        lane, controller, holder.container, CONSOLE, scopes[APP_ID],
        installed["installation_id"], CONTACT, value,
    )
    agent, requester = create_agent(
        lane, controller, holder.container,
        binary=aw_binary, team=TEAM, domain=DOMAIN, registry=REGISTRY, aweb=AWEB,
    )
    exercise_customer_use(
        lane,
        controller=controller, holder=holder.container,
        agent=agent, agent_principal=requester, owner_principal=principal,
        owner_scope=scopes[APP_ID], reader_scope=scopes[LOGS_APP_ID],
        contact=CONTACT, api=API, console=CONSOLE, logs=LOGS_CONSOLE,
        installation_id=installed["installation_id"], secret_id=secret_id, value=value,
    )


def signs_in_again_after_a_reissue(
    lane: ComposeLane, controller: str, holder: Holder, human: dict, hooks: dict
) -> dict[str, str]:
    """A renewed certificate and credential registration restore passkey login
    for the original Logs reader and Secrets owner holdings."""
    principal = human["id"]
    aw(holder.container, "plugin", "install", LOGS_API)
    holder.run_in_browser(principal, LOGS_CONSOLE, "credential", "app", "credential", "register", LOGS_APP_ID,
                          "--role", READER_ROLE, "--out", "/runtime/holder/logs-export.json")
    shutil.copyfile(lane.runtime / "holder" / "logs-export.json", lane.runtime / "controller" / "logs-export.json")
    designated = _docker("exec", controller, "aw", "app", "role", "designate", LOGS_APP_ID, "--role", READER_ROLE,
                         "--principal", principal, "--credential", "/runtime/controller/logs-export.json", "--yes",
                         timeout=300)
    assert designated.returncode == 0, designated.stdout + designated.stderr
    apps = (
        (LOGS_APP_ID, LOGS_CONSOLE, READER_ROLE, "logs"),
        (APP_ID, CONSOLE, ROLE, "secrets"),
    )
    scopes = {
        app_id: holding_scope(
            hooks["active"][f"{service}/production/post-governance-hook"]["installation_id"],
            TEAM_ID, role, principal,
        )
        for app_id, _, role, service in apps
    }
    for app_id, console, _, _ in apps:
        answer = holder.login(console, scopes[app_id])
        assert answer["status"] == 200, (app_id, answer)

    reissued = aw(controller, "id", "team", "reissue-cert", "holder", "--team", TEAM, "--namespace", DOMAIN,
                  "--did", human["did_key"], "--did-aw", principal, "--global", "--registry", REGISTRY)
    assert reissued["old_certificate_id"] != reissued["new_certificate_id"]
    for app_id, console, _, _ in apps:
        holder.refused_login(console, scopes[app_id])

    holder.run(principal, "--json", "id", "team", "fetch-cert", "--namespace", DOMAIN, "--team", TEAM,
               "--cert-id", reissued["new_certificate_id"], "--registry", REGISTRY, "--force")
    for app_id, console, role, database in apps:
        # Registration refreshes the certificate for the existing holding.
        # The new passkey is never designated or used to sign back in.
        holder.run_in_browser(
            principal, console, "credential", "app", "credential", "register", app_id,
            "--role", role,
            device="recovery-authenticator.json",
        )
        answer = holder.login(console, scopes[app_id])
        schema = "public" if database == "logs" else SECRETS_SCHEMA
        certificate_id = sql_literal(reissued["new_certificate_id"])
        scope = sql_literal(scopes[app_id])
        renewed = lane.query(
            "secrets", database,
            f"SELECT convert_from(certificate, 'UTF8')::jsonb->>'certificate_id' = {certificate_id}"
            f" FROM {schema}.role_holdings WHERE b3_scope = {scope} AND ended_in IS NULL",
        ) == "t"
        assert answer["status"] == 200 and renewed, (app_id, answer, renewed)
    return scopes
