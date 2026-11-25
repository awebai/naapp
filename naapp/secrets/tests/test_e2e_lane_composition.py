"""The Secrets e2e lane's composition, read as files: what both lanes run
before any container starts (docker-compose.e2e.yml and the lane fixtures)."""

import json
import re
from subprocess import CompletedProcess
from urllib.parse import urlsplit

import dns.asyncresolver
import pytest
import yaml
from aweb_naapp_testing.compose_lane import (
    BASE_COMPOSE,
    ComposeLane,
    front_caddyfile,
    identity_folder,
    machine_dir,
)
from aweb_naapp_testing.network import DnsServer
from awid.did import did_from_public_key
from awid.dns_verify import discover_registry_override
from cryptography import x509
from nacl.signing import SigningKey

from tests.test_e2e_owner_acts import (
    API,
    APP_DIR,
    AWEB,
    CONSOLE,
    DOMAIN,
    LANE_SUBNET,
    LOGS_API,
    LOGS_COMPOSE_ENV,
    LOGS_CONSOLE,
    REGISTRY,
    UPSTREAMS,
    secrets_env,
    write_service_member,
)

LANE = yaml.safe_load((APP_DIR / "docker-compose.e2e.yml").read_text())
COMMON = yaml.safe_load(BASE_COMPOSE.read_text())
LOGS_BASE = (APP_DIR.parent / "logs" / "compose.logs.yaml").read_text()
REQUIRED = re.compile(r"\$\{([A-Z0-9_]+):\?")
# Settings only the lane's Logs has, which never belong in the deployment
# base: the lane's http registry is accepted only in development. Every other
# setting comes through the base.
LANE_ONLY_LOGS_SETTINGS = frozenset({"APP_ENV"})


def test_every_setting_the_logs_base_requires_is_in_the_lanes_compose_environment():
    # Compose interpolates every service of the file it loads, whatever the
    # profile: one missing setting fails every command of both lanes.
    required = set(REQUIRED.findall(LOGS_BASE))
    assert required
    assert required <= set(LOGS_COMPOSE_ENV), sorted(required - set(LOGS_COMPOSE_ENV))


def test_the_lanes_logs_is_the_deployments_base_with_its_files_read_only():
    logs = LANE["services"]["logs"]
    assert logs["extends"] == {"file": "../logs/compose.logs.yaml", "service": "logs"}
    assert logs["profiles"] == ["logs"]
    assert "${NAAPP_E2E_RUNTIME:?}/logs:/run/secrets/logs:ro" in logs["volumes"]
    # Every deployment setting reaches it through the base, as in a deployment.
    assert set(logs.get("environment", {})) == LANE_ONLY_LOGS_SETTINGS
    assert "env_file" not in logs
    assert logs["depends_on"]["postgres"] == {"condition": "service_healthy"}
    assert "logs" in LANE["services"]["smtp-tls"]["profiles"]


def test_the_lanes_logs_uses_the_lanes_registry_and_mail_and_an_id_of_its_own():
    env = LOGS_COMPOSE_ENV
    assert env["AWID_REGISTRY_URL"] == REGISTRY
    assert env["LOGS_EMAIL_SMTP_HOST"] == "mail.svc.test" and env["LOGS_EMAIL_CA_FILE"] == "/runtime/tls/ca.pem"
    # Only the hosted origin may serve the hosted id.
    assert env["LOGS_APP_ID"] != "logs"
    assert (env["LOGS_PUBLIC_ORIGIN"], env["LOGS_CONSOLE_ORIGIN"]) == (LOGS_API, LOGS_CONSOLE)
    assert json.loads(env["LOGS_EGRESS_DENIED_NETWORKS"]) == []


def test_secrets_approval_mail_uses_the_authenticated_tls_mailbox():
    env = secrets_env()
    mail = LANE["services"]["smtp-tls"]
    assert env["SECRETS_EMAIL_SMTP_HOST"] in mail["networks"]["default"]["aliases"]
    # The mailbox presents the lane's certificate: the lane's CA, as hooks trust.
    assert env["SECRETS_EMAIL_CA_FILE"] == env["SECRETS_HOOK_CA_FILE"]
    assert "smtp-tls" in LANE["services"]["secrets"]["depends_on"]
    assert mail["environment"]["MP_SMTP_REQUIRE_STARTTLS"] == "true"
    assert mail["environment"]["MP_SMTP_AUTH_ACCEPT_ANY"] == "true"


