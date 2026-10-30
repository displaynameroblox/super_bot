"""End-to-end encryption between Super Bot clients and the session server.

Every client request is sealed to the server's long-term X25519 key with a
fresh ephemeral key (ephemeral-static Diffie-Hellman, HKDF-SHA256 and
ChaCha20-Poly1305). The server answers with a key derived from the same secret
and a fresh server nonce, so a response only opens for the request that caused
it. Anything that merely relays the bytes, including a TLS-terminating proxy or
tunnel, sees only ciphertext. The server must read the plaintext to run the AI,
so it is one end of the conversation.

Requests carry a timestamp and a random id; the server rejects stale and
repeated ids. There is no forward secrecy against theft of the server's
identity key (TLS still provides it on the wire).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


PROTOCOL_VERSION = 1
PUBLIC_KEY_LENGTH = 32
KEY_ID_LENGTH = 8
SERVER_NONCE_LENGTH = 32
TAG_LENGTH = 16
MAX_REQUEST_BYTES = 1_048_576
MAX_CLOCK_SKEW_SECONDS = 300
REPLAY_WINDOW_SECONDS = 2 * MAX_CLOCK_SKEW_SECONDS + 30
REQUEST_HEADER_LENGTH = 1 + KEY_ID_LENGTH + PUBLIC_KEY_LENGTH
RESPONSE_HEADER_LENGTH = 1 + SERVER_NONCE_LENGTH

_LABEL = b"super-bot-e2ee-v1"
_STREAM_AAD = _LABEL + b" stream"


class E2EEError(Exception):
    """Base class; `code` is a short reason that is safe to send in clear text."""

    code = "invalid_envelope"


class InvalidEnvelope(E2EEError):
    pass


class UnknownServerKey(E2EEError):
    code = "unknown_key"


class EnvelopeTooLarge(E2EEError):
    code = "too_large"


class ReplayedRequest(E2EEError):
    code = "replayed_request"


class StaleRequest(E2EEError):
    code = "stale_request"

    def __init__(self, server_time: int) -> None:
        super().__init__("Request timestamp is outside the allowed window")
        self.server_time = server_time


def public_key_id(public_key: bytes) -> bytes:
    return hashlib.sha256(public_key).digest()[:KEY_ID_LENGTH]


def public_key_fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def format_fingerprint(hex_digest: str) -> str:
    """Group a hex digest in blocks of eight so people can compare it by eye."""
    digest = hex_digest.replace(":", "").replace(" ", "").upper()
    return " ".join(digest[index : index + 8] for index in range(0, len(digest), 8))


def _raw_public_key(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _derive(shared: bytes, salt: bytes, purpose: bytes, length: int) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=_LABEL + b" " + purpose,
    ).derive(shared)


def _cipher_and_nonce(
    shared: bytes,
    salt: bytes,
    purpose: bytes,
) -> tuple[ChaCha20Poly1305, bytes]:
    material = _derive(shared, salt, purpose, 44)
    return ChaCha20Poly1305(material[:32]), material[32:]


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidEnvelope("Encrypted payload is not valid JSON") from exc


def _exchange(private_key: X25519PrivateKey, peer_public_key: bytes) -> bytes:
    try:
        return private_key.exchange(X25519PublicKey.from_public_bytes(peer_public_key))
    except ValueError as exc:
        raise InvalidEnvelope("Invalid public key") from exc


class ServerIdentity:
    """The server's long-term key pair; clients pin its public key."""

    def __init__(self, private_key: X25519PrivateKey) -> None:
        self._private_key = private_key
        self.public_key = _raw_public_key(private_key)
        self.key_id = public_key_id(self.public_key)
        self.fingerprint = public_key_fingerprint(self.public_key)

    @classmethod
    def generate(cls) -> ServerIdentity:
        return cls(X25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> ServerIdentity:
        path = Path(path)
        try:
            pem = path.read_bytes()
        except FileNotFoundError:
            identity = cls.generate()
            identity._save(path)
            return identity
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, X25519PrivateKey):
            raise ValueError(f"{path} does not contain an X25519 private key")
        path.chmod(0o600)
        return cls(key)

    def _save(self, path: Path) -> None:
        pem = self._private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp")
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(pem)
            os.replace(temporary_path, path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
            raise

    def shared_secret(self, peer_public_key: bytes) -> bytes:
        return _exchange(self._private_key, peer_public_key)


class StreamEncryptor:
    """Encrypts server-to-client frames; the frame counter is the AEAD nonce."""

    def __init__(self, key: bytes) -> None:
        self._cipher = ChaCha20Poly1305(key)
        self._counter = 0

    def seal(self, payload: Any) -> bytes:
        frame = self._cipher.encrypt(
            _frame_nonce(self._counter), _encode(payload), _STREAM_AAD
        )
        self._counter += 1
        return frame


class StreamDecryptor:
    def __init__(self, key: bytes) -> None:
        self._cipher = ChaCha20Poly1305(key)
        self._counter = 0

    def open(self, frame: bytes) -> Any:
        try:
            plaintext = self._cipher.decrypt(
                _frame_nonce(self._counter), frame, _STREAM_AAD
            )
        except InvalidTag as exc:
            raise InvalidEnvelope("Event frame was altered, dropped or reordered") from exc
        self._counter += 1
        return _decode(plaintext)


def _frame_nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + counter.to_bytes(8, "big")


class _Exchange:
    def __init__(self, client_public: bytes, server_public: bytes, shared: bytes) -> None:
        self._client_public = client_public
        self._server_public = server_public
        self._shared = shared
        self._server_nonce: bytes | None = None

    def _salt(self, server_nonce: bytes) -> bytes:
        return self._client_public + self._server_public + server_nonce

    def _stream_key(self) -> bytes:
        if self._server_nonce is None:
            raise InvalidEnvelope("The response must be exchanged before a stream starts")
        return _derive(self._shared, self._salt(self._server_nonce), b"stream", 32)


class ClientExchange(_Exchange):
    """Client-side state for one request and its single response."""

    def open_response(self, data: bytes) -> Any:
        if self._server_nonce is not None:
            raise InvalidEnvelope("The response was already opened")
        if (
            len(data) < RESPONSE_HEADER_LENGTH + TAG_LENGTH
            or data[0] != PROTOCOL_VERSION
        ):
            raise InvalidEnvelope("Malformed response")
        header = data[:RESPONSE_HEADER_LENGTH]
        server_nonce = header[1:]
        cipher, nonce = _cipher_and_nonce(
            self._shared, self._salt(server_nonce), b"response"
        )
        try:
            plaintext = cipher.decrypt(nonce, data[RESPONSE_HEADER_LENGTH:], header)
        except InvalidTag as exc:
            raise InvalidEnvelope(
                "The response could not be verified; the server key may have changed"
            ) from exc
        self._server_nonce = server_nonce
        return _decode(plaintext)

    def stream_decryptor(self) -> StreamDecryptor:
        return StreamDecryptor(self._stream_key())


class ServerExchange(_Exchange):
    """Server-side state for one opened request."""

    def __init__(
        self,
        payload: Any,
        client_public: bytes,
        server_public: bytes,
        shared: bytes,
    ) -> None:
        super().__init__(client_public, server_public, shared)
        self.payload = payload

    def seal_response(self, payload: Any) -> bytes:
        server_nonce = os.urandom(SERVER_NONCE_LENGTH)
        cipher, nonce = _cipher_and_nonce(
            self._shared, self._salt(server_nonce), b"response"
        )
        header = bytes([PROTOCOL_VERSION]) + server_nonce
        self._server_nonce = server_nonce
        return header + cipher.encrypt(nonce, _encode(payload), header)

    def stream_encryptor(self) -> StreamEncryptor:
        return StreamEncryptor(self._stream_key())


class ReplayCache:
    """Remembers request ids for as long as their timestamps stay acceptable."""

    def __init__(
        self,
        window_seconds: float = REPLAY_WINDOW_SECONDS,
        max_entries: int = 50_000,
    ) -> None:
        self._window = window_seconds
        self._max_entries = max_entries
        self._expiry_by_id: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, request_id: str, now: float) -> bool:
        """Record an id; return False when it was already seen."""
        with self._lock:
            while self._expiry_by_id:
                oldest_id, expires_at = next(iter(self._expiry_by_id.items()))
                if expires_at > now:
                    break
                del self._expiry_by_id[oldest_id]
            if request_id in self._expiry_by_id:
                return False
            self._expiry_by_id[request_id] = now + self._window
            while len(self._expiry_by_id) > self._max_entries:
                self._expiry_by_id.popitem(last=False)
            return True


