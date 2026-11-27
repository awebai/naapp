"""Sign OAuth client authentication assertions with a native certificate key."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from aweb_secrets_values import ValueInvalid
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aweb_secrets.secret import ASSERTION_ALGORITHMS

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
MAX_PEM_BYTES = 16_384
_ASSERTION_SECONDS = 300


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _segment(value: Mapping[str, str | int]) -> str:
    return _base64url(json.dumps(value, separators=(",", ":")).encode())


@dataclass(frozen=True, repr=False)
class CertificateCredential:
    certificate: x509.Certificate
    key: rsa.RSAPrivateKey
    key_id: str | None

    @classmethod
    def load(cls, value: Mapping[str, str]) -> CertificateCredential:
        """Validate the submitted key pair before sealing or signing."""
        try:
            cert_pem, key_pem = value["certificate_pem"], value["private_key_pem"]
            if max(len(cert_pem), len(key_pem)) > MAX_PEM_BYTES:
                raise ValueError
            [certificate] = x509.load_pem_x509_certificates(cert_pem.encode("ascii"))
            key = serialization.load_pem_private_key(key_pem.encode("ascii"), password=None)
            key_id = value.get("key_id")
            now = datetime.now(UTC)
            if (
                not isinstance(key, rsa.RSAPrivateKey)
                or not 2048 <= key.key_size <= 8192
                or key.public_key() != certificate.public_key()
                or (key_id is not None and (not key_id or len(key_id) > 256))
                or not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, UnsupportedAlgorithm):
            raise ValueInvalid("invalid OAuth certificate credential") from None
        return cls(certificate, key, key_id)

    def assertion(self, *, client_id: str, audience: str, algorithm: str) -> str:
        if algorithm not in ASSERTION_ALGORITHMS:
            raise ValueInvalid("invalid OAuth assertion algorithm")
        digest = hashes.SHA256()
        header = {
            "alg": algorithm,
            "typ": "JWT",
            "x5t#S256": _base64url(self.certificate.fingerprint(digest)),
        }
        if self.key_id is not None:
            header["kid"] = self.key_id
        issued = int(datetime.now(UTC).timestamp())
        expires = min(
            issued + _ASSERTION_SECONDS, int(self.certificate.not_valid_after_utc.timestamp())
        )
        if expires <= issued:
            raise ValueInvalid("invalid OAuth certificate credential")
        payload: dict[str, str | int] = {
            "iss": client_id,
            "sub": client_id,
            "aud": audience,
            "iat": issued,
            "nbf": issued,
            "exp": expires,
            "jti": str(uuid4()),
        }
        signed = f"{_segment(header)}.{_segment(payload)}"
        signature_padding: padding.AsymmetricPadding = (
            padding.PSS(mgf=padding.MGF1(digest), salt_length=padding.PSS.DIGEST_LENGTH)
            if algorithm == "PS256"
            else padding.PKCS1v15()
        )
        return f"{signed}.{_base64url(self.key.sign(signed.encode(), signature_padding, digest))}"


def sign_client_assertion(
    value: Mapping[str, str], *, client_id: str, audience: str, algorithm: str
) -> str:
    return CertificateCredential.load(value).assertion(
        client_id=client_id, audience=audience, algorithm=algorithm
    )