@pytest.fixture
def prepared_lane(tmp_path):
    lane = ComposeLane(
        "test-lane", tmp_path, APP_DIR / "docker-compose.e2e.yml",
        env={"NAAPP_E2E_AWID_SERVICE_TOKEN": "unused-placeholder"},
        subnet=LANE_SUBNET,
    )
    lane.prepare(
        domain=DOMAIN,
        upstreams=UPSTREAMS,
        app_origins=(API, LOGS_API),
        controller_did="did:key:placeholder",
    )
    return lane


def test_secrets_key_mount_contains_only_the_deployed_files(prepared_lane, monkeypatch):
    def issue_member(lane, controller, name, home):
        home.mkdir()
        certificate = home / "certificate"
        certificate.write_text("test certificate")
        return "test key", certificate

    monkeypatch.setattr("tests.test_e2e_owner_acts.admit_app_member", issue_member)
    write_service_member(prepared_lane, "controller")
    keys = prepared_lane.runtime / "keys"
    assert (keys / "member-cert.json").read_text() == "test certificate"
    assert all(path.is_file() for path in keys.iterdir())


def test_the_front_serves_logs_and_aweb_installs_it_from_its_internal_origin(prepared_lane):
    override = json.loads((prepared_lane.runtime / "lane.override.json").read_text())
    aliases = override["services"]["front"]["networks"]["default"]["aliases"]
    assert {"api.logs.svc.test", "console.logs.svc.test"} <= set(aliases)
    assert json.loads(prepared_lane.env["NAAPP_E2E_APP_ORIGINS"]) == [API, LOGS_API]
    assert UPSTREAMS["api.logs.svc.test"] == UPSTREAMS["console.logs.svc.test"] == "logs:8765"


def test_the_team_server_origin_resolves_only_to_the_lanes_tls_front(prepared_lane):
    aweb = COMMON["services"]["aweb"]
    front = COMMON["services"]["front"]
    origin = urlsplit(AWEB)
    override = json.loads((prepared_lane.runtime / "lane.override.json").read_text())
    assert origin.scheme == "https" and origin.port is None
    assert aweb["environment"]["AWEB_PUBLIC_ORIGIN"] == AWEB
    assert origin.hostname in override["services"]["front"]["networks"]["default"]["aliases"]
    assert origin.hostname not in aweb.get("networks", {}).get("default", {}).get("aliases", [])
    assert f"reverse_proxy aweb:{aweb['environment']['AWEB_PORT']}" in (
        prepared_lane.runtime / "front" / "Caddyfile"
    ).read_text()
    assert set(front["depends_on"]) == {"aweb"}
    assert COMMON["services"]["cli"]["environment"]["SSL_CERT_FILE"] == "/runtime/tls/ca.pem"
    # Secrets reads the team's server through its guard, which trusts the
    # hook CA; the process trust store does not hold the lane's CA.
    assert json.loads(secrets_env()["SECRETS_TEAM_SERVER_INTERNAL_ORIGINS"]) == [AWEB]
    assert "SSL_CERT_FILE" not in secrets_env()


def test_the_front_routes_each_host_to_its_service_with_the_lanes_certificate():
    text = front_caddyfile({"console.secrets.svc.test": "secrets:8770", "api.logs.svc.test": "logs:8765"},
                           "c.pem", "k.pem")
    assert text == ("{\n  auto_https off\n}\n"
                    "console.secrets.svc.test:443 {\n  tls /tls/c.pem /tls/k.pem\n  reverse_proxy secrets:8770\n}\n"
                    "api.logs.svc.test:443 {\n  tls /tls/c.pem /tls/k.pem\n  reverse_proxy logs:8765\n}\n")


def test_secrets_sends_its_governance_and_use_events_to_the_lanes_logs():
    env = secrets_env()
    assert json.loads(env["SECRETS_HOOK_INTERNAL_ORIGINS"]) == [LOGS_API]
    # The three settings together, inside the lane's network.
    assert env["SECRETS_HOOK_INTERNAL_DESTINATIONS"] == "allowed"
    assert env["SECRETS_HOOK_INTERNAL_CIDR"] == LANE_SUBNET
    assert env["SECRETS_HOOK_CA_FILE"] == "/run/secrets-keys/hook-ca.pem"
    assert env["SECRETS_CONSOLE_ORIGIN"] == CONSOLE


