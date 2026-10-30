import datetime
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from Ai import chat_app, e2ee, gateway, server, tls, transport

REPLY = '{"reply": "Hi there!", "is_coding_request": false}'


def write_ca_signed_certificate(directory: Path) -> tuple[Path, Path, Path]:
    """Create a private CA and a loopback server certificate it signed."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    certificate_path = directory / "leaf.pem"
    key_path = directory / "leaf-key.pem"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(leaf_certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, certificate_path, key_path


class LiveServer:
    """The real gateway behind real TLS on a loopback port."""

    def __init__(self, certfile: Path, keyfile: Path) -> None:
        self.identity = e2ee.ServerIdentity.generate()
        app = gateway.build_gateway(
            server.app,
            self.identity,
            access_error=server.event_access_error,
            stream_events=server.stream_session_events,
        )
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self.port = self._socket.getsockname()[1]
        self.url = f"https://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                ssl_certfile=str(certfile),
                ssl_keyfile=str(keyfile),
                log_level="warning",
                server_header=False,
            )
        )
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._socket]},
            daemon=True,
        )

    def __enter__(self) -> "LiveServer":
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("The test server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)
        self._socket.close()


class SecureConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.multiple(
            server,
            chat_sessions={},
            session_tokens={},
            session_hosts={},
            session_passwords={},
            session_locks={},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        phone = patch("Ai.server.phone_model.chat", return_value=REPLY)
        phone.start()
        self.addCleanup(phone.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = transport.TrustStore(self.directory / "known_servers.json")
        store_patch = patch.object(transport, "_trust_store", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        material = tls.prepare_server_tls(directory=self.directory / "server")
        live = LiveServer(material.certfile, material.keyfile)
        self.live = live.__enter__()
        self.addCleanup(live.__exit__, None, None, None)
        self.material = material

    def call(self, method: str, path: str, body=None, token: str = "") -> tuple[int, dict]:
        return transport.secure_request(
            self.live.url, method, path, body, token, timeout=15
        )

    def trust(self) -> transport.ServerInfo:
        info = transport.inspect_server(self.live.url)
        self.store.pin(self.live.url, info)
        return info

    def start_session(self, password: str = "pw") -> dict:
        status, body = self.call(
            "POST",
            "/start_ses",
            {"AI_persona": "a sailor", "plr_name": "Host", "password": password},
        )
        self.assertEqual(status, 200, body)
        return body

    def test_inspection_reports_the_self_signed_certificate_and_key(self) -> None:
        info = transport.inspect_server(self.live.url)

        self.assertFalse(info.ca_verified)
        self.assertEqual(info.tls_fingerprint, self.material.fingerprint)
        self.assertEqual(info.e2ee_public_key, self.live.identity.public_key)
        self.assertIn("BEGIN CERTIFICATE", info.tls_certificate)

    def test_new_server_is_not_trusted_until_pinned(self) -> None:
        self.assertEqual(transport.check_server(self.live.url, store=self.store).state, "new")
        with self.assertRaises(transport.UntrustedServer):
            self.call("POST", "/login", {"session_ID": 1, "password": ""})

        self.trust()

        self.assertEqual(
            transport.check_server(self.live.url, store=self.store).state, "trusted"
        )

    def test_encrypted_round_trip_covers_the_whole_session_api(self) -> None:
        self.trust()
        grant = self.start_session()
        token, session_id = grant["access_token"], grant["session_ID"]

        status, login = self.call(
            "POST", "/login", {"session_ID": session_id, "password": "pw"}
        )
        self.assertEqual(status, 200)
        guest = login["access_token"]
        status, reply = self.call(
            "POST",
            "/chat",
            {"ses_id": session_id, "msg": "ahoy", "coding_context": "", "show_progress": True},
            guest,
        )
        self.assertEqual((status, reply["is_coding_request"]), (200, False))
        self.assertIn("ahoy", reply["Chat_log"])
        status, session = self.call("GET", f"/chat/{session_id}", None, token)
        self.assertEqual((status, session["session_ID"]), (200, session_id))
        status, style = self.call("POST", "/style", {"ses_id": session_id, "style": "terse"}, token)
        self.assertEqual((status, style), (200, {"AI_style": "terse"}))
        status, skills = self.call(
            "POST", "/skills", {"ses_id": session_id, "skills": {"pirate": "Arr."}}, guest
        )
        self.assertEqual(status, 403, skills)

    def test_client_can_message_after_creating_a_session_with_a_trusted_server(self) -> None:
        self.trust()
        server.phone_model.chat.reset_mock()

        grant = chat_app.start_remote_session(
            self.live.url,
            "Host",
            "a sailor",
        )
        reply = chat_app.send_message(
            self.live.url,
            grant["session_ID"],
            "ahoy",
            grant["access_token"],
        )

        self.assertIn("ahoy", reply["Chat_log"])
        self.assertFalse(reply["is_coding_request"])
        self.assertEqual(server.phone_model.chat.call_count, 2)
        messages = server.phone_model.chat.call_args_list[1].args[0]
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(
            messages[1]["content"],
            "(Start a friendly conversation.)",
        )

    def test_client_saves_chats_and_can_create_a_second_session(self) -> None:
        self.trust()
        sessions = []
        for index, message in enumerate(("first chat", "second chat"), start=1):
            grant = chat_app.start_remote_session(
                self.live.url,
                f"Host {index}",
                "a sailor",
            )
            reply = chat_app.send_message(
                self.live.url,
                grant["session_ID"],
                message,
                grant["access_token"],
            )
            sessions.append(
                chat_app.ChatSession(
                    local_id=f"local-{index}",
                    player_name=grant["plr_name"],
                    persona=grant["AI_persona"],
                    chat_log=reply["Chat_log"],
                    server_session_id=grant["session_ID"],
                    access_token=grant["access_token"],
                    server_url=self.live.url,
                )
            )

        saved_path = self.directory / "chat_sessions.json"
        chat_app.save_sessions(saved_path, sessions)
        restored = chat_app.load_sessions(saved_path)

        self.assertEqual(len(restored), 2)
        self.assertNotEqual(
            restored[0].server_session_id,
            restored[1].server_session_id,
        )
        self.assertEqual([item.player_name for item in restored], ["Host 1", "Host 2"])
        self.assertIn("first chat", restored[0].chat_log)
        self.assertIn("second chat", restored[1].chat_log)
        self.assertTrue(all(item.access_token for item in restored))

    def test_inner_errors_keep_their_http_status(self) -> None:
        self.trust()

        status, body = self.call("POST", "/login", {"session_ID": 99, "password": ""})

        self.assertEqual((status, body["detail"]), (404, "Chat session not found"))

    def test_the_network_only_exposes_health_and_the_encrypted_endpoints(self) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        def plain_status(method: str, path: str, body: bytes | None = None) -> int:
            import http.client

            connection = http.client.HTTPSConnection(
                "127.0.0.1", self.live.port, timeout=10, context=context
            )
            try:
                connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
                return connection.getresponse().status
            finally:
                connection.close()

        self.assertEqual(plain_status("GET", "/health"), 200)
        self.assertEqual(plain_status("POST", "/start_ses", b"{}"), 404)
        self.assertEqual(plain_status("POST", "/login", b"{}"), 404)
        self.assertEqual(plain_status("GET", "/chat/1"), 404)
        self.assertEqual(plain_status("GET", "/docs"), 404)
        self.assertEqual(plain_status("GET", "/openapi.json"), 404)

    def test_plain_http_is_not_spoken_on_the_tls_port(self) -> None:
        with socket.create_connection(("127.0.0.1", self.live.port), timeout=10) as raw:
            raw.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            raw.settimeout(5)
            try:
                reply = raw.recv(200)
            except (ConnectionError, TimeoutError):
                reply = b""
        self.assertNotIn(b"200 OK", reply)

    def test_http_urls_are_refused_by_the_client(self) -> None:
        with self.assertRaises(transport.InsecureServerUrl):
            transport.secure_request(
                f"http://127.0.0.1:{self.live.port}", "GET", "/chat/1", timeout=5
            )
        with self.assertRaises(transport.InsecureServerUrl):
            transport.inspect_server(f"http://127.0.0.1:{self.live.port}")

    def test_a_different_certificate_than_the_pinned_one_is_refused(self) -> None:
        info = self.trust()
        other = tls.prepare_server_tls(directory=self.directory / "other")
        pinned = self.store.get(self.live.url)
        self.assertIsNotNone(pinned)
        impostor = transport.ServerInfo(
            url=info.url,
            e2ee_public_key=info.e2ee_public_key,
            tls_certificate=other.certfile.read_text(),
            tls_fingerprint=other.fingerprint,
            ca_verified=False,
        )
        self.store.pin(self.live.url, impostor)

        with self.assertRaises(transport.ServerIdentityChanged):
            self.call("POST", "/login", {"session_ID": 1, "password": ""})
        self.assertEqual(
            transport.check_server(self.live.url, store=self.store).state, "changed"
        )

    def test_a_different_encryption_key_than_the_pinned_one_is_refused(self) -> None:
        info = self.trust()
        impostor_key = e2ee.ServerIdentity.generate().public_key
        self.store.pin(
            self.live.url,
            transport.ServerInfo(
                url=info.url,
                e2ee_public_key=impostor_key,
                tls_certificate=info.tls_certificate,
                tls_fingerprint=info.tls_fingerprint,
                ca_verified=False,
            ),
        )

        with self.assertRaises(transport.ServerIdentityChanged):
            self.call("POST", "/login", {"session_ID": 1, "password": ""})
        check = transport.check_server(self.live.url, store=self.store)
        self.assertEqual((check.state, "encryption key" in check.reason), ("changed", True))

    def test_live_events_arrive_encrypted_and_in_order(self) -> None:
        self.trust()
        grant = self.start_session()
        token, session_id = grant["access_token"], grant["session_ID"]

        with transport.open_event_stream(self.live.url, session_id, token) as stream:
            joined = stream.recv(timeout=10)
            self.assertEqual(joined, {"type": "joined", "player_name": "Host"})
            status, _ = self.call(
                "POST",
                "/chat",
                {"ses_id": session_id, "msg": "hello", "coding_context": "", "show_progress": True},
                token,
            )
            self.assertEqual(status, 200)
            kinds = []
            while True:
                event = stream.recv(timeout=10)
                self.assertIsNotNone(event, "timed out waiting for events")
                kinds.append(event.get("phase") or event["type"])
                if event["type"] == "message":
                    self.assertEqual(event["text"], "hello")
                    break
        self.assertEqual(kinds[0], "received")
        self.assertEqual(kinds[-2:], ["complete", "message"])

    def test_live_events_refuse_a_bad_token(self) -> None:
        self.trust()
        grant = self.start_session()

        with self.assertRaises(transport.TransportError) as caught:
            with transport.open_event_stream(
                self.live.url, grant["session_ID"], "wrong-token"
            ):
                self.fail("the stream should not open with a bad token")

        self.assertIn("401", str(caught.exception))

    def test_the_traffic_on_the_wire_contains_no_plaintext(self) -> None:
        self.trust()
        captured: list[bytes] = []
        original = e2ee.seal_request

        def recording_seal(*args, **kwargs):
            sealed, exchange = original(*args, **kwargs)
            captured.append(sealed)
            return sealed, exchange

        with patch.object(transport.e2ee, "seal_request", recording_seal):
            self.start_session(password="very-secret-password")

        self.assertEqual(len(captured), 1)
        for secret in (b"very-secret-password", b"start_ses", b"a sailor", b"Host"):
            self.assertNotIn(secret, captured[0])

    def test_a_replayed_envelope_is_rejected_by_the_server(self) -> None:
        self.trust()
        pinned = self.store.get(self.live.url)
        sealed, exchange = e2ee.seal_request(
            pinned.e2ee_public_key,
            {"method": "GET", "path": "/chat/1", "authorization": None, "body": None},
        )
        context = transport.pinned_context(pinned.tls_certificate)

        def post() -> tuple[int, bytes]:
            import urllib.error
            import urllib.request

            request = urllib.request.Request(
                f"{self.live.url}/e2ee", data=sealed, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=10, context=context) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        first_status, first_body = post()
        second_status, second_body = post()

        self.assertEqual(first_status, 200)
        self.assertEqual(exchange.open_response(first_body)["status"], 401)
        self.assertEqual(second_status, 409)
        self.assertEqual(json.loads(second_body), {"error": "replayed_request"})


class CertificateAuthorityTests(unittest.TestCase):
    def test_a_ca_signed_server_needs_no_pin_for_tls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca_path, certificate_path, key_path = write_ca_signed_certificate(root)
            material = tls.prepare_server_tls(
                certfile=str(certificate_path), keyfile=str(key_path)
            )
            self.assertFalse(material.generated)
            store = transport.TrustStore(root / "known_servers.json")
            with LiveServer(material.certfile, material.keyfile) as live:
                with patch.dict(os.environ, {"SUPER_BOT_CA_CERT": str(ca_path)}):
                    check = transport.check_server(live.url, store=store)
                    self.assertEqual(check.state, "new")
                    self.assertTrue(check.info.ca_verified)
                    self.assertIsNone(check.info.tls_certificate)
                    store.pin(live.url, check.info)

                    status, _ = transport.secure_request(
                        live.url, "POST", "/login", {"session_ID": 1}, timeout=10, store=store
                    )
                    self.assertEqual(status, 404)

                # Without the CA the certificate is unknown, so the pin no longer matches.
                with patch.dict(os.environ, {"SUPER_BOT_CA_CERT": ""}):
                    self.assertEqual(
                        transport.check_server(live.url, store=store).state, "changed"
                    )
                    with self.assertRaises(transport.ServerIdentityChanged):
                        transport.secure_request(
                            live.url, "POST", "/login", {"session_ID": 1}, timeout=10, store=store
                        )


if __name__ == "__main__":
    unittest.main()
