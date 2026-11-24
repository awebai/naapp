"""Private Compose network for journeys against published AWID and aweb images."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from aweb_naapp_testing.network import Certificates, make_certificates

TEAM_SERVER_HOST = "aweb.svc.test"
TEAM_SERVER_ORIGIN = f"https://{TEAM_SERVER_HOST}"
REGISTRY_ORIGIN = "http://awid.svc.test:8010"
DEFAULT_SUBNET = "172.31.240.0/24"
CA_PATH = "/runtime/tls/ca.pem"
BASE_COMPOSE = Path(__file__).with_name("lane.compose.yaml")


def docker(
    *args: str,
    timeout: int = 120,
    input: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        input=input,
        cwd=cwd,
        env=env,
        check=False,
    )


def machine_dir(name: str) -> str:
    return f"/runtime/{name}"


def identity_folder(name: str) -> str:
    return f"{machine_dir(name)}/.aw"


def front_caddyfile(upstreams: Mapping[str, str], cert: str, key: str) -> str:
    """Terminate TLS for the lane's routed hosts and preserve the public Host."""
    return "{\n  auto_https off\n}\n" + "".join(
        f"{host}:443 {{\n  tls /tls/{cert} /tls/{key}\n  reverse_proxy {target}\n}}\n"
        for host, target in upstreams.items()
    )


def read_only_psql(user: str, database: str, select: str) -> list[str]:
    """Run one assertion query in a repeatable-read, read-only transaction."""
    return [
        "psql",
        "-U",
        user,
        "-d",
        database,
        "-At",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "-c",
        select,
        "-c",
        "COMMIT",
    ]


def await_awid_txt(machine: str, domain: str, controller_did: str) -> None:
    """Wait until the caller container's resolver sees the reloaded zone."""
    for _ in range(30):
        answer = docker("exec", machine, "nslookup", "-type=txt", f"_awid.{domain}")
        if answer.returncode == 0 and controller_did in answer.stdout:
            return
        time.sleep(0.5)
    raise AssertionError(f"_awid.{domain} did not publish the expected controller")


def _directory(path: Path) -> None:
    path.mkdir()
    path.chmod(0o755)


def _file(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o644)


