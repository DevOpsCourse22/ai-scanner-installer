"""An optional HTTPS certificate the user uploads (PEM certificate + private key).

It is stored as the Kubernetes TLS Secret `ai-scan-tls`, and the app's Ingress
terminates HTTPS with it, the same way on minikube, a server or EKS (where the
load balancer only passes TCP 443 through). Checked before anything is
deployed: the key must match the certificate, the certificate must cover the
app's host name and must not be expired.
"""

from __future__ import annotations

import datetime
import ipaddress
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtensionOID, NameOID

SECRET = "ai-scan-tls"


def _covers(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern.startswith("*."):
        return host.count(".") == pattern.count(".") and host.endswith(pattern[1:])
    return pattern == host


def check(cert_pem: str, key_pem: str, host: str) -> dict[str, Any]:
    """{ok, problems[], subject, names[], expires}. Never raises on bad input."""
    problems: list[str] = []
    info: dict[str, Any] = {"subject": "", "names": [], "expires": ""}
    try:
        certs = x509.load_pem_x509_certificates(cert_pem.encode())
        cert = certs[0]
    except ValueError:
        return {"ok": False, "problems": ["The certificate is not a PEM certificate (-----BEGIN CERTIFICATE-----)."],
                **info}
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except (ValueError, TypeError):
        problems.append("The private key is not an unencrypted PEM key (-----BEGIN PRIVATE KEY-----).")
        key = None
    try:
        sans = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        names = sans.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = [a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    info.update(subject=cert.subject.rfc4514_string(), names=names,
                expires=cert.not_valid_after_utc.isoformat(timespec="minutes"))
    if key is not None:
        pub = lambda k: k.public_bytes(serialization.Encoding.DER,  # noqa: E731
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
        if pub(key.public_key()) != pub(cert.public_key()):
            problems.append("The private key does not belong to this certificate.")
    if cert.not_valid_after_utc < datetime.datetime.now(datetime.timezone.utc):
        problems.append(f"The certificate expired on {info['expires']}.")
    if not host:
        problems.append("Set the app address to https://<a name the certificate covers>.")
    else:
        try:
            ipaddress.ip_address(host)
            problems.append("The app address must be a host name, not an IP, to use a certificate.")
        except ValueError:
            if not any(_covers(n, host) for n in names):
                problems.append(f"The certificate covers {', '.join(names) or 'no names'}, not {host}.")
    return {"ok": not problems, "problems": problems, **info}