def test_the_lane_subnet_is_the_one_the_compose_file_fixes(prepared_lane):
    config = json.loads((prepared_lane.runtime / "lane.override.json").read_text())
    network = config["networks"]["default"]
    assert network["internal"] is True
    assert network["ipam"]["config"] == [{"subnet": LANE_SUBNET}]
    assert prepared_lane.env["NAAPP_E2E_SUBNET"] == LANE_SUBNET


def test_the_owners_identity_is_issued_into_the_folder_the_controller_machine_selects():
    # The namespace root issues the owner's identity on the holder's machine,
    # where its controller key is, into the controller machine's own folder:
    # aw there selects its working directory's .aw.
    assert machine_dir("controller") == "/runtime/controller"
    assert identity_folder("controller") == machine_dir("controller") + "/.aw"
    assert identity_folder("controller") != identity_folder("holder")


def test_variants_have_independent_routes_on_the_same_lane(tmp_path):
    lane = ComposeLane("variant-lane", tmp_path, APP_DIR / "docker-compose.e2e.yml")
    routes = {
        "library-token.svc.test": "library-token:8000",
        "library-private.svc.test": "library-private:8000",
    }
    origins = [f"https://{host}" for host in routes]
    tls = lane.prepare(
        domain=DOMAIN,
        upstreams=routes,
        app_origins=origins,
        controller_did="did:key:placeholder",
    )

    certificate = x509.load_pem_x509_certificate(tls.cert_file.read_bytes())
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(names.get_values_for_type(x509.DNSName)) == {*routes, "aweb.svc.test"}
    config = json.loads((tmp_path / "lane.override.json").read_text())
    aliases = config["services"]["front"]["networks"]["default"]["aliases"]
    assert set(aliases) == {*routes, "aweb.svc.test"}
    caddyfile = (tmp_path / "front" / "Caddyfile").read_text()
    for host, target in routes.items():
        assert f"{host}:443 {{\n  tls " in caddyfile
        assert f"  reverse_proxy {target}\n" in caddyfile
    assert json.loads(lane.env["NAAPP_E2E_APP_ORIGINS"]) == origins


def test_lane_command_failure_redacts_configured_tokens_and_keys(prepared_lane):
    private_line = "disposable-test-key-content"
    key = f"-----BEGIN PRIVATE KEY-----\n{private_line}\n-----END PRIVATE KEY-----"
    prepared_lane.redact(key)
    result = CompletedProcess(
        ["docker"], 1, stdout="registry token: unused-placeholder\n",
        stderr=f"separate key line: {private_line}\n{key}\n",
    )

    with pytest.raises(AssertionError) as error:
        prepared_lane.checked(result, "start registry")
    assert str(error.value) == (
        "start registry: registry token: [redacted]\n"
        "separate key line: [redacted]\n[redacted]\n"
    )


@pytest.mark.parametrize(
    "app_env",
    [secrets_env, lambda: LANE["services"]["logs"]["environment"]],
    ids=["secrets", "logs"],
)
async def test_the_lanes_apps_accept_the_registry_the_lanes_dns_names(
    app_env, prepared_lane, monkeypatch
):
    # The app asks which registry the team's domain names before it reads the
    # team's server; the lane's DNS names its http registry, as every lane
    # service runs it.
    controller = did_from_public_key(bytes(SigningKey.generate().verify_key))
    prepared_lane.publish_awid_txt(DOMAIN, controller)
    zone = (prepared_lane.runtime / "dns" / f"{DOMAIN}.zone").read_text()
    record = re.search(r'TXT (".*")', zone).group(1)
    server = DnsServer(records={(f"_awid.{DOMAIN}", "TXT"): [record]})
    await server.start()
    try:
        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = ["127.0.0.1"]
        resolver.port = server.port
        monkeypatch.setattr(dns.asyncresolver, "default_resolver", resolver)
        for name in ("APP_ENV", "ENVIRONMENT"):
            monkeypatch.delenv(name, raising=False)
        for name, value in app_env().items():
            monkeypatch.setenv(name, value)
        assert await discover_registry_override(DOMAIN) == REGISTRY
    finally:
        server.close()
