"""Client side of the secure connection: HTTPS with pinned trust, plus E2EE.

The first time a server is used, `inspect_server` reads its TLS certificate and
encryption key and the user compares their fingerprints with the ones printed
by the server (like SSH host keys). `TrustStore` remembers the result. Every
later request is then sealed to the pinned key and travels over a TLS
connection that only accepts the pinned certificate, or a certificate that a
certificate authority vouches for. Plain HTTP is never used.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import http.client
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from . import e2ee, tls
    from .protocol import E2EE_EVENTS_PATH, E2EE_IDENTITY_PATH, E2EE_PATH
except ImportError:
    import e2ee
    import tls
    from protocol import E2EE_EVENTS_PATH, E2EE_IDENTITY_PATH, E2EE_PATH


TRUST_STORE_VERSION = 1


class TransportError(Exception):
    """A problem with the secure connection; the message is meant for users."""


class InsecureServerUrl(TransportError):
    pass


class UntrustedServer(TransportError):
    pass


class ServerIdentityChanged(TransportError):
    pass


@dataclass(frozen=True)
class ServerAddress:
    host: str
    port: int

    @property
    def key(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_server_url(url: str) -> ServerAddress:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https":
        raise InsecureServerUrl(
            "HTTPS is required: use https://HOST:PORT (plain HTTP is not supported)"
        )
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise TransportError("Enter a server address such as https://192.168.1.20:8000")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise TransportError("Server address contains an invalid port") from exc
    return ServerAddress(parsed.hostname, port)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class PinnedServer:
    e2ee_public_key: bytes
    tls_certificate: str | None
    tls_fingerprint: str | None
    pinned_at: str

    @property
    def e2ee_fingerprint(self) -> str:
        return e2ee.public_key_fingerprint(self.e2ee_public_key)


@dataclass(frozen=True)
class ServerInfo:
    url: str
    e2ee_public_key: bytes
    tls_certificate: str | None
    tls_fingerprint: str
    ca_verified: bool

    @property
    def e2ee_fingerprint(self) -> str:
        return e2ee.public_key_fingerprint(self.e2ee_public_key)


class TrustStore:
    """JSON file of verified servers, keyed by host and port."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TransportError(
                f"Could not read the trusted servers at {self.path}: {exc}"
            ) from exc
        servers = raw.get("servers") if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            raise TransportError(
                f"The trusted servers file {self.path} has an invalid format"
            )
        return servers

    def _write(self, servers: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f".{self.path.name}.tmp")
        document = {"version": TRUST_STORE_VERSION, "servers": servers}
        try:
            descriptor = os.open(
                temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2)
            os.replace(temporary_path, self.path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
            raise

    def get(self, url: str) -> PinnedServer | None:
        key = parse_server_url(url).key
        with self._lock:
            record = self._read().get(key)
        if record is None:
            return None
        try:
            certificate = record.get("tls_certificate")
            if certificate is not None and not isinstance(certificate, str):
                raise TypeError("tls_certificate")
            public_key = _b64decode(record["e2ee_public_key"])
            if len(public_key) != e2ee.PUBLIC_KEY_LENGTH:
                raise ValueError("e2ee_public_key")
            return PinnedServer(
                e2ee_public_key=public_key,
                tls_certificate=certificate,
                tls_fingerprint=(
                    None
                    if certificate is None
                    else tls.certificate_fingerprint(tls.certificate_der(certificate))
                ),
                pinned_at=str(record.get("pinned_at", "")),
            )
        except (KeyError, TypeError, ValueError, binascii.Error, AttributeError) as exc:
            raise TransportError(
                f"The trusted entry for {key} in {self.path} is invalid; "
                "remove it with /server forget"
            ) from exc

    def pin(self, url: str, info: ServerInfo) -> PinnedServer:
        key = parse_server_url(url).key
        record = {
            "e2ee_public_key": _b64encode(info.e2ee_public_key),
            "tls_certificate": info.tls_certificate,
            "pinned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with self._lock:
            servers = self._read()
            servers[key] = record
            self._write(servers)
        pinned = self.get(url)
        assert pinned is not None
        return pinned

    def forget(self, url: str) -> bool:
        key = parse_server_url(url).key
        with self._lock:
            servers = self._read()
            if key not in servers:
                return False
            del servers[key]
            self._write(servers)
        return True


_trust_store: TrustStore | None = None
_trust_store_lock = threading.Lock()


def configure_trust_store(path: Path) -> TrustStore:
    global _trust_store
    with _trust_store_lock:
        _trust_store = TrustStore(path)
        return _trust_store


def trust_store() -> TrustStore:
    global _trust_store
    with _trust_store_lock:
        if _trust_store is None:
            _trust_store = TrustStore(tls.app_data_dir() / "known_servers.json")
        return _trust_store


def system_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    ca_file = os.getenv("SUPER_BOT_CA_CERT", "").strip()
    if ca_file:
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(f"Could not load SUPER_BOT_CA_CERT {ca_file}: {exc}") from exc
    return context


def pinned_context(certificate_pem: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=certificate_pem)
    return context


def _unverified_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _context_for(pinned: PinnedServer) -> ssl.SSLContext:
    if pinned.tls_certificate is not None:
        return pinned_context(pinned.tls_certificate)
    return system_context()


def _fetch_identity(
    address: ServerAddress,
    context: ssl.SSLContext,
    timeout: float,
) -> tuple[Any, bytes]:
    connection = http.client.HTTPSConnection(
        address.host, address.port, timeout=timeout, context=context
    )
    try:
        try:
            connection.connect()
        except ssl.SSLCertVerificationError:
            raise
        except ssl.SSLError as exc:
            raise TransportError(
                f"TLS handshake with {address.key} failed ({exc.reason or exc}). "
                "Is the server running the current HTTPS version of Super Bot?"
            ) from exc
        except OSError as exc:
            raise TransportError(f"Could not reach {address.key}: {exc}") from exc
        certificate = connection.sock.getpeercert(binary_form=True)  # type: ignore[union-attr]
        if not certificate:
            raise TransportError(f"{address.key} did not present a TLS certificate")
        try:
            connection.request(
                "GET", E2EE_IDENTITY_PATH, headers={"Accept": "application/json"}
            )
            response = connection.getresponse()
            body = response.read(65_536)
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"Could not reach {address.key}: {exc}") from exc
        if response.status == 404:
            raise TransportError(
                f"{address.key} does not offer end-to-end encryption; "
                "update it to the current Super Bot version"
            )
        if response.status != 200:
            raise TransportError(
                f"{address.key} answered the identity request with HTTP {response.status}"
            )
        try:
            return json.loads(body), certificate
        except ValueError as exc:
            raise TransportError(f"{address.key} sent an invalid identity document") from exc
    finally:
        connection.close()


def _public_key_from_identity(document: Any, address: ServerAddress) -> bytes:
    if not isinstance(document, dict) or document.get("version") != e2ee.PROTOCOL_VERSION:
        raise TransportError(
            f"{address.key} uses an unsupported encryption version; update Super Bot"
        )
    value = document.get("public_key")
    try:
        public_key = _b64decode(value) if isinstance(value, str) else b""
    except (binascii.Error, ValueError):
        public_key = b""
    if len(public_key) != e2ee.PUBLIC_KEY_LENGTH:
        raise TransportError(f"{address.key} sent an invalid encryption key")
    return public_key


def inspect_server(url: str, timeout: float = 5.0) -> ServerInfo:
    """Read a server's TLS certificate and encryption key without trusting them."""
    address = parse_server_url(url)
    try:
        document, certificate = _fetch_identity(address, system_context(), timeout)
        ca_verified = True
    except ssl.SSLCertVerificationError:
        document, certificate = _fetch_identity(address, _unverified_context(), timeout)
        ca_verified = False
    return ServerInfo(
        url=url,
        e2ee_public_key=_public_key_from_identity(document, address),
        tls_certificate=None if ca_verified else ssl.DER_cert_to_PEM_cert(certificate),
        tls_fingerprint=tls.certificate_fingerprint(certificate),
        ca_verified=ca_verified,
    )


@dataclass(frozen=True)
class ServerCheck:
    state: str
    info: ServerInfo
    pinned: PinnedServer | None = None
    reason: str = ""


def check_server(
    url: str,
    timeout: float = 5.0,
    store: TrustStore | None = None,
) -> ServerCheck:
    """Compare a live server with what the user trusted: new, trusted or changed."""
    info = inspect_server(url, timeout)
    pinned = (store or trust_store()).get(url)
    if pinned is None:
        return ServerCheck("new", info)
    if pinned.e2ee_public_key != info.e2ee_public_key:
        return ServerCheck(
            "changed", info, pinned, "The server's end-to-end encryption key changed."
        )
    if pinned.tls_certificate is not None:
        if pinned.tls_fingerprint != info.tls_fingerprint:
            return ServerCheck(
                "changed", info, pinned, "The server's TLS certificate changed."
            )
    elif not info.ca_verified:
        return ServerCheck(
            "changed",
            info,
            pinned,
            "The server's TLS certificate is no longer vouched for by a certificate authority.",
        )
    return ServerCheck("trusted", info, pinned)


def _fingerprint_lines(
    e2ee_fingerprint: str,
    tls_fingerprint: str | None,
    tls_label: str,
) -> list[str]:
    lines = [
        "Server key (end-to-end encryption):",
        f"  {e2ee.format_fingerprint(e2ee_fingerprint)}",
    ]
    if tls_fingerprint is None:
        lines.append("TLS certificate: vouched for by a certificate authority")
    else:
        lines.extend([tls_label, f"  {e2ee.format_fingerprint(tls_fingerprint)}"])
    return lines


def describe_server(info: ServerInfo) -> list[str]:
    return [
        f"Server: {parse_server_url(info.url).key}",
        *_fingerprint_lines(
            info.e2ee_fingerprint,
            None if info.ca_verified else info.tls_fingerprint,
            "TLS certificate SHA-256 (self-signed or private authority):",
        ),
    ]


def describe_pinned(url: str, pinned: PinnedServer) -> list[str]:
    return [
        f"Server: {parse_server_url(url).key}",
        f"Verified: {pinned.pinned_at or 'earlier'}",
        *_fingerprint_lines(
            pinned.e2ee_fingerprint,
            pinned.tls_fingerprint,
            "TLS certificate SHA-256 (pinned):",
        ),
    ]


def confirm_server_on_terminal(
    url: str,
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    store: TrustStore | None = None,
) -> bool:
    """Verify a server before the full-screen app starts; pins it when accepted."""
    check = check_server(url, store=store)
    if check.state == "trusted":
        return True
    if check.state == "changed":
        say(f"WARNING: {check.reason}")
        say("Someone may be intercepting your connection, or the server's keys were reset.")
        say("Compare with the fingerprints the server printed:")
        for line in describe_server(check.info):
            say(f"  {line}")
        say("If the server was reset on purpose, start the app, use /server forget, and reconnect.")
        return False
    say("First connection to this server. Compare with the fingerprints it printed:")
    for line in describe_server(check.info):
        say(f"  {line}")
    try:
        answer = ask("Trust this server and remember it? [y/N] ")
    except EOFError:
        return False
    if answer.strip().casefold() != "y":
        return False
    (store or trust_store()).pin(url, check.info)
    return True


def _require_pin(url: str, store: TrustStore | None) -> PinnedServer:
    pinned = (store or trust_store()).get(url)
    if pinned is None:
        raise UntrustedServer(
            f"The server {parse_server_url(url).key} has not been verified yet. "
            "Use /connect to check and trust its fingerprint."
        )
    return pinned


def _identity_changed(address: ServerAddress) -> ServerIdentityChanged:
    return ServerIdentityChanged(
        f"The identity of {address.key} no longer matches what you trusted. "
        "Someone may be intercepting the connection, or the server's keys were reset. "
        "If you expected that, use /server forget and reconnect."
    )


def _rejected_envelope(error: urllib.error.HTTPError, address: ServerAddress) -> TransportError:
    try:
        document = json.loads(error.read(65_536).decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        document = {}
    code = document.get("error") if isinstance(document, dict) else None
    if code == "unknown_key":
        return _identity_changed(address)
    if code == "stale_request":
        server_time = document.get("server_time") if isinstance(document, dict) else None
        skew = (
            f" by about {abs(int(time.time()) - server_time)} seconds"
            if isinstance(server_time, int)
            else ""
        )
        return TransportError(
            f"Your clock differs from the server's{skew}; correct the system time."
        )
    if code == "too_large":
        return TransportError("The request was too large for the server")
    if code == "replayed_request":
        return TransportError("The server rejected a repeated request; try again")
    return TransportError(
        f"The server could not process the encrypted request (HTTP {error.code})"
    )


def secure_request(
    base_url: str,
    method: str,
    path: str,
    body: Any = None,
    access_token: str = "",
    *,
    timeout: float,
    store: TrustStore | None = None,
) -> tuple[int, Any]:
    """Send one end-to-end encrypted API call and return the inner status and body."""
    address = parse_server_url(base_url)
    pinned = _require_pin(base_url, store)
    context = _context_for(pinned)
    sealed, exchange = e2ee.seal_request(
        pinned.e2ee_public_key,
        {
            "method": method,
            "path": path,
            "authorization": f"Bearer {access_token}" if access_token else None,
            "body": body,
        },
    )
    request = urllib.request.Request(
        f"https://{address.key}{E2EE_PATH}",
        data=sealed,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise _rejected_envelope(exc, address) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise _identity_changed(address) from exc
        raise TransportError(f"Could not reach the server: {exc.reason}") from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise TransportError(f"Could not reach the server: {exc}") from exc
    try:
        result = exchange.open_response(data)
    except e2ee.E2EEError as exc:
        raise _identity_changed(address) from exc
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, int) or isinstance(status, bool):
        raise TransportError("The server sent an unexpected response")
    return status, result.get("body")


class EventStream:
    """Decrypted live events from one session."""

    def __init__(self, websocket: Any, decryptor: e2ee.StreamDecryptor) -> None:
        self._websocket = websocket
        self._decryptor = decryptor

    def recv(self, timeout: float) -> Any | None:
        try:
            frame = self._websocket.recv(timeout=timeout)
        except TimeoutError:
            return None
        if not isinstance(frame, bytes):
            raise TransportError("The server sent an unencrypted event")
        try:
            return self._decryptor.open(frame)
        except e2ee.E2EEError as exc:
            raise TransportError(str(exc)) from exc

@contextlib.contextmanager
def open_event_stream(
    base_url: str,
    session_id: int,
    access_token: str,
    *,
    open_timeout: float = 5.0,
    store: TrustStore | None = None,
) -> Iterator[EventStream]:
    from websockets.sync.client import connect

    address = parse_server_url(base_url)
    pinned = _require_pin(base_url, store)
    context = _context_for(pinned)
    sealed, exchange = e2ee.seal_request(
        pinned.e2ee_public_key,
        {
            "path": f"/events/{session_id}",
            "authorization": f"Bearer {access_token}",
        },
    )
    with contextlib.ExitStack() as stack:
        try:
            websocket = stack.enter_context(
                connect(
                    f"wss://{address.key}{E2EE_EVENTS_PATH}",
                    ssl=context,
                    compression=None,
                    open_timeout=open_timeout,
                    close_timeout=1,
                )
            )
        except ssl.SSLCertVerificationError as exc:
            raise _identity_changed(address) from exc
        websocket.send(sealed)
        reply = websocket.recv(timeout=open_timeout)
        if not isinstance(reply, bytes):
            raise TransportError("The server sent an unencrypted reply")
        try:
            result = exchange.open_response(reply)
        except e2ee.E2EEError as exc:
            raise _identity_changed(address) from exc
        status = result.get("status") if isinstance(result, dict) else None
        if status != 200:
            body = result.get("body") if isinstance(result, dict) else None
            detail = body.get("detail") if isinstance(body, dict) else ""
            raise TransportError(f"Live updates were refused (HTTP {status}): {detail}")
        yield EventStream(websocket, exchange.stream_decryptor())