def seal_request(
    server_public_key: bytes,
    payload: Any,
    *,
    now: float | None = None,
) -> tuple[bytes, ClientExchange]:
    if len(server_public_key) != PUBLIC_KEY_LENGTH:
        raise ValueError("The server public key must be 32 bytes")
    ephemeral = X25519PrivateKey.generate()
    client_public = _raw_public_key(ephemeral)
    shared = _exchange(ephemeral, server_public_key)
    header = bytes([PROTOCOL_VERSION]) + public_key_id(server_public_key) + client_public
    cipher, nonce = _cipher_and_nonce(
        shared, client_public + server_public_key, b"request"
    )
    plaintext = _encode(
        {
            "ts": int(time.time() if now is None else now),
            "rid": secrets.token_hex(16),
            "data": payload,
        }
    )
    if len(header) + len(plaintext) + TAG_LENGTH > MAX_REQUEST_BYTES:
        raise ValueError("The request is too large to send")
    sealed = header + cipher.encrypt(nonce, plaintext, header)
    return sealed, ClientExchange(client_public, server_public_key, shared)


def open_request(
    identity: ServerIdentity,
    data: bytes,
    *,
    replay_cache: ReplayCache,
    now: float | None = None,
) -> ServerExchange:
    if len(data) > MAX_REQUEST_BYTES:
        raise EnvelopeTooLarge("The request is too large")
    if len(data) < REQUEST_HEADER_LENGTH + TAG_LENGTH or data[0] != PROTOCOL_VERSION:
        raise InvalidEnvelope("Malformed request")
    header = data[:REQUEST_HEADER_LENGTH]
    if not hmac.compare_digest(header[1 : 1 + KEY_ID_LENGTH], identity.key_id):
        raise UnknownServerKey("The request was sealed to a different server key")
    client_public = header[1 + KEY_ID_LENGTH :]
    shared = identity.shared_secret(client_public)
    cipher, nonce = _cipher_and_nonce(
        shared, client_public + identity.public_key, b"request"
    )
    try:
        plaintext = cipher.decrypt(nonce, data[REQUEST_HEADER_LENGTH:], header)
    except InvalidTag as exc:
        raise InvalidEnvelope("The request could not be decrypted") from exc
    envelope = _decode(plaintext)
    if not isinstance(envelope, dict) or "data" not in envelope:
        raise InvalidEnvelope("Malformed request")
    timestamp = envelope.get("ts")
    request_id = envelope.get("rid")
    if (
        not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or not isinstance(request_id, str)
        or not 16 <= len(request_id) <= 64
    ):
        raise InvalidEnvelope("Malformed request")
    current_time = time.time() if now is None else now
    if abs(current_time - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise StaleRequest(int(current_time))
    if not replay_cache.add(request_id, current_time):
        raise ReplayedRequest("This request was already processed")
    return ServerExchange(
        envelope["data"], client_public, identity.public_key, shared
    )
