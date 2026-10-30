import tempfile
from pathlib import Path

from Ai import e2ee, tls, transport

URL = "https://192.168.1.20:8000"
_CERTIFICATES: list[tuple[str, str]] = []


def real_certificate(index: int = 0) -> tuple[str, str]:
    """PEM and SHA-256 of a generated self-signed certificate, cached per index."""
    while len(_CERTIFICATES) <= index:
        with tempfile.TemporaryDirectory() as directory:
            material = tls.prepare_server_tls(directory=Path(directory))
            _CERTIFICATES.append((material.certfile.read_text(), material.fingerprint))
    return _CERTIFICATES[index]


def make_info(
    url: str = URL,
    public_key: bytes | None = None,
    certificate_index: int = 0,
    ca_verified: bool = False,
) -> transport.ServerInfo:
    pem, fingerprint = real_certificate(certificate_index)
    return transport.ServerInfo(
        url=url,
        e2ee_public_key=public_key or e2ee.ServerIdentity.generate().public_key,
        tls_certificate=None if ca_verified else pem,
        tls_fingerprint=fingerprint,
        ca_verified=ca_verified,
    )