@dataclass
class ComposeLane:
    """The shared services, with app variants declared by the caller's Compose file.

    The caller writes its app env files and keys before starting the stack.
    Construct a separate project and runtime directory for each lane.
    """

    project: str
    runtime: Path
    compose_file: Path
    env: dict[str, str] = field(default_factory=dict, repr=False)
    profiles: tuple[str, ...] = ()
    subnet: str = DEFAULT_SUBNET
    _machines: list[str] = field(default_factory=list, init=False)
    _secret_values: set[str] = field(default_factory=set, init=False, repr=False)
    _dns_serial: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.runtime = self.runtime.resolve()
        self.compose_file = self.compose_file.resolve()
        self.env = {**os.environ, **self.env}
        self.env.update(
            NAAPP_E2E_BASE_COMPOSE=str(BASE_COMPOSE),
            NAAPP_E2E_RUNTIME=str(self.runtime),
            NAAPP_E2E_SUBNET=self.subnet,
        )
        user = self.env.setdefault("NAAPP_E2E_POSTGRES_USER", "postgres")
        password = self.env.setdefault("NAAPP_E2E_POSTGRES_PASSWORD", "disposable")
        database = self.env.setdefault("NAAPP_E2E_POSTGRES_DB", user)
        self.env["NAAPP_E2E_DATABASE_URL"] = (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@postgres:5432/{quote(database, safe='')}"
        )
        token = self.env.get("NAAPP_E2E_AWID_SERVICE_TOKEN") or secrets.token_hex(32)
        self.env["NAAPP_E2E_AWID_SERVICE_TOKEN"] = token
        self.redact(password, quote(password, safe=""), token)

    def prepare(
        self,
        *,
        domain: str,
        upstreams: Mapping[str, str],
        app_origins: Iterable[str],
        controller_did: str,
        extra_tls_hosts: Iterable[str] = (),
        extra_front_aliases: Iterable[str] = (),
    ) -> Certificates:
        """Write the zone, Caddy TLS and Compose network before starting Docker."""
        self.env["NAAPP_E2E_APP_ORIGINS"] = json.dumps(list(app_origins))
        self.runtime.chmod(0o755)
        _directory(self.runtime / "dns")
        _file(
            self.runtime / "dns" / "Corefile",
            f"{domain}:53 {{\n  errors\n  file /zones/{domain}.zone {domain} {{ reload 1s }}\n}}\n"
            ".:53 {\n  errors\n  forward . 127.0.0.11\n}\n",
        )
        self.publish_awid_txt(domain, controller_did)

        routes = {**upstreams, TEAM_SERVER_HOST: "aweb:8000"}
        _directory(self.runtime / "tls")
        tls = make_certificates(self.runtime / "tls", [*routes, *extra_tls_hosts], [])
        for path in (tls.ca_file, tls.cert_file, tls.key_file):
            path.chmod(0o644)
        self.redact(tls.key_file.read_text())
        _directory(self.runtime / "front")
        _file(
            self.runtime / "front" / "Caddyfile",
            front_caddyfile(routes, tls.cert_file.name, tls.key_file.name),
        )
        _file(
            self.runtime / "lane.override.json",
            json.dumps(
                {
                    "services": {
                        "front": {
                            "networks": {
                                "default": {
                                    "aliases": [*routes, *extra_front_aliases],
                                }
                            }
                        }
                    },
                    "networks": {
                        "default": {
                            "internal": True,
                            "ipam": {"config": [{"subnet": self.subnet}]},
                        }
                    },
                }
            ),
        )
        return tls

    def publish_awid_txt(
        self, domain: str, controller_did: str, *, registry: str = REGISTRY_ORIGIN
    ) -> None:
        self._dns_serial = max(int(time.time()), self._dns_serial + 1)
        zone = self.runtime / "dns" / f"{domain}.zone"
        temporary = zone.with_suffix(".tmp")
        _file(
            temporary,
            f"$ORIGIN {domain}.\n$TTL 1\n"
            f"@ IN SOA ns.{domain}. hostmaster.{domain}. ({self._dns_serial} 1 1 1 1)\n"
            f"@ IN NS ns.{domain}.\nns IN A 127.0.0.1\n"
            f'_awid IN TXT "awid=v1; controller={controller_did}; registry={registry};"\n',
        )
        temporary.replace(zone)

    def compose(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        profiles = [arg for profile in self.profiles for arg in ("--profile", profile)]
        return docker(
            "compose",
            "-p",
            self.project,
            "-f",
            str(self.compose_file),
            "-f",
            str(self.runtime / "lane.override.json"),
            *profiles,
            *args,
            cwd=self.compose_file.parent,
            env=self.env,
            timeout=timeout,
        )

    def redact(self, *values: str) -> None:
        """Register values the caller supplies to the disposable services."""
        for value in values:
            value = value.strip()
            if value:
                self._secret_values.add(value)
            if "PRIVATE KEY-----" in value:
                self._secret_values.update(
                    line for line in value.splitlines() if line and not line.startswith("-----")
                )

    def _safe(self, text: str) -> str:
        for value in sorted(self._secret_values, key=len, reverse=True):
            text = text.replace(value, "[redacted]").replace(json.dumps(value)[1:-1], "[redacted]")
        return text

    def checked(self, result: subprocess.CompletedProcess[str], operation: str) -> str:
        if result.returncode != 0:
            raise AssertionError(f"{operation}: {self._safe(result.stdout + result.stderr)}")
        return result.stdout.strip()

    def up(self, *services: str) -> None:
        result = self.compose("up", "-d", "--build", "--wait", *services)
        if result.returncode != 0:
            logs = self.compose("logs", "--no-color", "--tail", "60", *services)
            raise AssertionError(
                self._safe(result.stderr[-4000:] + "\n" + logs.stdout + logs.stderr)
            )

    def start_core(self) -> None:
        """Give consumers the CoreDNS address before they start."""
        self.up("dns")
        container = self.checked(self.compose("ps", "-q", "dns"), "find lane DNS")
        assert container, "lane DNS has no container"
        address = self.checked(
            docker(
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container,
            ),
            "inspect lane DNS",
        )
        assert address, "lane DNS has no address"
        self.env["NAAPP_E2E_DNS_IP"] = address
        self.up("postgres", "redis", "awid", "aweb", "front")

    def machine(self, name: str, *, aw_binary: Path | None = None) -> str:
        """Start one person's CLI container and optionally mount a released Linux aw."""
        container = f"{self.project}-{name}"
        (self.runtime / name).mkdir(mode=0o700)
        uid, gid = os.getuid(), os.getgid()
        accounts = self.runtime / "accounts"
        if not accounts.exists():
            _directory(accounts)
            _file(
                accounts / "passwd",
                f"root:x:0:0:root:/root:/bin/sh\nlane:x:{uid}:{gid}:lane:/runtime:/bin/sh\n",
            )
            _file(accounts / "group", f"root:x:0:\nlane:x:{gid}:\n")
        binary = (
            ["-v", f"{aw_binary.resolve(strict=True)}:/usr/local/bin/aw:ro"] if aw_binary else []
        )
        self.checked(
            self.compose(
                "run",
                "-d",
                "--name",
                container,
                "--user",
                f"{uid}:{gid}",
                "-v",
                f"{accounts / 'passwd'}:/etc/passwd:ro",
                "-v",
                f"{accounts / 'group'}:/etc/group:ro",
                *binary,
                "-e",
                f"HOME={machine_dir(name)}",
                "-w",
                machine_dir(name),
                "cli",
            ),
            "start lane CLI",
        )
        self._machines.append(container)
        return container

    def query(self, user: str, database: str, select: str) -> str:
        return self.checked(
            self.compose("exec", "-T", "postgres", *read_only_psql(user, database, select)),
            "read lane PostgreSQL",
        )

    def close(self) -> str:
        """Retain redacted Compose logs and remove the project's containers."""
        try:
            logs = self.compose("logs", "--no-color", "--timestamps")
            captured = self._safe(logs.stdout + logs.stderr)
            (self.runtime / "compose.log").write_text(captured)
        finally:
            try:
                if self._machines:
                    self.checked(docker("rm", "-f", *self._machines), "remove lane CLI containers")
            finally:
                self.checked(self.compose("down", "-v", "--remove-orphans"), "stop lane Compose")
        return captured
