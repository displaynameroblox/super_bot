"""TLS certificates for the session server, plus data-directory helpers."""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import socket
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


CERTIFICATE_FILENAME = "tls-certificate.pem"
PRIVATE_KEY_FILENAME = "tls-private-key.pem"
CERTIFICATE_VALIDITY_DAYS = 3650
RENEW_WITHIN_DAYS = 30


@dataclass(frozen=True)
class TlsMaterial:
    certfile: Path
    keyfile: Path
    keyfile_password: str | None
    fingerprint: str
    generated: bool
    self_signed: bool


def app_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "super_bot"
    if os.name == "nt":
        return Path(os.getenv("APPDATA", Path.home())) / "super_bot"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share")) / "super_bot"


def server_data_dir() -> Path:
    configured = os.getenv("SUPER_BOT_SERVER_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return app_data_dir() / "server"


def certificate_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def certificate_der(pem: str | bytes) -> bytes:
    data = pem.encode("ascii") if isinstance(pem, str) else pem
    return x509.load_pem_x509_certificate(data).public_bytes(serialization.Encoding.DER)


def prepare_server_tls(
    *,
    directory: Path | None = None,
    certfile: str = "",
    keyfile: str = "",
    keyfile_password: str = "",
    now: datetime.datetime | None = None,
) -> TlsMaterial:
    """Use the configured certificate or create and reuse a self-signed one."""
    if bool(certfile) != bool(keyfile):
        raise ValueError("Set both SSL_CERTFILE and SSL_KEYFILE, or neither")
    current_time = now or datetime.datetime.now(datetime.timezone.utc)
    if certfile:
        material = _configured_material(
            Path(certfile).expanduser(),
            Path(keyfile).expanduser(),
            keyfile_password or None,
        )
    else:
        directory = directory or server_data_dir()
        material = _existing_material(directory, current_time) or _generate(
            directory, current_time
        )
    _check_loadable(material)
    return material


def _configured_material(
    certificate_path: Path,
    key_path: Path,
    password: str | None,
) -> TlsMaterial:
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read the TLS certificate {certificate_path}: {exc}") from exc
    if not key_path.is_file():
        raise ValueError(f"TLS private key not found: {key_path}")
    return TlsMaterial(
        certfile=certificate_path,
        keyfile=key_path,
        keyfile_password=password,
        fingerprint=certificate_fingerprint(
            certificate.public_bytes(serialization.Encoding.DER)
        ),
        generated=False,
        self_signed=certificate.issuer == certificate.subject,
    )


def _check_loadable(material: TlsMaterial) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(
            material.certfile,
            material.keyfile,
            material.keyfile_password,
        )
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise ValueError(
            f"Could not load the TLS certificate and key "
            f"({material.certfile}, {material.keyfile}): {exc}"
        ) from exc


def _existing_material(
    directory: Path,
    now: datetime.datetime,
) -> TlsMaterial | None:
    certificate_path = directory / CERTIFICATE_FILENAME
    key_path = directory / PRIVATE_KEY_FILENAME
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    if certificate.not_valid_after_utc - now < datetime.timedelta(days=RENEW_WITHIN_DAYS):
        return None
    public_key_format = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if certificate.public_key().public_bytes(*public_key_format) != key.public_key().public_bytes(
        *public_key_format
    ):
        return None
    key_path.chmod(0o600)
    return TlsMaterial(
        certfile=certificate_path,
        keyfile=key_path,
        keyfile_password=None,
        fingerprint=certificate_fingerprint(
            certificate.public_bytes(serialization.Encoding.DER)
        ),
        generated=False,
        self_signed=True,
    )


def _generate(directory: Path, now: datetime.datetime) -> TlsMaterial:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Super Bot server")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERTIFICATE_VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(_alternative_names()), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = directory / PRIVATE_KEY_FILENAME
    certificate_path = directory / CERTIFICATE_FILENAME
    _write_private_file(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    certificate_path.write_bytes(certificate_pem)
    return TlsMaterial(
        certfile=certificate_path,
        keyfile=key_path,
        keyfile_password=None,
        fingerprint=certificate_fingerprint(
            certificate.public_bytes(serialization.Encoding.DER)
        ),
        generated=True,
        self_signed=True,
    )


def _write_private_file(path: Path, data: bytes) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def _alternative_names() -> list[x509.GeneralName]:
    host_names = ["localhost"]
    addresses = {"127.0.0.1", "::1"}
    host_name = socket.gethostname()
    if host_name:
        host_names.append(host_name)
        if "." not in host_name:
            host_names.append(f"{host_name}.local")
        try:
            for result in socket.getaddrinfo(host_name, None):
                addresses.add(str(result[4][0]).partition("%")[0])
        except OSError:
            pass
    addresses.update(_outbound_addresses())

    names: list[x509.GeneralName] = []
    for host in dict.fromkeys(host_names):
        try:
            names.append(x509.DNSName(host))
        except ValueError:
            continue
    for address in sorted(addresses):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            continue
    return names


def _outbound_addresses() -> set[str]:
    addresses: set[str] = set()
    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe_socket:
                probe_socket.connect((probe, 9))
                addresses.add(str(probe_socket.getsockname()[0]).partition("%")[0])
        except OSError:
            continue
    return addresses
