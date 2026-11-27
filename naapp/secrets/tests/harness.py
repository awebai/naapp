"""The S19 upstream served over TLS where the real egress guard reaches it.

The upstream listens on this host's own non-loopback address and is reached
as an internal destination with that address's exact CIDR, under
``internal_destinations: allowed``, by a name a local DNS server answers. The
credentials are generated when the test runs.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aweb_naapp.egress import (
    AddressPolicy,
    Destination,
    DnsResolver,
    Egress,
    InternalDestinations,
    parse_destination,
)
from aweb_naapp_testing.network import DnsServer, local_address, make_certificates
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from upstream.app import UpstreamCredentials, create_upstream
from upstream.serve import serving

NAME = "upstream.secrets.test"
OTHER = "other.secrets.test"


def oauth_certificate(client_id: str) -> tuple[x509.Certificate, dict[str, str]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "OAuth test client")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate, {
        "client_id": client_id,
        "certificate_pem": certificate.public_bytes(serialization.Encoding.PEM).decode(),
        "private_key_pem": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }


def _decode_segment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


@dataclass(repr=False)
class CertificateTokenEndpoint:
    """Local token server's registered client and replay check."""

    client_id: str
    certificates: tuple[x509.Certificate, ...]
    audience: str | None = None
    algorithm: str = "PS256"
    key_id: str | None = None
    scope: str = "https://graph.microsoft.com/.default"
    used: set[str] = field(default_factory=set)

    def __call__(self, request: Request, fields: dict[str, list[str]]) -> bool:
        # Microsoft Entra's documented client-credentials assertion form.
        expected = {
            "grant_type": ["client_credentials"],
            "client_id": [self.client_id],
            "client_assertion_type": ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"],
        }
        if self.scope:
            expected["scope"] = [self.scope]
        if (
            set(fields) != set(expected) | {"client_assertion"}
            or any(fields[name] != value for name, value in expected.items())
            or len(fields["client_assertion"]) != 1
            or "authorization" in request.headers
            or request.headers.get("content-type") != "application/x-www-form-urlencoded"
        ):
            return False
        try:
            header, payload, signature = fields["client_assertion"][0].split(".")
            protected = json.loads(_decode_segment(header))
            claims = json.loads(_decode_segment(payload))
            now = int(datetime.now(UTC).timestamp())
            if not (
                claims["iss"] == claims["sub"] == self.client_id
                and claims["aud"] == (self.audience or str(request.url))
                and claims["iat"] == claims["nbf"] <= now < claims["exp"]
                and 0 < claims["exp"] - claims["iat"] <= 300
                and claims["jti"]
                and claims["jti"] not in self.used
            ):
                return False
            for cert in self.certificates:
                thumbprint = base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA256()))
                expected_header = {
                    "typ": "JWT",
                    "alg": self.algorithm,
                    "x5t#S256": thumbprint.rstrip(b"=").decode(),
                }
                if self.key_id is not None:
                    expected_header["kid"] = self.key_id
                if protected != expected_header:
                    continue
                key = cert.public_key()
                assert isinstance(key, rsa.RSAPublicKey)
                signature_padding = (
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
                    if self.algorithm == "PS256"
                    else padding.PKCS1v15()
                )
                key.verify(
                    _decode_segment(signature),
                    f"{header}.{payload}".encode(),
                    signature_padding,
                    hashes.SHA256(),
                )
                self.used.add(claims["jti"])
                return True
        except (ValueError, KeyError, TypeError, InvalidSignature):
            return False
        return False


@dataclass
class Upstream:
    creds: UpstreamCredentials
    egress: Egress
    port: int
    host: str
    app: FastAPI
    # The Host header of every request the upstream received.
    requests: list[str]
    # The path of every request the upstream received.
    paths: list[str]

    def issued(self) -> int:
        """How many access tokens the upstream's token endpoint has issued."""
        return int(self.app.state.stats["issued"])

    def url(self, path: str, name: str = NAME) -> str:
        return f"https://{name}:{self.port}{path}"

    def destination(self, name: str = NAME, **entry: object) -> Destination:
        return parse_destination(
            {
                "host": name,
                "port": self.port,
                "path": "/**",
                "methods": ["GET", "POST"],
                "network": "internal",
                "cidr": f"{self.host}/32",
                **entry,
            },
            internal_destinations=InternalDestinations.ALLOWED,
        )


def credentials() -> UpstreamCredentials:
    return UpstreamCredentials(
        bearer=secrets.token_urlsafe(24),
        basic_username="svc-" + secrets.token_hex(4),
        basic_password=secrets.token_urlsafe(24),
        header_name="X-Upstream-Key",
        header_prefix="Key ",
        header_value=secrets.token_urlsafe(24),
        query_param="key",
        query_value=secrets.token_urlsafe(24),
        oauth_client_id="client-" + secrets.token_hex(4),
        oauth_client_secret=secrets.token_urlsafe(24),
        oauth_scope="read:things",
        sigv4_access_key_id="AKIA" + secrets.token_hex(8).upper(),
        sigv4_secret_access_key=secrets.token_urlsafe(30),
        sigv4_region="eu-west-1",
        sigv4_service="execute-api",
    )


@asynccontextmanager
async def running_upstream(
    tmp_path: Path, *, certificate_endpoint: CertificateTokenEndpoint | None = None, **options: Any
) -> AsyncIterator[Upstream]:
    """The S19 upstream over TLS, and an egress that reaches it by NAME (and
    by OTHER, which is never a destination). ``options`` go to create_upstream."""
    host = local_address()
    certificates = make_certificates(tmp_path, [NAME, OTHER], [])
    creds = credentials()
    if certificate_endpoint is not None:
        options["verify_oauth_assertion"] = certificate_endpoint
        creds = replace(creds, oauth_scope=certificate_endpoint.scope)
    app = create_upstream(creds, **options)
    requests: list[str] = []
    paths: list[str] = []

    @app.middleware("http")
    async def record(request, call_next):
        requests.append(request.headers.get("host", ""))
        paths.append(request.url.path)
        return await call_next(request)

    dns = DnsServer(records={(NAME, "A"): [host], (OTHER, "A"): [host]})
    await dns.start()
    try:
        async with serving(app, certificates, host) as port:
            egress = Egress(
                addresses=AddressPolicy(internal_destinations=InternalDestinations.ALLOWED),
                resolver=DnsResolver(nameservers=["127.0.0.1"], port=dns.port),
                user_agent="aweb-secrets/test",
                ca_file=certificates.ca_file,
            )
            yield Upstream(creds, egress, port, host, app, requests, paths)
    finally:
        dns.close()
